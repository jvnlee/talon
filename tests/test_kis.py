import json
from datetime import datetime

import httpx
import pytest

from talon.errors import SourceError
from talon.sources.kis import KisClient

NOW = datetime(2026, 7, 15, 12, 0, 0)
VALID_EXPIRY = "2026-07-16 11:59:59"
STALE_EXPIRY = "2026-07-15 12:05:00"


class Recorder:
    def __init__(self, quote_responses=None, token_responses=None):
        self.token_calls = 0
        self.quote_calls = 0
        self.quote_responses = quote_responses or []
        self.token_responses = token_responses or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            self.token_calls += 1
            if self.token_responses:
                return self.token_responses.pop(0)
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok{self.token_calls}",
                    "expires_in": 86400,
                    "access_token_token_expired": VALID_EXPIRY,
                },
            )
        self.quote_calls += 1
        if self.quote_responses:
            return self.quote_responses.pop(0)
        return httpx.Response(200, json={"rt_cd": "0", "output": {"stck_prpr": "279500"}})


def make_client(tmp_path, recorder, **kwargs):
    slept = kwargs.pop("slept", [])
    return KisClient(
        "key",
        "secret",
        base_url="https://kis.test",
        token_path=tmp_path / "kis_token.json",
        transport=httpx.MockTransport(recorder.handler),
        sleep=slept.append,
        now=lambda: NOW,
        **kwargs,
    )


def test_issues_and_caches_token(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        payload = client.get("/quote", "TR1", {})

    assert payload["output"]["stck_prpr"] == "279500"
    assert recorder.token_calls == 1
    cached = json.loads((tmp_path / "kis_token.json").read_text())
    assert cached["access_token"] == "tok1"
    assert cached["expired_at"] == VALID_EXPIRY


def test_reuses_cached_token_across_clients(tmp_path):
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        client.get("/quote", "TR1", {})
    with make_client(tmp_path, recorder) as second:
        second.get("/quote", "TR1", {})

    assert recorder.token_calls == 1
    assert recorder.quote_calls == 2


def test_stale_cached_token_is_reissued(tmp_path):
    (tmp_path / "kis_token.json").write_text(
        json.dumps({"access_token": "old", "expired_at": STALE_EXPIRY})
    )
    recorder = Recorder()
    with make_client(tmp_path, recorder) as client:
        client.get("/quote", "TR1", {})

    assert recorder.token_calls == 1


def test_error_response_raises_with_code(tmp_path):
    recorder = Recorder(
        quote_responses=[
            httpx.Response(200, json={"rt_cd": "1", "msg_cd": "OPSQ1234", "msg1": "권한 없음"})
        ]
    )
    with make_client(tmp_path, recorder) as client, pytest.raises(SourceError, match="OPSQ1234"):
        client.get("/quote", "TR1", {})


def test_rate_limit_is_retried(tmp_path):
    recorder = Recorder(
        quote_responses=[
            httpx.Response(200, json={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 초과"}),
            httpx.Response(200, json={"rt_cd": "0", "output": {"ok": "1"}}),
        ]
    )
    with make_client(tmp_path, recorder) as client:
        payload = client.get("/quote", "TR1", {})

    assert payload["output"]["ok"] == "1"
    assert recorder.quote_calls == 2


def test_expired_token_mid_session_is_refreshed(tmp_path):
    recorder = Recorder(
        quote_responses=[
            httpx.Response(200, json={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "토큰 만료"}),
            httpx.Response(200, json={"rt_cd": "0", "output": {"ok": "1"}}),
        ]
    )
    with make_client(tmp_path, recorder) as client:
        payload = client.get("/quote", "TR1", {})

    assert payload["output"]["ok"] == "1"
    assert recorder.token_calls == 2


def test_server_error_is_retried(tmp_path):
    recorder = Recorder(
        quote_responses=[
            httpx.Response(500, text="oops"),
            httpx.Response(200, json={"rt_cd": "0", "output": {"ok": "1"}}),
        ]
    )
    with make_client(tmp_path, recorder) as client:
        payload = client.get("/quote", "TR1", {})

    assert payload["output"]["ok"] == "1"


def test_token_throttle_waits_and_retries(tmp_path):
    slept = []
    recorder = Recorder(
        token_responses=[
            httpx.Response(
                200, json={"error_code": "EGW00133", "error_description": "잠시 후 재시도"}
            ),
            httpx.Response(
                200,
                json={
                    "access_token": "tok-late",
                    "expires_in": 86400,
                    "access_token_token_expired": VALID_EXPIRY,
                },
            ),
        ]
    )
    with make_client(tmp_path, recorder, slept=slept) as client:
        client.get("/quote", "TR1", {})

    assert recorder.token_calls == 2
    assert 60.0 in slept


def test_throttle_paces_consecutive_calls(tmp_path):
    slept = []
    ticks = iter([0.0, 0.0, 0.01, 0.01, 0.02])
    recorder = Recorder()
    client = KisClient(
        "key",
        "secret",
        base_url="https://kis.test",
        token_path=tmp_path / "kis_token.json",
        rps=2.0,
        transport=httpx.MockTransport(recorder.handler),
        sleep=slept.append,
        clock=lambda: next(ticks),
        now=lambda: NOW,
    )
    client.get("/quote", "TR1", {})
    client.get("/quote", "TR1", {})
    client.close()

    assert any(wait > 0.4 for wait in slept)


def test_missing_keys_are_rejected(tmp_path):
    with pytest.raises(SourceError, match="앱키"):
        KisClient("", "", base_url="https://kis.test", token_path=tmp_path / "t.json")

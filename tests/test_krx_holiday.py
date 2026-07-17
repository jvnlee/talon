from datetime import date

import httpx
import pytest

from talon.errors import SchemaDriftError, SourceError
from talon.sources.krx_holiday import fetch_holidays


def _handler(payload):
    def handle(request):
        if "GenerateOTP" in request.url.path:
            return httpx.Response(200, text="OTP123")
        assert request.url.path.endswith("OPN99000001.jspx")
        assert b"code=OTP123" in request.content
        return httpx.Response(200, json=payload)

    return handle


def test_fetch_holidays_parses_block1():
    payload = {
        "block1": [
            {"calnd_dd": "2026-07-17", "dy_tp_cd": "FRI", "holdy_nm": "제헌절"},
            {"calnd_dd": "2026-10-05", "dy_tp_cd": "MON", "holdy_nm": "추석"},
        ]
    }
    got = fetch_holidays(2026, transport=httpx.MockTransport(_handler(payload)))
    assert got == {date(2026, 7, 17): "제헌절", date(2026, 10, 5): "추석"}


def test_fetch_holidays_empty_year():
    got = fetch_holidays(2027, transport=httpx.MockTransport(_handler({"block1": []})))
    assert got == {}


def test_fetch_holidays_schema_drift_is_not_retried():
    calls = []

    def handle(request):
        if "GenerateOTP" in request.url.path:
            return httpx.Response(200, text="OTP123")
        calls.append(request.url.path)
        return httpx.Response(200, json={"unexpected": []})

    with pytest.raises(SchemaDriftError):
        fetch_holidays(2026, transport=httpx.MockTransport(handle))
    assert len(calls) == 1


def test_fetch_holidays_rejects_non_json_body():
    def handle(request):
        if "GenerateOTP" in request.url.path:
            return httpx.Response(200, text="OTP123")
        return httpx.Response(200, text="LOGOUT")

    with pytest.raises(SchemaDriftError):
        fetch_holidays(2026, transport=httpx.MockTransport(handle))


def test_fetch_holidays_retries_http_errors():
    attempts = []

    def handle(request):
        if "GenerateOTP" in request.url.path:
            return httpx.Response(200, text="OTP123")
        attempts.append(request.url.path)
        if len(attempts) < 3:
            return httpx.Response(500)
        return httpx.Response(
            200, json={"block1": [{"calnd_dd": "2026-07-17", "holdy_nm": "제헌절"}]}
        )

    waits = []
    got = fetch_holidays(2026, transport=httpx.MockTransport(handle), sleep=waits.append)
    assert got == {date(2026, 7, 17): "제헌절"}
    assert waits == [2.0, 4.0]


def test_fetch_holidays_gives_up_after_max_attempts():
    def handle(request):
        return httpx.Response(500)

    waits = []
    with pytest.raises(SourceError):
        fetch_holidays(2026, transport=httpx.MockTransport(handle), sleep=waits.append)
    assert len(waits) == 2

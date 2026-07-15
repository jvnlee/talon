import json
import logging
import os
import stat
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from talon.errors import SourceError

log = logging.getLogger(__name__)

TOKEN_URL = "/oauth2/tokenP"
TOKEN_EXPIRY_MARGIN = timedelta(minutes=10)
TOKEN_RETRY_WAIT = 60.0
RATE_LIMIT_CODES = {"EGW00201"}
TOKEN_EXPIRED_CODES = {"EGW00123"}
TOKEN_THROTTLED_CODES = {"EGW00133"}
MAX_ATTEMPTS = 3


class KisClient:
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        base_url: str,
        token_path: Path,
        rps: float = 8.0,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        if not (app_key and app_secret):
            raise SourceError("KIS 앱키가 없습니다 (TALON_KIS_APP_KEY / TALON_KIS_APP_SECRET)")
        self._app_key = app_key
        self._app_secret = app_secret
        self._token_path = token_path
        self._min_interval = 1.0 / rps if rps > 0 else 0.0
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._last_call: float | None = None
        self._token: str | None = None
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "KisClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def token(self) -> str:
        if self._token is not None:
            return self._token
        cached = self._read_cached_token()
        if cached is not None:
            self._token = cached
            return cached
        return self._issue_token()

    def _read_cached_token(self) -> str | None:
        if not self._token_path.exists():
            return None
        try:
            payload = json.loads(self._token_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        token = payload.get("access_token")
        expired_at_text = payload.get("expired_at")
        if not token or not isinstance(expired_at_text, str):
            return None
        try:
            expired_at = datetime.fromisoformat(expired_at_text)
        except ValueError:
            return None
        if self._now() >= expired_at - TOKEN_EXPIRY_MARGIN:
            return None
        return str(token)

    def _issue_token(self) -> str:
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        for attempt in range(MAX_ATTEMPTS):
            response = self._client.post(TOKEN_URL, json=body)
            payload = self._payload(response)
            token = payload.get("access_token")
            if token:
                self._token = str(token)
                self._write_token_cache(str(token), payload)
                return self._token
            error_code = str(payload.get("error_code") or "")
            if error_code in TOKEN_THROTTLED_CODES and attempt < MAX_ATTEMPTS - 1:
                self._sleep(TOKEN_RETRY_WAIT)
                continue
            raise SourceError(
                f"KIS 토큰 발급 실패: {error_code} {payload.get('error_description', '')}"
            )
        raise SourceError("KIS 토큰 발급 실패: 재시도 소진")

    def _write_token_cache(self, token: str, payload: dict[str, Any]) -> None:
        expired_at_text = payload.get("access_token_token_expired")
        if isinstance(expired_at_text, str):
            expired_at = expired_at_text
        else:
            expires_in = float(payload.get("expires_in") or 0)
            expired_at = (self._now() + timedelta(seconds=expires_in)).isoformat(sep=" ")
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(
            json.dumps(
                {
                    "access_token": token,
                    "expired_at": expired_at,
                    "issued_at": self._now().isoformat(timespec="seconds"),
                }
            )
        )
        os.chmod(self._token_path, stat.S_IRUSR | stat.S_IWUSR)

    def _throttle(self) -> None:
        if self._last_call is not None:
            elapsed = self._clock() - self._last_call
            wait = self._min_interval - elapsed
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _headers(self, tr_id: str, tr_cont: str = "") -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.token()}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": tr_id,
            "tr_cont": tr_cont,
            "custtype": "P",
        }

    def get(
        self,
        path: str,
        tr_id: str,
        params: dict[str, str],
        *,
        tr_cont: str = "",
    ) -> dict[str, Any]:
        last_error = ""
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            try:
                response = self._client.get(
                    path, params=params, headers=self._headers(tr_id, tr_cont)
                )
            except httpx.HTTPError as exc:
                raise SourceError(f"KIS 요청 실패: {exc}") from exc
            if response.status_code >= 500 and attempt < MAX_ATTEMPTS - 1:
                last_error = f"HTTP {response.status_code}"
                self._sleep(1.0 * (attempt + 1))
                continue
            payload = self._payload(response)
            rt_cd = str(payload.get("rt_cd", ""))
            if rt_cd == "0":
                return payload
            msg_cd = str(payload.get("msg_cd") or "")
            msg = str(payload.get("msg1") or "").strip()
            if msg_cd in RATE_LIMIT_CODES and attempt < MAX_ATTEMPTS - 1:
                last_error = f"{msg_cd} {msg}"
                self._sleep(1.0 * (attempt + 1))
                continue
            if msg_cd in TOKEN_EXPIRED_CODES and attempt < MAX_ATTEMPTS - 1:
                last_error = f"{msg_cd} {msg}"
                self._token = None
                self._issue_token()
                continue
            raise SourceError(f"KIS 응답 오류: {msg_cd} {msg} (tr_id={tr_id})")
        raise SourceError(f"KIS 요청 재시도 소진 (tr_id={tr_id}): {last_error}")

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        try:
            return dict(response.json())
        except (json.JSONDecodeError, ValueError) as exc:
            raise SourceError(
                f"KIS 응답이 JSON이 아닙니다 (HTTP {response.status_code})"
            ) from exc

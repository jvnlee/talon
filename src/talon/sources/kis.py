import fcntl
import json
import logging
import os
import stat
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from talon.errors import SourceError

if TYPE_CHECKING:
    from talon.config import TalonSettings

log = logging.getLogger(__name__)

TOKEN_URL = "/oauth2/tokenP"
TOKEN_EXPIRY_MARGIN = timedelta(minutes=10)
TOKEN_RETRY_WAIT = 60.0
RATE_LIMIT_CODES = {"EGW00201"}
TOKEN_EXPIRED_CODES = {"EGW00123"}
TOKEN_THROTTLED_CODES = {"EGW00133"}
MAX_ATTEMPTS = 3
STALE_TOKEN_ATTEMPTS = 8
PACER_CLAMP_SECONDS = 60.0


class RatePacer:
    def __init__(
        self,
        path: Path,
        *,
        rps: float,
        penalty_rps: float,
        penalty_seconds: float,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._path = path
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._penalty_interval = 1.0 / penalty_rps if penalty_rps > 0 else 0.0
        self._penalty_seconds = penalty_seconds
        self._clock = clock
        self._sleep = sleep

    def acquire(self) -> None:
        slot = self._claim_slot()
        wait = slot - self._clock()
        if wait > 0:
            self._sleep(wait)

    def report_rate_limit(self) -> None:
        def update(state: dict[str, float], now: float) -> None:
            state["penalty_until"] = max(state["penalty_until"], now + self._penalty_seconds)

        self._mutate(update)

    def _claim_slot(self) -> float:
        slot = 0.0

        def update(state: dict[str, float], now: float) -> None:
            nonlocal slot
            interval = self._penalty_interval if now < state["penalty_until"] else self._interval
            slot = max(now, state["next_at"])
            if slot - now > PACER_CLAMP_SECONDS:
                slot = now
            state["next_at"] = slot + interval

        self._mutate(update)
        return slot

    def _mutate(self, update: Callable[[dict[str, float], float], None]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)
        with open(self._path, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                state = self._parse(handle.read())
                update(state, self._clock())
                handle.seek(0)
                handle.truncate()
                json.dump(state, handle)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _parse(text: str) -> dict[str, float]:
        try:
            raw = json.loads(text)
            return {
                "next_at": float(raw["next_at"]),
                "penalty_until": float(raw["penalty_until"]),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return {"next_at": 0.0, "penalty_until": 0.0}


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
        pacer: RatePacer | None = None,
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
        self._pacer = pacer
        self._last_call: float | None = None
        self._token: str | None = None
        self._token_expired_at: datetime | None = None
        self._token_lock = threading.Lock()
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "KisClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def token(self) -> str:
        token = self._token
        if token is not None and not self._is_stale():
            return token
        with self._token_lock:
            if self._token is not None and not self._is_stale():
                return self._token
            cached = self._read_cached_token()
            if cached is not None:
                self._token, self._token_expired_at = cached
                return self._token
            stale = self._token
            self._token = None
            self._token_expired_at = None
            return self._issue_token(stale=stale)

    def _is_stale(self) -> bool:
        expired_at = self._token_expired_at
        if expired_at is None:
            return True
        return self._now() >= expired_at - TOKEN_EXPIRY_MARGIN

    def _read_cached_token(self) -> tuple[str, datetime] | None:
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
        return str(token), expired_at

    def _issue_token(self, stale: str | None = None) -> str:
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        for _ in range(STALE_TOKEN_ATTEMPTS):
            response = self._client.post(TOKEN_URL, json=body)
            payload = self._payload(response)
            token = payload.get("access_token")
            if token:
                token = str(token)
                if stale is not None and token == stale:
                    self._sleep(TOKEN_RETRY_WAIT)
                    continue
                self._store_token(token, payload)
                return token
            error_code = str(payload.get("error_code") or "")
            if error_code in TOKEN_THROTTLED_CODES:
                self._sleep(TOKEN_RETRY_WAIT)
                continue
            raise SourceError(
                f"KIS 토큰 발급 실패: {error_code} {payload.get('error_description', '')}"
            )
        raise SourceError("KIS 토큰 발급 실패: 재발급이 이전 토큰만 반환")

    def _store_token(self, token: str, payload: dict[str, Any]) -> None:
        expired_at = self._expiry_from_payload(payload)
        self._token = token
        self._token_expired_at = expired_at
        self._write_token_cache(token, expired_at)

    def _expiry_from_payload(self, payload: dict[str, Any]) -> datetime:
        expired_at_text = payload.get("access_token_token_expired")
        if isinstance(expired_at_text, str):
            try:
                return datetime.fromisoformat(expired_at_text)
            except ValueError:
                pass
        expires_in = float(payload.get("expires_in") or 0)
        return self._now() + timedelta(seconds=expires_in)

    def _write_token_cache(self, token: str, expired_at: datetime) -> None:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(
            json.dumps(
                {
                    "access_token": token,
                    "expired_at": expired_at.isoformat(sep=" "),
                    "issued_at": self._now().isoformat(timespec="seconds"),
                }
            )
        )
        os.chmod(self._token_path, stat.S_IRUSR | stat.S_IWUSR)

    def _refresh_token(self, stale: str) -> None:
        with self._token_lock:
            if self._token == stale:
                self._token = None
                self._token_expired_at = None
                self._issue_token(stale=stale)

    def _throttle(self) -> None:
        if self._pacer is not None:
            self._pacer.acquire()
            return
        if self._last_call is not None:
            elapsed = self._clock() - self._last_call
            wait = self._min_interval - elapsed
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _headers(self, tr_id: str, tr_cont: str, token: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {token}",
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
            token = self.token()
            try:
                response = self._client.get(
                    path, params=params, headers=self._headers(tr_id, tr_cont, token)
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
            if msg_cd in RATE_LIMIT_CODES:
                if self._pacer is not None:
                    self._pacer.report_rate_limit()
                if attempt < MAX_ATTEMPTS - 1:
                    last_error = f"{msg_cd} {msg}"
                    self._sleep(1.0 * (attempt + 1))
                    continue
            if msg_cd in TOKEN_EXPIRED_CODES and attempt < MAX_ATTEMPTS - 1:
                last_error = f"{msg_cd} {msg}"
                self._refresh_token(token)
                self._sleep(1.0 * (attempt + 1))
                continue
            raise SourceError(f"KIS 응답 오류: {msg_cd} {msg} (tr_id={tr_id})")
        raise SourceError(f"KIS 요청 재시도 소진 (tr_id={tr_id}): {last_error}")

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        try:
            return dict(response.json())
        except (json.JSONDecodeError, ValueError) as exc:
            raise SourceError(f"KIS 응답이 JSON이 아닙니다 (HTTP {response.status_code})") from exc


def build_kis_client(cfg: "TalonSettings") -> KisClient:
    pacer = RatePacer(
        cfg.kis_pacer_path,
        rps=cfg.kis_rps,
        penalty_rps=cfg.kis_penalty_rps,
        penalty_seconds=cfg.kis_penalty_seconds,
    )
    return KisClient(
        cfg.kis_app_key,
        cfg.kis_app_secret,
        base_url=cfg.kis_base_url,
        token_path=cfg.kis_token_path,
        rps=cfg.kis_rps,
        timeout=cfg.request_timeout,
        pacer=pacer,
    )

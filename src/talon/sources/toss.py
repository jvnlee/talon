import logging
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import httpx

from talon.errors import SourceError
from talon.models import Candle, CandlePage, InvestorFlowRecord

log = logging.getLogger(__name__)

TOKEN_PATH = "/oauth2/token"
DEFAULT_BASE_URL = "https://openapi.tossinvest.com"


class TossError(SourceError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def rate_group(path: str) -> str:
    if path == TOKEN_PATH:
        return "AUTH"
    if path.startswith("/api/v1/candles"):
        return "MARKET_DATA_CHART"
    if path.startswith("/api/v1/market-indicators"):
        return "MARKET_INDICATOR_CHART" if path.endswith("/candles") else "MARKET_INDICATOR"
    if path.startswith(("/api/v1/prices", "/api/v1/orderbook", "/api/v1/trades")):
        return "MARKET_DATA"
    if path.startswith("/api/v1/price-limits"):
        return "MARKET_DATA"
    if path.startswith("/api/v1/stocks"):
        return "STOCK"
    if path.startswith("/api/v1/rankings"):
        return "RANKING"
    if path.startswith(("/api/v1/market-calendar", "/api/v1/exchange-rate")):
        return "MARKET_INFO"
    return "DEFAULT"


class RatePacer:
    def __init__(
        self,
        rps: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_ok: dict[str, float] = {}

    def wait(self, group: str) -> None:
        if self._interval <= 0:
            return
        now = self._clock()
        next_ok = self._next_ok.get(group, now)
        if next_ok > now:
            self._sleep(next_ok - now)
            now = next_ok
        self._next_ok[group] = now + self._interval


class TossClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        rps: float = 5.0,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_attempts: int = 4,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._sleep = sleep
        self._clock = clock
        self._max_attempts = max_attempts
        self._pacer = RatePacer(rps, clock, sleep)
        self._token: str | None = None
        self._token_deadline = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "TossClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _fetch_token(self) -> None:
        self._pacer.wait("AUTH")
        try:
            response = self._http.post(
                TOKEN_PATH,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.TransportError as exc:
            raise TossError(0, "transport-error", str(exc)) from exc
        if response.status_code != 200:
            raise self._to_error(response)
        payload = response.json()
        self._token = payload["access_token"]
        self._token_deadline = self._clock() + float(payload.get("expires_in", 3600)) - 60.0

    def _ensure_token(self) -> str:
        if self._token is None or self._clock() >= self._token_deadline:
            self._fetch_token()
        assert self._token is not None
        return self._token

    @staticmethod
    def _to_error(response: httpx.Response) -> TossError:
        try:
            payload = response.json()
        except ValueError:
            return TossError(response.status_code, "http-error", response.text[:200])
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, str):
            return TossError(response.status_code, error, payload.get("error_description", ""))
        if isinstance(error, dict):
            return TossError(
                response.status_code,
                error.get("code", "unknown"),
                error.get("message", ""),
            )
        return TossError(response.status_code, "http-error", str(payload)[:200])

    @staticmethod
    def _reset_seconds(response: httpx.Response) -> float:
        try:
            return float(response.headers.get("X-RateLimit-Reset", "1"))
        except ValueError:
            return 1.0

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        group = rate_group(path)
        attempts = 0
        rate_retries = 0
        refreshed = False
        while True:
            token = self._ensure_token()
            self._pacer.wait(group)
            attempts += 1
            try:
                response = self._http.get(
                    path, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.TransportError as exc:
                if attempts >= self._max_attempts:
                    raise TossError(0, "transport-error", str(exc)) from exc
                self._sleep(min(0.5 * 2 ** (attempts - 1), 8.0))
                continue
            if response.status_code == 401 and not refreshed:
                refreshed = True
                self._token = None
                continue
            if response.status_code == 429:
                rate_retries += 1
                if rate_retries > 3:
                    raise self._to_error(response)
                self._sleep(min(max(self._reset_seconds(response), 0.5), 15.0))
                continue
            if response.status_code >= 500:
                if attempts >= self._max_attempts:
                    raise self._to_error(response)
                self._sleep(min(0.5 * 2 ** (attempts - 1), 8.0))
                continue
            if response.status_code >= 400:
                raise self._to_error(response)
            payload = response.json()
            if isinstance(payload, dict) and "result" in payload:
                return payload["result"]
            return payload

    def candles(
        self,
        symbol: str,
        interval: str,
        *,
        count: int = 200,
        before: str | None = None,
        adjusted: bool = False,
    ) -> CandlePage:
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "count": count,
            "adjusted": adjusted,
        }
        if before is not None:
            params["before"] = before
        return CandlePage.model_validate(self._request("/api/v1/candles", params))

    def indicator_candles(
        self,
        symbol: str,
        interval: str,
        *,
        count: int = 200,
        before: str | None = None,
    ) -> CandlePage:
        params: dict[str, Any] = {"interval": interval, "count": count}
        if before is not None:
            params["before"] = before
        return CandlePage.model_validate(
            self._request(f"/api/v1/market-indicators/{symbol}/candles", params)
        )

    def candles_since(
        self,
        symbol: str,
        interval: str,
        since: datetime | None,
        *,
        max_pages: int = 30,
        adjusted: bool = False,
        indicator: bool = False,
    ) -> list[Candle]:
        collected: dict[datetime, Candle] = {}
        before: str | None = None
        for _ in range(max_pages):
            if indicator:
                page = self.indicator_candles(symbol, interval, before=before)
            else:
                page = self.candles(symbol, interval, before=before, adjusted=adjusted)
            if not page.candles:
                break
            for candle in page.candles:
                collected.setdefault(candle.ts, candle)
            oldest = min(candle.ts for candle in page.candles)
            if since is not None and oldest <= since:
                break
            if page.next_before is None:
                break
            before = page.next_before
        candles = sorted(collected.values(), key=lambda c: c.ts)
        if since is not None:
            candles = [c for c in candles if c.ts > since]
        return candles

    def investor_trading(
        self,
        symbol: str,
        *,
        interval: str = "1d",
        count: int = 100,
        until: str | None = None,
    ) -> list[InvestorFlowRecord]:
        params: dict[str, Any] = {"interval": interval, "count": count}
        if until is not None:
            params["until"] = until
        result = self._request(f"/api/v1/market-indicators/{symbol}/investor-trading", params)
        return [InvestorFlowRecord.from_toss(raw) for raw in result.get("records", [])]

    def market_calendar_kr(self, day: date | None = None) -> dict[str, Any]:
        params = {"date": day.isoformat()} if day is not None else None
        result = self._request("/api/v1/market-calendar/KR", params)
        return dict(result)

import logging
from datetime import date

import httpx

from talon.errors import SourceError

log = logging.getLogger(__name__)

EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

_WHEN = {
    "time-pre-market": "bmo",
    "time-after-hours": "amc",
}


def fetch_earnings_calendar(
    day: date,
    *,
    timeout: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, str]]:
    try:
        with httpx.Client(timeout=timeout, transport=transport, headers=_HEADERS) as client:
            response = client.get(EARNINGS_URL, params={"date": day.isoformat()})
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"Nasdaq 실적 캘린더 요청 실패 ({day}): {exc}") from exc
    except ValueError as exc:
        raise SourceError(f"Nasdaq 실적 캘린더 응답이 JSON이 아닙니다 ({day})") from exc
    status = payload.get("status") or {}
    if status.get("rCode") not in (200, None):
        raise SourceError(f"Nasdaq 실적 캘린더 오류 응답 ({day}): {status}")
    data = payload.get("data") or {}
    rows = data.get("rows") or []
    results = []
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        results.append({"symbol": symbol, "when": _WHEN.get(row.get("time", ""), "unknown")})
    return results

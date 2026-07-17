import csv
import io
import logging
from datetime import date, datetime

import httpx
import polars as pl

from talon.data.store import US_MACRO_DAILY_SCHEMA
from talon.errors import SourceError

log = logging.getLogger(__name__)

FREDGRAPH_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
CBOE_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
FREDGRAPH_DATE_COLUMN = "observation_date"
REALTIME_ALL_START = "1776-07-04"
REALTIME_ALL_END = "9999-12-31"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}


def _get_text(url: str, params: dict[str, str], timeout: float, transport) -> str:
    try:
        with httpx.Client(
            timeout=timeout, transport=transport, follow_redirects=True, headers=_HEADERS
        ) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceError(f"요청 실패 ({url}): {exc}") from exc
    return response.text


def _macro_frame(rows: list[dict[str, object]], source: str, captured_at: datetime) -> pl.DataFrame:
    for row in rows:
        row["source"] = source
        row["captured_at"] = captured_at
    if not rows:
        return pl.DataFrame(schema=US_MACRO_DAILY_SCHEMA)
    return pl.DataFrame(rows, schema=US_MACRO_DAILY_SCHEMA).sort("day")


def parse_fredgraph(text: str, series_id: str, captured_at: datetime) -> pl.DataFrame:
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise SourceError(f"FRED {series_id} 응답이 비어 있습니다") from exc
    if len(header) != 2 or header[0] != FREDGRAPH_DATE_COLUMN or header[1] != series_id:
        raise SourceError(f"FRED {series_id} 헤더가 예상과 다릅니다: {header}")
    rows: list[dict[str, object]] = []
    for line in reader:
        if len(line) != 2 or line[1] in ("", "."):
            continue
        rows.append({"day": date.fromisoformat(line[0]), "value": float(line[1])})
    if not rows:
        raise SourceError(f"FRED {series_id} 관측치가 한 건도 없습니다")
    return _macro_frame(rows, f"fred:{series_id}", captured_at)


def fetch_fred_series(
    series_id: str,
    captured_at: datetime,
    *,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> pl.DataFrame:
    text = _get_text(FREDGRAPH_URL, {"id": series_id}, timeout, transport)
    return parse_fredgraph(text, series_id, captured_at)


def parse_vix_history(text: str, captured_at: datetime) -> pl.DataFrame:
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise SourceError("CBOE VIX 응답이 비어 있습니다") from exc
    expected_header = ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]
    if [column.strip().upper() for column in header[:5]] != expected_header:
        raise SourceError(f"CBOE VIX 헤더가 예상과 다릅니다: {header}")
    rows: list[dict[str, object]] = []
    for line in reader:
        if len(line) < 5 or not line[4]:
            continue
        month, day, year = line[0].split("/")
        rows.append(
            {"day": date(int(year), int(month), int(day)), "value": float(line[4])}
        )
    if not rows:
        raise SourceError("CBOE VIX 관측치가 한 건도 없습니다")
    return _macro_frame(rows, "cboe", captured_at)


def fetch_vix_history(
    captured_at: datetime,
    *,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> pl.DataFrame:
    text = _get_text(CBOE_VIX_URL, {}, timeout, transport)
    return parse_vix_history(text, captured_at)


def fetch_release_dates(
    release_id: int,
    api_key: str,
    *,
    start: date,
    end: date,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> list[date]:
    if not api_key:
        raise SourceError("FRED API 키가 없습니다 (TALON_FRED_API_KEY)")
    params = {
        "release_id": str(release_id),
        "api_key": api_key,
        "file_type": "json",
        "realtime_start": REALTIME_ALL_START,
        "realtime_end": REALTIME_ALL_END,
        "include_release_dates_with_no_data": "true",
        "limit": "10000",
    }
    try:
        with httpx.Client(timeout=timeout, transport=transport, headers=_HEADERS) as client:
            response = client.get(FRED_RELEASE_DATES_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"FRED release/dates 요청 실패 (release {release_id}): {exc}") from exc
    entries = payload.get("release_dates")
    if entries is None:
        raise SourceError(f"FRED release/dates 응답에 release_dates가 없습니다: {payload}")
    days = []
    for entry in entries:
        day = date.fromisoformat(entry["date"])
        if start <= day <= end:
            days.append(day)
    return sorted(set(days))

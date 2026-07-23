import lzma
import struct
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple

import httpx

from talon.errors import SourceError

DUKASCOPY_BASE = "https://datafeed.dukascopy.com/datafeed"

SYMBOL_CODES: dict[str, str] = {
    "US500": "USA500IDXUSD",
    "USTEC": "USATECHIDXUSD",
}

RECORD_SIZE = 24
RECORD_FORMAT = ">Iiiiif"
PRICE_SCALE = 1000.0
BAR_SECONDS = 60
DAY_SECONDS = 86400
TARGET_BAR_OFFSET = 22140
WINDOW_START_OFFSET = 21600
RETRY_STATUS = frozenset({500, 502, 503, 504})
USER_AGENT = "Mozilla/5.0 (talon)"


class DukascopyBar(NamedTuple):
    offset: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class ProxyBar(NamedTuple):
    close: float
    bar_ts: datetime
    stale_minutes: int


def day_file_url(symbol_code: str, day: date, side: str = "BID") -> str:
    return (
        f"{DUKASCOPY_BASE}/{symbol_code}/{day.year:04d}/"
        f"{day.month - 1:02d}/{day.day:02d}/{side}_candles_min_1.bi5"
    )


def _decompress(payload: bytes) -> bytes:
    try:
        return lzma.decompress(payload, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError as exc:
        raise SourceError(f"Dukascopy LZMA 해제 실패: {exc}") from exc


def decode_day_file(payload: bytes) -> list[DukascopyBar]:
    raw = _decompress(payload)
    if not raw or len(raw) % RECORD_SIZE != 0:
        raise SourceError(f"Dukascopy 레코드 길이 비정합: {len(raw)} bytes")
    bars: list[DukascopyBar] = []
    previous = -1
    for index in range(len(raw) // RECORD_SIZE):
        chunk = raw[index * RECORD_SIZE : (index + 1) * RECORD_SIZE]
        offset, open_i, close_i, low_i, high_i, volume = struct.unpack(RECORD_FORMAT, chunk)
        if offset <= previous or offset >= DAY_SECONDS:
            raise SourceError(f"Dukascopy 오프셋 비단조: {offset} (직전 {previous})")
        previous = offset
        bars.append(
            DukascopyBar(
                offset=offset,
                open=open_i / PRICE_SCALE,
                high=high_i / PRICE_SCALE,
                low=low_i / PRICE_SCALE,
                close=close_i / PRICE_SCALE,
                volume=volume,
            )
        )
    return bars


def select_1510_bar(bars: list[DukascopyBar], day: date) -> ProxyBar | None:
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    by_offset = {bar.offset: bar for bar in bars}
    offset = TARGET_BAR_OFFSET
    while offset >= WINDOW_START_OFFSET:
        bar = by_offset.get(offset)
        if bar is not None and bar.volume > 0:
            return ProxyBar(
                close=bar.close,
                bar_ts=midnight + timedelta(seconds=offset + BAR_SECONDS),
                stale_minutes=(TARGET_BAR_OFFSET - offset) // BAR_SECONDS,
            )
        offset -= BAR_SECONDS
    return None


def fetch_day_file(
    symbol_code: str,
    day: date,
    *,
    side: str = "BID",
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes | None:
    url = day_file_url(symbol_code, day, side)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with httpx.Client(
                timeout=timeout, transport=transport, follow_redirects=True
            ) as client:
                response = client.get(url, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < retries - 1:
                sleep(1.0 * (attempt + 1))
            continue
        if response.status_code == 404:
            return None
        if response.status_code in RETRY_STATUS:
            last_error = SourceError(f"Dukascopy {url} HTTP {response.status_code}")
            if attempt < retries - 1:
                sleep(1.0 * (attempt + 1))
            continue
        if response.status_code != 200:
            raise SourceError(f"Dukascopy {url} HTTP {response.status_code}")
        return response.content
    raise SourceError(f"Dukascopy 요청 실패 ({url}): {last_error}")


def fetch_1510_bars(
    symbol: str,
    day: date,
    *,
    side: str = "BID",
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> list[DukascopyBar] | None:
    code = SYMBOL_CODES.get(symbol)
    if code is None:
        raise SourceError(f"알 수 없는 Dukascopy 심볼: {symbol}")
    payload = fetch_day_file(
        code, day, side=side, timeout=timeout, transport=transport, retries=retries, sleep=sleep
    )
    if payload is None:
        return None
    return decode_day_file(payload)

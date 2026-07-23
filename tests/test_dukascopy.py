import lzma
import struct
from datetime import UTC, date, datetime

import httpx
import pytest

from talon.errors import SourceError
from talon.sources.dukascopy import (
    RECORD_FORMAT,
    day_file_url,
    decode_day_file,
    fetch_1510_bars,
    fetch_day_file,
    select_1510_bar,
)

DAY = date(2026, 7, 15)
WINDOW_OFFSETS = tuple(range(21600, 22141, 60))


def pack(records: list[tuple[int, float, float, float, float, float]]) -> bytes:
    raw = b"".join(
        struct.pack(
            RECORD_FORMAT,
            offset,
            round(o * 1000),
            round(c * 1000),
            round(lo * 1000),
            round(hi * 1000),
            vol,
        )
        for offset, o, c, lo, hi, vol in records
    )
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def make_day(
    overrides: dict[int, tuple[float, float, float, float, float]] | None = None,
) -> bytes:
    overrides = overrides or {}
    records = []
    for minute in range(1440):
        offset = minute * 60
        if offset in overrides:
            o, c, lo, hi, vol = overrides[offset]
        else:
            o = c = lo = hi = 7500.0
            vol = 0.05
        records.append((offset, o, c, lo, hi, vol))
    return pack(records)


def no_sleep(_seconds: float) -> None:
    return None


def test_decode_scales_prices_and_orders_offsets():
    bars = decode_day_file(make_day({22140: (7508.0, 7510.0, 7505.0, 7512.0, 0.1)}))
    assert len(bars) == 1440
    assert bars[0].offset == 0
    assert bars[0].close == 7500.0
    target = next(bar for bar in bars if bar.offset == 22140)
    assert target.close == 7510.0
    assert target.high == 7512.0
    assert target.low == 7505.0
    assert all(bars[i].offset < bars[i + 1].offset for i in range(len(bars) - 1))


def test_select_picks_target_bar_on_time():
    bars = decode_day_file(make_day({22140: (7508.0, 7510.0, 7505.0, 7512.0, 0.1)}))
    proxy = select_1510_bar(bars, DAY)
    assert proxy is not None
    assert proxy.close == 7510.0
    assert proxy.stale_minutes == 0
    assert proxy.bar_ts == datetime(2026, 7, 15, 6, 10, tzinfo=UTC)


def test_select_falls_back_to_last_fresh_bar_and_records_stale():
    bars = decode_day_file(
        make_day(
            {
                22140: (7500.0, 7500.0, 7500.0, 7500.0, 0.0),
                22080: (7508.0, 7509.0, 7505.0, 7510.0, 0.1),
            }
        )
    )
    proxy = select_1510_bar(bars, DAY)
    assert proxy is not None
    assert proxy.close == 7509.0
    assert proxy.stale_minutes == 1
    assert proxy.bar_ts == datetime(2026, 7, 15, 6, 9, tzinfo=UTC)


def test_select_returns_none_when_window_is_all_stale():
    overrides = {offset: (7500.0, 7500.0, 7500.0, 7500.0, 0.0) for offset in WINDOW_OFFSETS}
    bars = decode_day_file(make_day(overrides))
    assert select_1510_bar(bars, DAY) is None


def test_decode_rejects_bad_record_length():
    payload = lzma.compress(b"x" * 25, format=lzma.FORMAT_ALONE)
    with pytest.raises(SourceError, match="레코드 길이"):
        decode_day_file(payload)


def test_decode_rejects_nonmonotonic_offsets():
    raw = struct.pack(RECORD_FORMAT, 60, 1, 1, 1, 1, 0.1) + struct.pack(
        RECORD_FORMAT, 0, 1, 1, 1, 1, 0.1
    )
    payload = lzma.compress(raw, format=lzma.FORMAT_ALONE)
    with pytest.raises(SourceError, match="비단조"):
        decode_day_file(payload)


def test_decode_rejects_corrupt_lzma():
    with pytest.raises(SourceError, match="LZMA"):
        decode_day_file(b"not a real lzma stream")


def test_url_month_is_zero_indexed():
    url = day_file_url("USA500IDXUSD", DAY)
    assert "/2026/06/15/BID_candles_min_1.bi5" in url


def test_fetch_uses_zero_indexed_month_path():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(404)

    result = fetch_day_file(
        "USA500IDXUSD", DAY, transport=httpx.MockTransport(handler), sleep=no_sleep
    )
    assert result is None
    assert seen["path"] == "/datafeed/USA500IDXUSD/2026/06/15/BID_candles_min_1.bi5"


def test_fetch_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"<html>not found</html>")

    result = fetch_day_file(
        "USA500IDXUSD", DAY, transport=httpx.MockTransport(handler), sleep=no_sleep
    )
    assert result is None


def test_fetch_retries_5xx_then_succeeds():
    calls = {"n": 0}
    payload = make_day({22140: (7500.0, 7501.0, 7499.0, 7502.0, 0.1)})

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=payload)

    bars = fetch_1510_bars(
        "US500", DAY, transport=httpx.MockTransport(handler), sleep=no_sleep
    )
    assert bars is not None
    assert calls["n"] == 3
    assert select_1510_bar(bars, DAY).close == 7501.0


def test_fetch_raises_after_persistent_5xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with pytest.raises(SourceError, match="요청 실패"):
        fetch_day_file(
            "USA500IDXUSD", DAY, transport=httpx.MockTransport(handler), sleep=no_sleep
        )
    assert calls["n"] == 3


def test_fetch_1510_bars_rejects_unknown_symbol():
    with pytest.raises(SourceError, match="알 수 없는"):
        fetch_1510_bars("DAX", DAY)

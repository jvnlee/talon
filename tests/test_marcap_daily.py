import io
from datetime import date, datetime

import httpx
import polars as pl
import pytest

from talon.data.store import DAILY_SNAPSHOT_SCHEMA, MARKET_CAP_SCHEMA
from talon.errors import SchemaDriftError, SourceError
from talon.sources.marcap_daily import MarcapSource

DAY_1 = date(2026, 7, 8)
DAY_2 = date(2026, 7, 9)


def year_frame(rows):
    return pl.DataFrame(
        {
            "Code": [r["code"] for r in rows],
            "Name": ["종목"] * len(rows),
            "Close": [r.get("close", 100.0) for r in rows],
            "ChangesRatio": [r.get("change", 1.5) for r in rows],
            "Volume": [r.get("volume", 10.0) for r in rows],
            "Amount": [r.get("amount", 1000.0) for r in rows],
            "Open": [r.get("open", 90.0) for r in rows],
            "High": [r.get("high", 110.0) for r in rows],
            "Low": [r.get("low", 80.0) for r in rows],
            "Marcap": [r.get("cap", r.get("close", 100.0) * r.get("shares", 5.0)) for r in rows],
            "Stocks": [int(r.get("shares", 5)) for r in rows],
            "Date": [datetime.combine(r["day"], datetime.min.time()) for r in rows],
        }
    )


def parquet_bytes(frame):
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    return buffer.getvalue()


def serve(payloads):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        body = payloads.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler), calls


def make_source(tmp_path, payloads, **kwargs):
    transport, calls = serve(payloads)
    source = MarcapSource(
        tmp_path / "marcap",
        transport=transport,
        min_rows=kwargs.pop("min_rows", 1),
        **kwargs,
    )
    return source, calls


def default_payloads(frame):
    return {"/FinanceData/marcap/master/data/marcap-2026.parquet": parquet_bytes(frame)}


def test_snapshot_maps_columns_and_filters(tmp_path):
    frame = year_frame(
        [
            {"code": "005930", "day": DAY_1, "close": 278000.0, "shares": 5846278608},
            {"code": "000001", "day": DAY_1, "close": 0.0, "high": 0.0, "cap": 0.0},
            {"code": "005930", "day": DAY_2, "close": 279000.0, "shares": 5846278608},
        ]
    )
    with make_source(tmp_path, default_payloads(frame))[0] as source:
        daily, caps = source.snapshot(DAY_1)

    assert dict(daily.schema) == DAILY_SNAPSHOT_SCHEMA
    assert dict(caps.schema) == MARKET_CAP_SCHEMA
    assert daily.height == 1
    assert caps.height == 1
    row = daily.row(0, named=True)
    assert row["day"] == DAY_1
    assert row["symbol"] == "005930"
    assert row["close"] == 278000.0
    assert row["value"] == 1000.0
    assert row["change_pct"] == 1.5
    cap_row = caps.row(0, named=True)
    assert cap_row["cap"] == 278000.0 * 5846278608
    assert cap_row["shares"] == 5846278608.0


def test_year_file_downloaded_once_and_cached(tmp_path):
    frame = year_frame(
        [
            {"code": "005930", "day": DAY_1},
            {"code": "005930", "day": DAY_2},
        ]
    )
    source, calls = make_source(tmp_path, default_payloads(frame))
    with source:
        source.snapshot(DAY_1)
        source.snapshot(DAY_2)
    assert len(calls) == 1
    assert (tmp_path / "marcap" / "marcap-2026.parquet").exists()


def test_existing_cache_file_used_without_download(tmp_path):
    frame = year_frame([{"code": "005930", "day": DAY_1}])
    cache = tmp_path / "marcap"
    cache.mkdir(parents=True)
    (cache / "marcap-2026.parquet").write_bytes(parquet_bytes(frame))
    source, calls = make_source(tmp_path, {})
    with source:
        daily, _ = source.snapshot(DAY_1)
    assert daily.height == 1
    assert calls == []


def test_stale_cache_refreshed_when_day_beyond_max(tmp_path):
    stale = year_frame([{"code": "005930", "day": DAY_1}])
    fresh = year_frame(
        [
            {"code": "005930", "day": DAY_1},
            {"code": "005930", "day": DAY_2},
        ]
    )
    cache = tmp_path / "marcap"
    cache.mkdir(parents=True)
    (cache / "marcap-2026.parquet").write_bytes(parquet_bytes(stale))
    source, calls = make_source(tmp_path, default_payloads(fresh))
    with source:
        daily, _ = source.snapshot(DAY_2)
    assert daily.height == 1
    assert len(calls) == 1


def test_unpublished_day_raises_without_redownload_after_fresh_fetch(tmp_path):
    frame = year_frame([{"code": "005930", "day": DAY_1}])
    source, calls = make_source(tmp_path, default_payloads(frame))
    with source:
        source.snapshot(DAY_1)
        with pytest.raises(SourceError, match="not yet published"):
            source.snapshot(DAY_2)
        with pytest.raises(SourceError, match="not yet published"):
            source.snapshot(DAY_2)
    assert len(calls) == 1


def test_missing_trading_day_within_range_raises(tmp_path):
    frame = year_frame(
        [
            {"code": "005930", "day": date(2026, 7, 7)},
            {"code": "005930", "day": DAY_2},
        ]
    )
    source, _ = make_source(tmp_path, default_payloads(frame))
    with source, pytest.raises(SourceError, match="no rows"):
        source.snapshot(DAY_1)


def test_too_few_rows_raises(tmp_path):
    frame = year_frame([{"code": "005930", "day": DAY_1}])
    source, _ = make_source(tmp_path, default_payloads(frame), min_rows=2)
    with source, pytest.raises(SourceError, match="suspiciously low"):
        source.snapshot(DAY_1)


def test_schema_drift_raises(tmp_path):
    frame = year_frame([{"code": "005930", "day": DAY_1}]).drop("Amount")
    source, _ = make_source(tmp_path, default_payloads(frame))
    with source, pytest.raises(SchemaDriftError, match="Amount"):
        source.snapshot(DAY_1)


def test_download_error_raises_source_error(tmp_path):
    with make_source(tmp_path, {})[0] as source, pytest.raises(SourceError, match="HTTP 404"):
        source.snapshot(DAY_1)


def test_latest_available_reports_max_date(tmp_path):
    frame = year_frame(
        [
            {"code": "005930", "day": DAY_1},
            {"code": "005930", "day": DAY_2},
        ]
    )
    with make_source(tmp_path, default_payloads(frame))[0] as source:
        assert source.latest_available(2026) == DAY_2


def test_latest_available_falls_back_to_previous_year(tmp_path):
    frame = year_frame([{"code": "005930", "day": date(2025, 12, 30)}])
    payloads = {"/FinanceData/marcap/master/data/marcap-2025.parquet": parquet_bytes(frame)}
    with make_source(tmp_path, payloads)[0] as source:
        assert source.latest_available(2026) == date(2025, 12, 30)

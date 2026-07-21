from datetime import date

import polars as pl

from conftest import make_candle, utc
from talon.data.store import (
    DAILY_CANDLES,
    DAILY_SNAPSHOT_SCHEMA,
    MINUTE_CANDLES,
    candles_to_frame,
    investor_records_to_frame,
    normalize_daily_snapshot,
)
from talon.models import InvestorFlowRecord


def test_upsert_dedupes_and_keeps_last(series):
    first = candles_to_frame(
        [
            make_candle(utc(2026, 7, 10, 0, 1), price=100.0),
            make_candle(utc(2026, 7, 10, 0, 2), price=101.0),
        ]
    )
    assert series.upsert(MINUTE_CANDLES, "005930", first) == 2

    revised = candles_to_frame(
        [
            make_candle(utc(2026, 7, 10, 0, 2), price=999.0),
            make_candle(utc(2026, 7, 10, 0, 3), price=102.0),
        ]
    )
    assert series.upsert(MINUTE_CANDLES, "005930", revised) == 1

    frame = series.read(MINUTE_CANDLES, "005930")
    assert frame.height == 3
    assert frame.sort("ts").get_column("close").to_list() == [100.0, 999.0, 102.0]


def test_upsert_empty_frame_is_noop(series):
    assert series.upsert(MINUTE_CANDLES, "005930", candles_to_frame([])) == 0
    assert series.read(MINUTE_CANDLES, "005930") is None


def test_last_value(series):
    assert series.last_value(MINUTE_CANDLES, "005930") is None
    series.upsert(
        MINUTE_CANDLES,
        "005930",
        candles_to_frame([make_candle(utc(2026, 7, 10, 0, 5))]),
    )
    assert series.last_value(MINUTE_CANDLES, "005930") == utc(2026, 7, 10, 0, 5)


def test_names_sorted(series):
    for symbol in ("035720", "005930"):
        series.upsert(
            MINUTE_CANDLES, symbol, candles_to_frame([make_candle(utc(2026, 7, 10, 0, 1))])
        )
    assert series.names(MINUTE_CANDLES) == ["005930", "035720"]
    assert series.names("unknown") == []


def test_no_tmp_files_left(series, cfg):
    series.upsert(MINUTE_CANDLES, "005930", candles_to_frame([make_candle(utc(2026, 7, 10, 0, 1))]))
    leftovers = list(cfg.parquet_dir.rglob("*.tmp"))
    assert leftovers == []


def test_date_partitioned_store(snapshots):
    day1 = date(2026, 7, 9)
    day2 = date(2026, 7, 10)
    frame1 = pl.DataFrame({"symbol": ["005930"], "close": [70000.0]})
    frame2 = pl.DataFrame({"symbol": ["005930"], "close": [71000.0]})

    assert not snapshots.has_date(DAILY_CANDLES, day1)
    snapshots.write_date(DAILY_CANDLES, day1, frame1)
    snapshots.write_date(DAILY_CANDLES, day2, frame2)

    assert snapshots.has_date(DAILY_CANDLES, day1)
    assert snapshots.dates(DAILY_CANDLES) == [day1, day2]
    latest = snapshots.latest(DAILY_CANDLES)
    assert latest is not None
    assert latest[0] == day2
    assert latest[1].get_column("close").to_list() == [71000.0]
    assert snapshots.read_date(DAILY_CANDLES, date(2026, 1, 1)) is None


def test_investor_frame_upsert_by_day(series):
    raw = {
        "date": "2026-07-10",
        "updatedAt": "2026-07-10T18:10:00+09:00",
        "individual": {"buyAmount": "100", "sellAmount": "90"},
        "foreigner": {"buyAmount": "50", "sellAmount": "60"},
        "institution": {"buyAmount": "30", "sellAmount": "20", "breakdown": {"pension": {}}},
        "otherCorporation": {"buyAmount": "1", "sellAmount": "2"},
    }
    record = InvestorFlowRecord.from_toss(raw)
    frame = investor_records_to_frame([record])
    assert series.upsert("investor_trading", "KOSPI", frame, key="day") == 1

    revised = record.model_copy(update={"individual_buy": 111.0})
    assert (
        series.upsert("investor_trading", "KOSPI", investor_records_to_frame([revised]), key="day")
        == 0
    )
    stored = series.read("investor_trading", "KOSPI")
    assert stored.height == 1
    assert stored.get_column("individual_buy").to_list() == [111.0]
    assert stored.get_column("updated_at").to_list() == [utc(2026, 7, 10, 9, 10)]


def test_normalize_daily_snapshot_nulls_prices_on_no_trade_rows():
    frame = pl.DataFrame(
        {
            "day": [date(2016, 9, 30)] * 4,
            "symbol": ["TRADED", "HALTED", "GISE", "DEAD"],
            "open": [100.0, 0.0, 0.0, 0.0],
            "high": [110.0, 0.0, 0.0, 0.0],
            "low": [90.0, 0.0, 0.0, 0.0],
            "close": [105.0, 3270.0, 3760.0, 0.0],
            "volume": [1000.0, 0.0, 0.0, 0.0],
            "value": [105000.0, 0.0, 0.0, 0.0],
            "change_pct": [1.5, 0.0, 14.81, 0.0],
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )

    normalized = normalize_daily_snapshot(frame)

    assert dict(normalized.schema) == DAILY_SNAPSHOT_SCHEMA
    assert normalized.get_column("symbol").to_list() == ["TRADED", "HALTED", "GISE"]
    traded = normalized.row(0, named=True)
    assert (traded["open"], traded["high"], traded["low"]) == (100.0, 110.0, 90.0)
    for row in normalized.tail(2).iter_rows(named=True):
        assert row["open"] is None
        assert row["high"] is None
        assert row["low"] is None
        assert row["volume"] == 0.0
    assert normalized.row(2, named=True)["close"] == 3760.0
    assert normalized.row(2, named=True)["change_pct"] == 14.81


def test_normalize_daily_snapshot_nulls_prices_when_high_is_null():
    frame = pl.DataFrame(
        {
            "day": [date(2016, 9, 30)],
            "symbol": ["NULLHIGH"],
            "open": [100.0],
            "high": [None],
            "low": [90.0],
            "close": [105.0],
            "volume": [10.0],
            "value": [1050.0],
            "change_pct": [None],
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )

    normalized = normalize_daily_snapshot(frame)

    row = normalized.row(0, named=True)
    assert row["open"] is None
    assert row["low"] is None
    assert row["close"] == 105.0

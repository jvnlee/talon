from datetime import date

import polars as pl

from talon.data.store import DAILY_CANDLES, DAILY_SNAPSHOT_SCHEMA, MARKET_CAP
from talon.errors import SourceError
from talon.ingest.history import backfill_daily

START = date(2026, 7, 6)
END = date(2026, 7, 10)
BROKEN_DAY = date(2026, 7, 9)


def frame_for(day):
    return pl.DataFrame(
        {
            "day": [day],
            "symbol": ["005930"],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
            "value": [1.0],
            "change_pct": [0.0],
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )


def caps_for(day):
    return pl.DataFrame({"day": [day], "symbol": ["005930"], "value": [1.0], "volume": [1.0]})


def install_sources(monkeypatch):
    def fetch_ohlcv(day):
        if day == BROKEN_DAY:
            raise SourceError("krx down")
        return frame_for(day)

    monkeypatch.setattr("talon.ingest.history.fetch_daily_ohlcv", fetch_ohlcv)
    monkeypatch.setattr("talon.ingest.history.fetch_market_cap", caps_for)


def test_backfill_skips_loads_and_records_failures(cfg, cal, state, snapshots, monkeypatch):
    install_sources(monkeypatch)
    snapshots.write_date(DAILY_CANDLES, date(2026, 7, 7), frame_for(date(2026, 7, 7)))
    snapshots.write_date(MARKET_CAP, date(2026, 7, 7), caps_for(date(2026, 7, 7)))
    progressed = []

    summary = backfill_daily(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=START,
        end=END,
        sleep=lambda seconds: None,
        progress=lambda index, total, day: progressed.append((index, total)),
    )
    assert summary.sessions == 5
    assert summary.skipped == 1
    assert summary.loaded == 3
    assert summary.failed == [BROKEN_DAY.isoformat()]
    assert summary.status == "partial"
    assert progressed[-1] == (5, 5)
    assert snapshots.has_date(DAILY_CANDLES, date(2026, 7, 6))
    assert not snapshots.has_date(DAILY_CANDLES, BROKEN_DAY)
    assert state.recent_runs("backfill-daily")[0].ok is False


def test_backfill_second_run_only_retries_failures(cfg, cal, state, snapshots, monkeypatch):
    install_sources(monkeypatch)
    backfill_daily(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=START,
        end=END,
        sleep=lambda seconds: None,
    )
    summary = backfill_daily(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=START,
        end=END,
        sleep=lambda seconds: None,
    )
    assert summary.skipped == 4
    assert summary.loaded == 0
    assert summary.failed == [BROKEN_DAY.isoformat()]

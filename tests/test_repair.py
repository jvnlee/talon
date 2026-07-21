from datetime import date

import polars as pl
import pytest

from talon.data.store import ADJUST_FACTORS, DAILY_CANDLES, DAILY_SNAPSHOT_SCHEMA
from talon.errors import SourceError
from talon.ingest.repair import repair_daily_gaps

D1 = date(2026, 7, 6)
D2 = date(2026, 7, 7)


def daily_frame(day, rows):
    return pl.DataFrame(
        {
            "day": [day] * len(rows),
            "symbol": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[1] for r in rows],
            "close": [r[2] for r in rows],
            "volume": [r[3] for r in rows],
            "value": [0.0] * len(rows),
            "change_pct": [r[4] for r in rows],
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )


def official_for(day):
    rows = {
        D1: [("AAA", 100.0, 100.0, 1000.0, 0.0), ("BBB", None, 3760.0, 0.0, 14.81)],
        D2: [("AAA", 100.0, 100.0, 1000.0, 0.0), ("BBB", None, 3600.0, 0.0, -4.26)],
    }
    return daily_frame(day, rows[day])


def make_fetch(broken=()):
    def fetch(day):
        if day in broken:
            raise SourceError("marcap gap")
        return official_for(day), pl.DataFrame()

    return fetch


def adjusted_fetch(symbol, start, end):
    closes = {"BBB": [3760.0, 3600.0]}
    return pl.DataFrame(
        {"day": [D1, D2], "close": closes[symbol]},
        schema={"day": pl.Date(), "close": pl.Float64()},
    )


def seed_store(snapshots):
    for day in (D1, D2):
        snapshots.write_date(
            DAILY_CANDLES, day, daily_frame(day, [("AAA", 100.0, 100.0, 1000.0, 0.0)])
        )


def test_repair_inserts_missing_rows_and_rebuilds_factors(cfg, state, snapshots, series):
    seed_store(snapshots)
    progressed = []

    summary = repair_daily_gaps(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        fetch=make_fetch(),
        factor_fetch=adjusted_fetch,
        throttle=0.0,
        progress=lambda index, total, day: progressed.append((index, total)),
    )

    assert summary.status == "ok"
    assert summary.sessions == 2
    assert summary.repaired_days == 2
    assert summary.inserted_rows == 2
    assert summary.affected_symbols == ["BBB"]
    assert summary.uncovered == []
    assert progressed[-1] == (2, 2)

    stored = snapshots.read_date(DAILY_CANDLES, D1)
    assert stored.get_column("symbol").to_list() == ["AAA", "BBB"]
    inserted = stored.filter(pl.col("symbol") == "BBB").row(0, named=True)
    assert inserted["open"] is None
    assert inserted["close"] == 3760.0
    assert inserted["change_pct"] == 14.81

    assert summary.adjust is not None
    assert summary.adjust.computed == 1
    factors = series.read(ADJUST_FACTORS, "BBB")
    assert factors["day"].to_list() == [D1, D2]
    assert factors["factor"].to_list() == pytest.approx([1.0, 1.0])
    assert series.read(ADJUST_FACTORS, "AAA") is None
    assert state.recent_runs("repair-daily")[0].ok is True


def test_repair_is_idempotent(cfg, state, snapshots, series):
    seed_store(snapshots)
    first = repair_daily_gaps(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        fetch=make_fetch(),
        factor_fetch=adjusted_fetch,
        throttle=0.0,
    )
    assert first.inserted_rows == 2

    second = repair_daily_gaps(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        fetch=make_fetch(),
        factor_fetch=adjusted_fetch,
        throttle=0.0,
    )

    assert second.status == "ok"
    assert second.inserted_rows == 0
    assert second.repaired_days == 0
    assert second.affected_symbols == []
    assert second.adjust is None


def test_repair_records_uncovered_days_and_still_repairs_the_rest(cfg, state, snapshots, series):
    seed_store(snapshots)

    summary = repair_daily_gaps(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        fetch=make_fetch(broken={D2}),
        factor_fetch=adjusted_fetch,
        throttle=0.0,
    )

    assert summary.status == "partial"
    assert summary.uncovered == [D2.isoformat()]
    assert summary.repaired_days == 1
    assert summary.inserted_rows == 1
    assert summary.affected_symbols == ["BBB"]
    assert snapshots.read_date(DAILY_CANDLES, D1).height == 2
    assert snapshots.read_date(DAILY_CANDLES, D2).height == 1
    assert state.recent_runs("repair-daily")[0].ok is False


def test_repair_without_rebuild_skips_factor_build(cfg, state, snapshots, series):
    seed_store(snapshots)

    summary = repair_daily_gaps(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        fetch=make_fetch(),
        rebuild_factors=False,
        throttle=0.0,
    )

    assert summary.inserted_rows == 2
    assert summary.adjust is None
    assert series.read(ADJUST_FACTORS, "BBB") is None


def test_repair_respects_date_window(cfg, state, snapshots, series):
    seed_store(snapshots)

    summary = repair_daily_gaps(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        fetch=make_fetch(),
        rebuild_factors=False,
        start=D2,
        throttle=0.0,
    )

    assert summary.sessions == 1
    assert summary.inserted_rows == 1
    assert snapshots.read_date(DAILY_CANDLES, D1).height == 1
    assert snapshots.read_date(DAILY_CANDLES, D2).height == 2

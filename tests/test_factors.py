from datetime import date, timedelta

import polars as pl
import pytest

from talon.data.adjust import FACTOR_SCHEMA
from talon.data.store import ADJUST_FACTORS, ADJUST_MANIFEST, DAILY_CANDLES, DAILY_SNAPSHOT_SCHEMA
from talon.errors import SchemaDriftError, SourceError
from talon.ingest.factors import MANIFEST_NAME, build_factors, rebase_factors
from talon.sources.fdr_daily import HISTORY_SCHEMA

BASE = date(2018, 4, 30)


def days(count, start=BASE):
    return [start + timedelta(days=i) for i in range(count)]


def snapshot_frame(day, rows):
    closes = [float(row[1]) for row in rows]
    return pl.DataFrame(
        {
            "day": [day] * len(rows),
            "symbol": [row[0] for row in rows],
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * len(rows),
            "value": [1.0] * len(rows),
            "change_pct": [float(row[2]) if len(row) > 2 else 0.0 for row in rows],
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )


def history(closes, start=BASE):
    values = [float(c) for c in closes]
    return pl.DataFrame(
        {
            "day": days(len(closes), start),
            "open": values,
            "high": values,
            "low": values,
            "close": values,
            "volume": [1.0] * len(closes),
        },
        schema=HISTORY_SCHEMA,
    )


class FakeFetch:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        result = self.responses[symbol]
        if isinstance(result, Exception):
            raise result
        return result


def write_snapshots(snapshots, per_day_rows):
    for day, rows in per_day_rows.items():
        snapshots.write_date(DAILY_CANDLES, day, snapshot_frame(day, rows))


def two_symbol_snapshots(snapshots):
    d = days(3)
    write_snapshots(
        snapshots,
        {
            d[0]: [("SPLIT", 2_650_000), ("FLAT", 1000)],
            d[1]: [("SPLIT", 53_000), ("FLAT", 1010)],
            d[2]: [("SPLIT", 53_900), ("FLAT", 1020)],
        },
    )


def test_build_writes_factors_and_manifest(cfg, state, snapshots, series):
    two_symbol_snapshots(snapshots)
    fetch = FakeFetch(
        {
            "SPLIT": history([53_000, 53_000, 53_900]),
            "FLAT": history([1000, 1010, 1020]),
        }
    )

    summary = build_factors(
        cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, throttle=0
    )

    assert summary.status == "ok"
    assert summary.symbols == 2
    assert summary.computed == 2
    assert fetch.calls == [
        ("FLAT", BASE, BASE + timedelta(days=2)),
        ("SPLIT", BASE, BASE + timedelta(days=2)),
    ]
    split_factors = series.read(ADJUST_FACTORS, "SPLIT")
    assert split_factors["factor"].to_list() == pytest.approx([0.02, 1.0, 1.0])
    flat_factors = series.read(ADJUST_FACTORS, "FLAT")
    assert flat_factors["factor"].to_list() == pytest.approx([1.0, 1.0, 1.0])
    manifest = series.read(ADJUST_MANIFEST, MANIFEST_NAME)
    assert manifest.height == 2
    assert set(manifest["status"].to_list()) == {"ok"}
    assert manifest.filter(pl.col("symbol") == "SPLIT")["factor_days"][0] == 3
    assert state.recent_runs("adjust-build")[0].ok is True


def test_rerun_skips_fresh_symbols(cfg, state, snapshots, series):
    two_symbol_snapshots(snapshots)
    fetch = FakeFetch(
        {
            "SPLIT": history([53_000, 53_000, 53_900]),
            "FLAT": history([1000, 1010, 1020]),
        }
    )
    build_factors(cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, throttle=0)
    fetch.calls.clear()

    summary = build_factors(
        cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, throttle=0
    )

    assert summary.skipped == 2
    assert summary.computed == 0
    assert fetch.calls == []


def test_new_raw_day_refreshes_only_stale_symbol(cfg, state, snapshots, series):
    two_symbol_snapshots(snapshots)
    fetch = FakeFetch(
        {
            "SPLIT": history([53_000, 53_000, 53_900]),
            "FLAT": history([1000, 1010, 1020]),
        }
    )
    build_factors(cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, throttle=0)
    new_day = BASE + timedelta(days=3)
    write_snapshots(snapshots, {new_day: [("FLAT", 1030)]})
    fetch.responses["FLAT"] = history([1000, 1010, 1020, 1030])
    fetch.calls.clear()

    summary = build_factors(
        cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, throttle=0
    )

    assert summary.computed == 1
    assert summary.skipped == 1
    assert fetch.calls == [("FLAT", BASE, new_day)]
    assert series.read(ADJUST_FACTORS, "FLAT").height == 4


def reduction_snapshots(snapshots):
    d = days(3)
    write_snapshots(
        snapshots,
        {
            d[0]: [("GAMJA", 293, 0.0), ("FLAT", 1000)],
            d[1]: [("GAMJA", 293, 0.0), ("FLAT", 1010)],
            d[2]: [("GAMJA", 4530, -10.65), ("FLAT", 1020)],
        },
    )
    return (4530 / (1 - 0.1065)) / 293


def test_build_bridges_missed_capital_reduction(cfg, state, snapshots, series):
    ratio = reduction_snapshots(snapshots)
    fetch = FakeFetch(
        {
            "GAMJA": history([293, 293, 4530]),
            "FLAT": history([1000, 1010, 1020]),
        }
    )

    summary = build_factors(
        cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, throttle=0
    )

    assert summary.status == "ok"
    assert summary.rebased == ["GAMJA"]
    factors = series.read(ADJUST_FACTORS, "GAMJA")
    assert factors["factor"].to_list() == pytest.approx([ratio, ratio, 1.0])
    assert series.read(ADJUST_FACTORS, "FLAT")["factor"].to_list() == pytest.approx([1.0] * 3)


def flat_factor_frame(count):
    return pl.DataFrame(
        {"day": days(count), "factor": [1.0] * count},
        schema=FACTOR_SCHEMA,
    )


def test_rebase_repairs_stored_factors_without_fetching(cfg, state, snapshots, series):
    ratio = reduction_snapshots(snapshots)
    series.replace(ADJUST_FACTORS, "GAMJA", flat_factor_frame(3))
    series.replace(ADJUST_FACTORS, "FLAT", flat_factor_frame(3))

    summary = rebase_factors(cfg, state=state, snapshots=snapshots, series=series)

    assert summary.status == "ok"
    assert summary.computed == 1
    assert summary.skipped == 1
    assert summary.rebased == ["GAMJA"]
    factors = series.read(ADJUST_FACTORS, "GAMJA")
    assert factors["factor"].to_list() == pytest.approx([ratio, ratio, 1.0])
    assert state.recent_runs("adjust-rebase")[0].ok is True

    again = rebase_factors(cfg, state=state, snapshots=snapshots, series=series)
    assert again.computed == 0
    assert again.skipped == 2


def test_rebase_flags_symbols_without_inputs(cfg, state, snapshots, series):
    reduction_snapshots(snapshots)
    series.replace(ADJUST_FACTORS, "FLAT", flat_factor_frame(3))

    summary = rebase_factors(
        cfg, state=state, snapshots=snapshots, series=series, symbols=["FLAT", "ZZZZ"]
    )

    assert summary.status == "partial"
    assert summary.failed == ["ZZZZ"]
    assert summary.skipped == 1
    assert state.recent_runs("adjust-rebase")[0].ok is False


def test_empty_adjusted_is_recorded_and_retried(cfg, state, snapshots, series):
    write_snapshots(snapshots, {BASE: [("GONE", 500)]})
    fetch = FakeFetch({"GONE": pl.DataFrame(schema=HISTORY_SCHEMA)})

    summary = build_factors(
        cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, throttle=0
    )

    assert summary.status == "ok"
    assert summary.empty == ["GONE"]
    assert series.read(ADJUST_FACTORS, "GONE") is None
    manifest = series.read(ADJUST_MANIFEST, MANIFEST_NAME)
    assert manifest["status"].to_list() == ["empty"]

    build_factors(cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, throttle=0)
    assert len(fetch.calls) == 2


def test_source_error_marks_partial_and_isolates_failure(cfg, state, snapshots, series, alerter):
    two_symbol_snapshots(snapshots)
    fetch = FakeFetch(
        {
            "SPLIT": SourceError("naver down"),
            "FLAT": history([1000, 1010, 1020]),
        }
    )

    summary = build_factors(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        alerter=alerter,
        fetch=fetch,
        throttle=0,
    )

    assert summary.status == "partial"
    assert summary.failed == ["SPLIT"]
    assert summary.computed == 1
    assert series.read(ADJUST_FACTORS, "SPLIT") is None
    assert series.read(ADJUST_FACTORS, "FLAT") is not None
    manifest = series.read(ADJUST_MANIFEST, MANIFEST_NAME)
    assert manifest.filter(pl.col("symbol") == "SPLIT")["status"][0] == "failed"
    assert state.recent_runs("adjust-build")[0].ok is False


def test_failure_alerts_and_records_heartbeat(cfg, state, snapshots, series, alerter, notifier):
    """무인 스케줄 잡이므로 실패가 조용하면 안 된다."""
    two_symbol_snapshots(snapshots)
    fetch = FakeFetch(
        {
            "SPLIT": SourceError("naver down"),
            "FLAT": history([1000, 1010, 1020]),
        }
    )

    build_factors(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        alerter=alerter,
        fetch=fetch,
        throttle=0,
    )

    assert state.get_heartbeat("adjust-build").ok is False
    assert any("수정계수 산출 실패" in text for text in notifier.sent)


def test_success_records_heartbeat_without_alerting(
    cfg, state, snapshots, series, alerter, notifier
):
    two_symbol_snapshots(snapshots)
    fetch = FakeFetch(
        {
            "SPLIT": history([53_000, 53_000, 53_900]),
            "FLAT": history([1000, 1010, 1020]),
        }
    )

    build_factors(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        alerter=alerter,
        fetch=fetch,
        throttle=0,
    )

    assert state.get_heartbeat("adjust-build").ok is True
    assert notifier.sent == []


def test_schema_drift_aborts_run(cfg, state, snapshots, series):
    write_snapshots(snapshots, {BASE: [("A", 100)]})
    fetch = FakeFetch({"A": SchemaDriftError("columns changed")})

    with pytest.raises(SchemaDriftError):
        build_factors(cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, throttle=0)


def test_symbol_subset_and_unknown_symbol(cfg, state, snapshots, series):
    two_symbol_snapshots(snapshots)
    fetch = FakeFetch({"FLAT": history([1000, 1010, 1020])})

    summary = build_factors(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        fetch=fetch,
        symbols=["FLAT", "ZZZZ"],
        throttle=0,
    )

    assert summary.symbols == 2
    assert summary.computed == 1
    assert summary.failed == ["ZZZZ"]
    assert [call[0] for call in fetch.calls] == ["FLAT"]


def test_force_recomputes_fresh_symbols(cfg, state, snapshots, series):
    two_symbol_snapshots(snapshots)
    fetch = FakeFetch(
        {
            "SPLIT": history([53_000, 53_000, 53_900]),
            "FLAT": history([1000, 1010, 1020]),
        }
    )
    build_factors(cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, throttle=0)
    fetch.calls.clear()

    summary = build_factors(
        cfg, state=state, snapshots=snapshots, series=series, fetch=fetch, force=True, throttle=0
    )

    assert summary.computed == 2
    assert len(fetch.calls) == 2


def test_no_snapshots_returns_no_data(cfg, state, snapshots, series):
    summary = build_factors(cfg, state=state, snapshots=snapshots, series=series, throttle=0)

    assert summary.status == "no-data"


def test_throttle_sleeps_between_fetches(cfg, state, snapshots, series):
    two_symbol_snapshots(snapshots)
    fetch = FakeFetch(
        {
            "SPLIT": history([53_000, 53_000, 53_900]),
            "FLAT": history([1000, 1010, 1020]),
        }
    )
    naps = []

    build_factors(
        cfg,
        state=state,
        snapshots=snapshots,
        series=series,
        fetch=fetch,
        throttle=0.5,
        sleep=naps.append,
    )

    assert naps == [0.5, 0.5]

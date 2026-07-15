from datetime import date, timedelta

import polars as pl
import pytest

from talon.backtest.cohort import (
    H1_PIECES,
    H2_PIECES,
    diagnose_cohorts,
    signal_factors,
    signal_warmup,
    with_signals,
)
from talon.backtest.lookahead import pick_cuts, verify_factors

BASE = date(2020, 1, 6)


def d(i: int) -> date:
    return BASE + timedelta(days=i)


def build_panel(rows: list[dict]) -> pl.DataFrame:
    return (
        pl.DataFrame(rows)
        .sort("symbol", "day")
        .with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))
        .sort("day", "symbol")
    )


def bar(
    day: date,
    symbol: str,
    *,
    open_: float,
    close: float,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1000.0,
    value: float = 5_000_000_000.0,
    cap: float = 1.0,
    tradable: bool = True,
) -> dict:
    return {
        "day": day,
        "symbol": symbol,
        "open": float(open_),
        "high": float(high if high is not None else max(open_, close)),
        "low": float(low if low is not None else min(open_, close)),
        "close": float(close),
        "volume": float(volume),
        "value": float(value),
        "cap": float(cap),
        "tradable_stock": bool(tradable),
    }


def flat(day: date, symbol: str, price: float = 100.0, **kwargs) -> dict:
    return bar(day, symbol, open_=price, close=price, high=price, low=price, **kwargs)


def h2_firing_panel() -> pl.DataFrame:
    rows = [flat(d(i), "AAA") for i in range(10)]
    rows.append(bar(d(10), "AAA", open_=101.0, close=112.0, high=112.0, low=100.0))
    rows.append(bar(d(11), "AAA", open_=112.0, close=113.0, high=113.0, low=112.0))
    rows.append(bar(d(12), "AAA", open_=114.0, close=146.0, high=146.0, low=114.0))
    rows.append(bar(d(13), "AAA", open_=146.0, close=147.0, high=147.0, low=146.0))
    rows.append(bar(d(14), "AAA", open_=147.0, close=148.0, high=148.0, low=147.0))
    return build_panel(rows)


def test_signal_factor_expressions_are_verbatim():
    factors = signal_factors()
    assert tuple(factors[name] for name in factors if name.startswith("h1_")) == H1_PIECES
    assert tuple(factors[name] for name in factors if name.startswith("h2_")) == H2_PIECES
    assert signal_warmup() == 250


def test_baseline_gap_stats_and_symbol_boundary():
    rows = []
    for i, open_next in enumerate([100.0, 95.0, 98.0, 100.0, 103.0]):
        rows.append(bar(d(i), "AAA", open_=open_next, close=100.0))
    for i, open_next in enumerate([100.0, 105.0, 102.0, 100.0, 97.0]):
        rows.append(bar(d(i), "BBB", open_=open_next, close=100.0))
    panel = build_panel(rows)

    report = diagnose_cohorts(panel, universe_size=10, min_value=0.0)

    baseline = report.baseline
    assert baseline.n == 8
    assert baseline.mean_pct == pytest.approx(0.0)
    assert baseline.median_pct == pytest.approx(0.0)
    assert baseline.std_pct == pytest.approx((76 / 7) ** 0.5)
    assert baseline.win_rate_pct == pytest.approx(37.5)
    assert baseline.p10_pct <= baseline.median_pct <= baseline.p90_pct


def test_halt_gap_beyond_seven_calendar_days_is_dropped_and_counted():
    rows = [bar(d(i), "AAA", open_=100.0, close=100.0) for i in range(3)]
    rows.append(bar(d(10), "AAA", open_=100.0, close=100.0))
    rows += [bar(d(i), "BBB", open_=100.0, close=100.0) for i in range(3)]
    panel = build_panel(rows)

    report = diagnose_cohorts(panel, universe_size=10, min_value=0.0)

    assert report.baseline.n == 4
    assert report.halt_excluded == 1


def test_market_cap_terciles_group_ties_into_the_lowest_bucket():
    caps = [10.0, 10.0, 10.0, 40.0, 50.0, 60.0]
    rows = []
    for symbol, cap in zip("STUVWX", caps, strict=True):
        rows.append(bar(d(0), symbol, open_=100.0, close=100.0, cap=cap))
        rows.append(bar(d(1), symbol, open_=100.0, close=100.0, cap=cap))
    panel = build_panel(rows)

    report = diagnose_cohorts(panel, universe_size=100, min_value=0.0)
    buckets = {
        row.label: row.stats.n for row in report.rows if row.label.startswith("baseline_cap")
    }

    assert buckets == {"baseline_cap1": 3, "baseline_cap2": 1, "baseline_cap3": 2}


def test_limit_up_signal_day_is_excluded_and_counted():
    panel = h2_firing_panel()

    report = diagnose_cohorts(panel, universe_size=10, min_value=0.0)
    h2_row = next(row for row in report.rows if row.label == "h2")

    assert report.limit_up_excluded["h2"] == 1
    assert h2_row.stats.n == 2
    assert h2_row.verdict == "보류"


def test_baseline_tercile_rows_are_context_only():
    panel = h2_firing_panel()

    report = diagnose_cohorts(panel, universe_size=10, min_value=0.0)

    for row in report.rows:
        if row.label.startswith("baseline_cap"):
            assert row.verdict == "문맥"
            assert row.baseline_label == "baseline"
        elif row.label.startswith("h1_cap") or row.label.startswith("h2_cap"):
            assert row.baseline_label == f"baseline_cap{row.tercile}"


def test_report_has_eleven_rows_in_declared_order():
    panel = h2_firing_panel()

    report = diagnose_cohorts(panel, universe_size=10, min_value=0.0)

    assert [row.label for row in report.rows] == [
        "h1",
        "h2",
        "h1_cap1",
        "h1_cap2",
        "h1_cap3",
        "h2_cap1",
        "h2_cap2",
        "h2_cap3",
        "baseline_cap1",
        "baseline_cap2",
        "baseline_cap3",
    ]


def test_signals_are_truncation_invariant():
    panel = h2_firing_panel()
    cuts = pick_cuts(panel["day"].to_list(), 3)

    assert verify_factors(panel, signal_factors(), cuts) == []

    full = with_signals(panel).sort("day", "symbol")
    for cut in cuts:
        truncated = with_signals(panel.filter(pl.col("day") <= cut)).sort("day", "symbol")
        full_at_cut = full.filter(pl.col("day") == cut).select("h1", "h2")
        trunc_at_cut = truncated.filter(pl.col("day") == cut).select("h1", "h2")
        assert full_at_cut.equals(trunc_at_cut)


def test_warmup_nulls_never_leak_as_a_true_signal():
    rows = [flat(d(i), "AAA", price=100.0 + i) for i in range(8)]
    panel = build_panel(rows)

    frame = with_signals(panel)

    for column in ("h1", "h2"):
        assert frame[column].null_count() == 0
        assert not frame[column].any()


def test_signal_fires_only_after_its_window_is_warm():
    panel = h2_firing_panel()

    frame = with_signals(panel).sort("day")
    warm = frame.filter(pl.col("h2"))["day"].to_list()

    assert warm
    assert min(warm) >= d(11)

from datetime import date, timedelta

import polars as pl
import pytest

from talon.backtest.fidelity import (
    approximate_panel,
    daily_selections,
    measure_fidelity,
    selection_overlap,
)
from talon.quant.signals import StrategySpec

BASE = date(2026, 4, 15)


def d(i):
    return BASE + timedelta(days=i)


def probe_spec():
    return StrategySpec(
        name="probe",
        entry=("close_1510 / prev_close - 1 >= 0.02",),
        score="close_1510 / prev_close - 1",
        stop="close_1510 * 0.95",
        target=None,
        ref_price="close_1510",
        execution="close_overnight",
    )


def row(
    i,
    symbol,
    close,
    close_1510=None,
    exact=True,
    value=2_000_000_000.0,
    volume=1000.0,
    tradable=True,
):
    close_1510 = close_1510 if close_1510 is not None else close
    return {
        "day": d(i),
        "symbol": symbol,
        "open": float(close),
        "high": float(max(close, close_1510)),
        "low": float(min(close, close_1510)),
        "close": float(close),
        "volume": float(volume),
        "value": float(value),
        "close_1510": float(close_1510),
        "high_1510": float(close_1510),
        "low_1510": float(close_1510),
        "volume_1510": float(volume),
        "intraday_exact": bool(exact),
        "option_expiry": False,
        "tradable_stock": bool(tradable),
    }


def build_panel(rows):
    return (
        pl.DataFrame(rows)
        .sort("symbol", "day")
        .with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))
        .sort("day", "symbol")
    )


def test_approximate_panel_erases_the_1510_state():
    panel = build_panel([row(0, "AAA", 100.0, close_1510=97.0)])
    approx = approximate_panel(panel)

    first = approx.row(0, named=True)
    assert first["close_1510"] == 100.0
    assert first["high_1510"] == first["high"]
    assert first["intraday_exact"] is False


def test_daily_selections_rank_by_score_and_cap():
    rows = [
        row(0, "AAA", 100.0),
        row(0, "BBB", 100.0),
        row(0, "CCC", 100.0),
        row(0, "DDD", 100.0),
        row(1, "AAA", 105.0),
        row(1, "BBB", 104.0),
        row(1, "CCC", 103.0),
        row(1, "DDD", 101.0),
    ]
    panel = build_panel(rows)

    picks = daily_selections(panel, probe_spec(), top_n=2, universe_size=10, min_value=0.0)

    assert picks.filter(pl.col("day") == d(1)).get_column("symbol").to_list() == ["AAA", "BBB"]


def test_daily_selections_respect_universe():
    rows = [
        row(0, "AAA", 100.0),
        row(0, "BBB", 100.0),
        row(1, "AAA", 105.0, value=1.0),
        row(1, "BBB", 104.0),
    ]
    panel = build_panel(rows)

    picks = daily_selections(panel, probe_spec(), top_n=3, universe_size=10, min_value=100.0)

    assert picks.get_column("symbol").to_list() == ["BBB"]


def test_selection_overlap_scores_jaccard_per_day():
    exact = pl.DataFrame({"day": [d(1), d(1), d(3)], "symbol": ["AAA", "BBB", "AAA"]})
    approx = pl.DataFrame({"day": [d(1), d(1), d(3)], "symbol": ["BBB", "CCC", "AAA"]})

    overlap = selection_overlap(exact, approx, [d(1), d(2), d(3)])

    assert overlap.days == 3
    assert overlap.active_days == 2
    assert overlap.mean_jaccard == pytest.approx((1 / 3 + 1.0) / 2)
    assert overlap.exact_picks == 3
    assert overlap.approx_picks == 3
    assert overlap.common_picks == 2


def test_measure_fidelity_sees_divergent_selection():
    rows = []
    for i in range(4):
        aaa_1510 = 101.0 if i == 2 else None
        rows.append(row(i, "AAA", 100.0 if i != 2 else 108.0, close_1510=aaa_1510))
        rows.append(row(i, "BBB", 100.0 if i != 2 else 103.0))
    panel = build_panel(rows)

    report = measure_fidelity(
        panel, {"probe": probe_spec()}, universe_size=10, min_value=0.0
    )

    assert report.exact_days == 4
    assert report.settled_days == 0
    overlap = report.overlaps["probe"]
    assert overlap.active_days == 1
    assert overlap.mean_jaccard == pytest.approx(0.5)
    assert overlap.exact_picks == 1
    assert overlap.approx_picks == 2
    assert overlap.common_picks == 1
    assert report.price_error_abs_pct["max"] > 0
    assert report.volume_ratio["p50"] == pytest.approx(1.0)


def test_measure_fidelity_requires_exact_rows():
    panel = build_panel([row(0, "AAA", 100.0, exact=False)])

    with pytest.raises(ValueError, match="정확한 15:10"):
        measure_fidelity(panel, {"probe": probe_spec()}, universe_size=10, min_value=0.0)

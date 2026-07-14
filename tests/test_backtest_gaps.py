from datetime import date, timedelta

import polars as pl
import pytest

from talon.backtest.gaps import overnight_gap_stats

BASE = date(2023, 3, 6)


def d(i):
    return BASE + timedelta(days=i)


def rows_for(symbol, closes, opens, value=1_000_000_000.0, volume=1000.0, tradable=True):
    rows = []
    for index, (close, open_) in enumerate(zip(closes, opens, strict=True)):
        rows.append(
            {
                "day": d(index),
                "symbol": symbol,
                "open": float(open_),
                "close": float(close),
                "volume": float(volume),
                "value": float(value),
                "tradable_stock": bool(tradable),
            }
        )
    return rows


def build_panel(rows):
    return (
        pl.DataFrame(rows)
        .sort("symbol", "day")
        .with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))
        .sort("day", "symbol")
    )


def test_gap_distribution_from_known_gaps():
    closes = [100.0, 100.0, 100.0, 100.0, 100.0]
    opens = [100.0, 95.0, 98.0, 100.0, 103.0]
    panel = build_panel(rows_for("AAA", closes, opens))

    stats = overnight_gap_stats(panel, universe_size=10, min_value=0.0)

    assert stats.count == 4
    assert stats.mean_pct == pytest.approx((-5 - 2 + 0 + 3) / 4)
    assert stats.quantiles_pct["p50"] == pytest.approx(-1.0)
    assert stats.quantiles_pct["p1"] <= stats.quantiles_pct["p50"]


def test_gap_universe_excludes_untradable_thin_and_low_ranked():
    closes = [100.0, 100.0, 100.0]
    opens = [100.0, 95.0, 95.0]
    rows = rows_for("AAA", closes, opens, value=1000.0)
    rows += rows_for("BBB", closes, [100.0, 90.0, 90.0], value=900.0)
    rows += rows_for("DEAD", closes, opens, value=800.0, volume=0.0)
    rows += rows_for("SPACY", closes, opens, value=700.0, tradable=False)
    rows += rows_for("TINY", closes, [100.0, 50.0, 50.0], value=1.0)
    panel = build_panel(rows)

    stats = overnight_gap_stats(panel, universe_size=1, min_value=100.0)

    assert stats.count == 2
    assert stats.mean_pct == pytest.approx(-5.0)


def test_gap_strength_floor_conditions_on_the_up_day():
    closes = [100.0, 104.0, 104.0]
    opens = [100.0, 100.0, 101.0]
    panel = build_panel(rows_for("AAA", closes, opens))

    conditioned = overnight_gap_stats(
        panel, universe_size=10, min_value=0.0, strength_floor_pct=3.0
    )

    assert conditioned.count == 1
    assert conditioned.mean_pct == pytest.approx((101.0 / 104.0 - 1) * 100)


def test_gap_stats_empty_population():
    panel = build_panel(rows_for("AAA", [100.0], [100.0]))
    stats = overnight_gap_stats(panel, universe_size=10, min_value=0.0)

    assert stats.count == 0
    assert stats.quantiles_pct == {}

from datetime import date

import polars as pl
import pytest

from talon.backtest.data import MarketView, load_panel
from talon.data.adjust import FACTOR_SCHEMA
from talon.data.store import ADJUST_FACTORS, DAILY_CANDLES, DAILY_SNAPSHOT_SCHEMA

D0 = date(2018, 5, 2)
D1 = date(2018, 5, 3)


def snapshot_frame(day, rows):
    return pl.DataFrame(
        {
            "day": [day] * len(rows),
            "symbol": [r["symbol"] for r in rows],
            "open": [float(r["open"]) for r in rows],
            "high": [float(r.get("high", r["open"])) for r in rows],
            "low": [float(r.get("low", r["open"])) for r in rows],
            "close": [float(r["close"]) for r in rows],
            "volume": [float(r.get("volume", 100)) for r in rows],
            "value": [float(r.get("value", 1_000_000)) for r in rows],
            "change_pct": [0.0] * len(rows),
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )


def factor_frame(pairs):
    return pl.DataFrame(
        {"day": [p[0] for p in pairs], "factor": [float(p[1]) for p in pairs]},
        schema=FACTOR_SCHEMA,
    )


@pytest.fixture
def split_data(snapshots, series):
    snapshots.write_date(
        DAILY_CANDLES,
        D0,
        snapshot_frame(
            D0,
            [
                {"symbol": "SPLIT", "open": 2_600_000, "close": 2_650_000, "volume": 100},
                {"symbol": "FLAT", "open": 1000, "close": 1000},
            ],
        ),
    )
    snapshots.write_date(
        DAILY_CANDLES,
        D1,
        snapshot_frame(
            D1,
            [
                {"symbol": "SPLIT", "open": 53_000, "close": 53_900, "volume": 5000},
                {"symbol": "FLAT", "open": 1000, "close": 1010},
            ],
        ),
    )
    series.replace(ADJUST_FACTORS, "SPLIT", factor_frame([(D0, 0.02), (D1, 1.0)]))
    series.replace(ADJUST_FACTORS, "FLAT", factor_frame([(D0, 1.0), (D1, 1.0)]))


def test_panel_adjusts_prices_and_keeps_raw(snapshots, series, split_data):
    panel = load_panel(snapshots, series)

    split_d0 = panel.filter((pl.col("symbol") == "SPLIT") & (pl.col("day") == D0)).row(
        0, named=True
    )
    assert split_d0["close"] == pytest.approx(53_000.0)
    assert split_d0["open"] == pytest.approx(52_000.0)
    assert split_d0["raw_close"] == pytest.approx(2_650_000.0)
    assert split_d0["volume"] == pytest.approx(5_000.0)
    assert split_d0["value"] == pytest.approx(1_000_000.0)
    assert split_d0["prev_close"] is None

    split_d1 = panel.filter((pl.col("symbol") == "SPLIT") & (pl.col("day") == D1)).row(
        0, named=True
    )
    assert split_d1["prev_close"] == pytest.approx(53_000.0)
    assert split_d1["close"] == pytest.approx(53_900.0)


def test_panel_symbol_and_date_filters(snapshots, series, split_data):
    only_flat = load_panel(snapshots, series, symbols=["FLAT"])
    assert only_flat["symbol"].unique().to_list() == ["FLAT"]

    windowed = load_panel(snapshots, series, start=D1)
    assert windowed["day"].unique().to_list() == [D1]
    flat_row = windowed.filter(pl.col("symbol") == "FLAT").row(0, named=True)
    assert flat_row["prev_close"] == pytest.approx(1000.0)


def test_panel_drops_symbols_without_factors(snapshots, series, split_data):
    snapshots.write_date(
        DAILY_CANDLES,
        date(2018, 5, 4),
        snapshot_frame(date(2018, 5, 4), [{"symbol": "NOFAC", "open": 10, "close": 10}]),
    )
    panel = load_panel(snapshots, series)
    assert "NOFAC" not in panel["symbol"].unique().to_list()


def test_panel_requires_snapshots_and_factors(snapshots, series):
    with pytest.raises(ValueError, match="일봉"):
        load_panel(snapshots, series)
    snapshots.write_date(
        DAILY_CANDLES, D0, snapshot_frame(D0, [{"symbol": "AAA", "open": 10, "close": 10}])
    )
    with pytest.raises(ValueError, match="수정계수"):
        load_panel(snapshots, series)


def test_market_view_respects_cutoff(snapshots, series, split_data):
    panel = load_panel(snapshots, series)
    view = MarketView(panel, D0)

    history = view.history("SPLIT")
    assert history["day"].max() == D0
    assert view.cross_section()["day"].unique().to_list() == [D0]

    tail = MarketView(panel, D1).history("SPLIT", days=1)
    assert tail["day"].to_list() == [D1]

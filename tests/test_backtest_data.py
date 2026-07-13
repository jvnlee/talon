from datetime import date

import polars as pl
import pytest

from conftest import stock_info_frame, write_stock_info
from talon.backtest.data import MarketView, load_panel
from talon.data.adjust import FACTOR_SCHEMA
from talon.data.store import ADJUST_FACTORS, DAILY_CANDLES, DAILY_SNAPSHOT_SCHEMA, STOCK_INFO

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
    write_stock_info(snapshots, [D0, D1], ["SPLIT", "FLAT"])


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
    day = date(2018, 5, 4)
    snapshots.write_date(
        DAILY_CANDLES,
        day,
        snapshot_frame(day, [{"symbol": "NOFAC", "open": 10, "close": 10}]),
    )
    write_stock_info(snapshots, [day], ["NOFAC"])
    panel = load_panel(snapshots, series)
    assert "NOFAC" not in panel["symbol"].unique().to_list()


def test_panel_requires_snapshots_factors_and_stock_info(snapshots, series):
    with pytest.raises(ValueError, match="일봉"):
        load_panel(snapshots, series)
    snapshots.write_date(
        DAILY_CANDLES, D0, snapshot_frame(D0, [{"symbol": "AAA", "open": 10, "close": 10}])
    )
    with pytest.raises(ValueError, match="수정계수"):
        load_panel(snapshots, series)
    series.replace(ADJUST_FACTORS, "AAA", factor_frame([(D0, 1.0)]))
    with pytest.raises(ValueError, match="종목기본정보"):
        load_panel(snapshots, series)


def test_panel_carries_the_last_classification_forward(snapshots, series, split_data):
    newest = date(2018, 5, 4)
    snapshots.write_date(
        DAILY_CANDLES,
        newest,
        snapshot_frame(newest, [{"symbol": "FLAT", "open": 10, "close": 10}]),
    )
    series.replace(ADJUST_FACTORS, "FLAT", factor_frame([(D0, 1.0), (D1, 1.0), (newest, 1.0)]))

    panel = load_panel(snapshots, series)

    row = panel.filter((pl.col("symbol") == "FLAT") & (pl.col("day") == newest)).row(0, named=True)
    assert row["tradable_stock"] is True


def test_panel_refuses_classification_staler_than_allowed(snapshots, series, split_data):
    newest = date(2018, 6, 20)
    snapshots.write_date(
        DAILY_CANDLES,
        newest,
        snapshot_frame(newest, [{"symbol": "FLAT", "open": 10, "close": 10}]),
    )
    series.replace(ADJUST_FACTORS, "FLAT", factor_frame([(D0, 1.0), (D1, 1.0), (newest, 1.0)]))

    with pytest.raises(ValueError, match="낡았습니다"):
        load_panel(snapshots, series, max_info_stale_days=10)


def test_panel_refuses_days_before_any_classification(snapshots, series, split_data):
    early = date(2018, 4, 2)
    snapshots.write_date(
        DAILY_CANDLES,
        early,
        snapshot_frame(early, [{"symbol": "FLAT", "open": 10, "close": 10}]),
    )
    series.replace(ADJUST_FACTORS, "FLAT", factor_frame([(early, 1.0), (D0, 1.0), (D1, 1.0)]))

    with pytest.raises(ValueError, match="2018-04-02 이전 종목기본정보가 없습니다"):
        load_panel(snapshots, series)


def test_carried_forward_classification_never_reads_the_future(snapshots, series, split_data):
    snapshots.write_date(
        STOCK_INFO, D1, stock_info_frame(D1, ["SPLIT", "FLAT"], section="관리종목(소속부없음)")
    )

    panel = load_panel(snapshots, series)

    before = panel.filter((pl.col("symbol") == "FLAT") & (pl.col("day") == D0)).row(0, named=True)
    after = panel.filter((pl.col("symbol") == "FLAT") & (pl.col("day") == D1)).row(0, named=True)
    assert before["tradable_stock"] is True
    assert after["tradable_stock"] is False


def test_panel_marks_non_common_stocks_as_untradable(snapshots, series, split_data):
    write_stock_info(snapshots, [D0, D1], ["SPLIT"], security_group="부동산투자회사")
    panel = load_panel(snapshots, series)

    split_row = panel.filter((pl.col("symbol") == "SPLIT") & (pl.col("day") == D0)).row(
        0, named=True
    )
    assert split_row["tradable_stock"] is False
    flat_row = panel.filter((pl.col("symbol") == "FLAT") & (pl.col("day") == D0)).row(0, named=True)
    assert flat_row["tradable_stock"] is False


def test_panel_marks_symbols_absent_from_stock_info_as_untradable(snapshots, series, split_data):
    write_stock_info(snapshots, [D0, D1], ["FLAT"])
    panel = load_panel(snapshots, series)

    split_row = panel.filter((pl.col("symbol") == "SPLIT") & (pl.col("day") == D0)).row(
        0, named=True
    )
    assert split_row["tradable_stock"] is False
    flat_row = panel.filter((pl.col("symbol") == "FLAT") & (pl.col("day") == D0)).row(0, named=True)
    assert flat_row["tradable_stock"] is True


def test_market_view_respects_cutoff(snapshots, series, split_data):
    panel = load_panel(snapshots, series)
    view = MarketView(panel, D0)

    history = view.history("SPLIT")
    assert history["day"].max() == D0
    assert view.cross_section()["day"].unique().to_list() == [D0]

    tail = MarketView(panel, D1).history("SPLIT", days=1)
    assert tail["day"].to_list() == [D1]

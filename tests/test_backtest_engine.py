from datetime import date, timedelta

import polars as pl
import pytest

from talon.backtest.costs import KrCostModel
from talon.backtest.engine import EngineConfig, Order, run_backtest

BASE = date(2026, 1, 5)

NO_SLIP = EngineConfig(slippage_pct=0.0)


def d(i):
    return BASE + timedelta(days=i)


def bar(day, symbol, open_, close=None, high=None, low=None, volume=1e9, factor=1.0):
    close = close if close is not None else open_
    high = high if high is not None else max(open_, close)
    low = low if low is not None else min(open_, close)
    return {
        "day": day,
        "symbol": symbol,
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
        "value": float(close) * float(volume),
        "raw_close": float(close) / factor,
        "factor": float(factor),
    }


def build_panel(rows):
    return (
        pl.DataFrame(rows)
        .sort("symbol", "day")
        .with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))
        .sort("day", "symbol")
    )


class ZeroCost:
    def buy_fee(self, notional, day):
        return 0.0

    def sell_fee(self, notional, day):
        return 0.0


class ScriptedStrategy:
    def __init__(self, script=None):
        self.script = script or {}
        self.observations = []

    def decide(self, view, portfolio):
        self.observations.append((view, portfolio))
        return self.script.get(view.day, [])


def flat_series(symbol, days, price=100):
    return [bar(d(i), symbol, price) for i in days]


def test_buy_fills_next_open_with_slippage_and_fees():
    panel = build_panel(
        [bar(d(0), "AAA", 100), bar(d(1), "AAA", 100, close=110), bar(d(2), "AAA", 110)]
    )
    strategy = ScriptedStrategy({d(0): [Order("buy", "AAA", budget=1_000_000)]})

    result = run_backtest(panel, strategy, costs=KrCostModel())

    exec_price = 100 * 1.001
    shares = int(1_000_000 / exec_price)
    notional = shares * exec_price
    fee = notional * 0.00015
    day1 = result.equity.filter(pl.col("day") == d(1)).row(0, named=True)
    assert day1["cash"] == pytest.approx(10_000_000 - notional - fee)
    assert day1["position_value"] == pytest.approx(shares * 110)
    assert day1["equity"] == pytest.approx(10_000_000 - notional - fee + shares * 110)
    assert result.trades.is_empty()
    assert result.stats.open_positions == 1


def test_sell_next_open_applies_sell_tax():
    panel = build_panel([bar(d(i), "AAA", 100) for i in range(4)])
    strategy = ScriptedStrategy(
        {
            d(0): [Order("buy", "AAA", budget=1_000_000)],
            d(1): [Order("sell", "AAA")],
        }
    )

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=KrCostModel())

    trade = result.trades.row(0, named=True)
    assert trade["entry_day"] == d(1)
    assert trade["exit_day"] == d(2)
    assert trade["reason"] == "strategy"
    assert trade["entry_notional"] == pytest.approx(1_000_000.0)
    assert trade["exit_notional"] == pytest.approx(1_000_000.0)
    expected_fees = 1_000_000 * 0.00015 + 1_000_000 * (0.00015 + 0.0020)
    assert trade["fees"] == pytest.approx(expected_fees)
    assert trade["pnl"] == pytest.approx(-expected_fees)


def test_stop_gap_through_fills_at_open():
    panel = build_panel(
        [
            bar(d(0), "AAA", 100),
            bar(d(1), "AAA", 100, close=95, low=95),
            bar(d(2), "AAA", 80, close=82),
        ]
    )
    strategy = ScriptedStrategy({d(0): [Order("buy", "AAA", budget=1_000_000, stop=90.0)]})

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    trade = result.trades.row(0, named=True)
    assert trade["reason"] == "stop"
    assert trade["exit_day"] == d(2)
    assert trade["exit_price"] == pytest.approx(80.0)


def test_stop_intrabar_fills_at_stop_price():
    panel = build_panel(
        [
            bar(d(0), "AAA", 100),
            bar(d(1), "AAA", 100),
            bar(d(2), "AAA", 95, close=92, low=88),
        ]
    )
    strategy = ScriptedStrategy({d(0): [Order("buy", "AAA", budget=1_000_000, stop=90.0)]})

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    trade = result.trades.row(0, named=True)
    assert trade["reason"] == "stop"
    assert trade["exit_price"] == pytest.approx(90.0)


def test_stop_beats_target_when_both_hit():
    panel = build_panel(
        [
            bar(d(0), "AAA", 100),
            bar(d(1), "AAA", 100),
            bar(d(2), "AAA", 100, high=110, low=85, close=100),
        ]
    )
    strategy = ScriptedStrategy(
        {d(0): [Order("buy", "AAA", budget=1_000_000, stop=90.0, target=105.0)]}
    )

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    trade = result.trades.row(0, named=True)
    assert trade["reason"] == "stop"
    assert trade["exit_price"] == pytest.approx(90.0)


def test_target_intrabar_fills_at_target_price():
    panel = build_panel(
        [
            bar(d(0), "AAA", 100),
            bar(d(1), "AAA", 100),
            bar(d(2), "AAA", 100, high=108, low=95, close=104),
        ]
    )
    strategy = ScriptedStrategy(
        {d(0): [Order("buy", "AAA", budget=1_000_000, stop=90.0, target=105.0)]}
    )

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    trade = result.trades.row(0, named=True)
    assert trade["reason"] == "target"
    assert trade["exit_price"] == pytest.approx(105.0)


def test_target_gap_open_fills_at_open():
    panel = build_panel(
        [
            bar(d(0), "AAA", 100),
            bar(d(1), "AAA", 100),
            bar(d(2), "AAA", 107, close=106),
        ]
    )
    strategy = ScriptedStrategy({d(0): [Order("buy", "AAA", budget=1_000_000, target=105.0)]})

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    trade = result.trades.row(0, named=True)
    assert trade["reason"] == "target"
    assert trade["exit_price"] == pytest.approx(107.0)


def test_limit_up_open_rejects_buy():
    panel = build_panel(
        [bar(d(0), "AAA", 100), bar(d(1), "AAA", 100), bar(d(2), "AAA", 130, close=130)]
    )
    strategy = ScriptedStrategy({d(1): [Order("buy", "AAA", budget=1_000_000)]})

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    rejection = result.rejections.row(0, named=True)
    assert rejection == {"day": d(2), "symbol": "AAA", "kind": "buy", "reason": "limit-up"}
    assert result.stats.open_positions == 0


def test_limit_down_open_blocks_sells_and_stops():
    panel = build_panel(
        [
            bar(d(0), "AAA", 100),
            bar(d(1), "AAA", 100),
            bar(d(2), "AAA", 70, close=70, low=60),
            bar(d(3), "AAA", 72, close=75),
        ]
    )
    strategy = ScriptedStrategy(
        {
            d(0): [Order("buy", "AAA", budget=1_000_000, stop=98.0)],
            d(1): [Order("sell", "AAA")],
        }
    )

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    rejection = result.rejections.row(0, named=True)
    assert rejection["reason"] == "limit-down"
    assert rejection["day"] == d(2)
    trade = result.trades.row(0, named=True)
    assert trade["exit_day"] == d(3)
    assert trade["reason"] == "stop"
    assert trade["exit_price"] == pytest.approx(72.0)


def test_delisted_position_force_closed_at_last_close():
    rows = flat_series("AAA", range(5))
    rows += [
        bar(d(0), "BBB", 100),
        bar(d(1), "BBB", 100),
        bar(d(2), "BBB", 60, close=50),
    ]
    panel = build_panel(rows)
    strategy = ScriptedStrategy({d(0): [Order("buy", "BBB", budget=1_000_000)]})

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    trade = result.trades.row(0, named=True)
    assert trade["reason"] == "delist"
    assert trade["exit_day"] == d(2)
    assert trade["exit_price"] == pytest.approx(50.0)
    assert result.stats.open_positions == 0


def test_suspended_symbol_holds_mark_and_rejects_sell():
    rows = flat_series("AAA", range(5))
    rows += [
        bar(d(0), "BBB", 100),
        bar(d(1), "BBB", 100, close=100),
        bar(d(3), "BBB", 90, close=90),
        bar(d(4), "BBB", 90),
    ]
    panel = build_panel(rows)
    strategy = ScriptedStrategy(
        {
            d(0): [Order("buy", "BBB", budget=1_000_000)],
            d(1): [Order("sell", "BBB")],
            d(2): [Order("sell", "BBB")],
        }
    )

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    rejection = result.rejections.row(0, named=True)
    assert rejection == {"day": d(2), "symbol": "BBB", "kind": "sell", "reason": "no-bar"}
    day2 = result.equity.filter(pl.col("day") == d(2)).row(0, named=True)
    assert day2["position_value"] == pytest.approx(10_000 * 100.0)
    trade = result.trades.row(0, named=True)
    assert trade["exit_day"] == d(3)
    assert trade["exit_price"] == pytest.approx(90.0)


def test_same_day_stop_after_entry():
    panel = build_panel(
        [
            bar(d(0), "AAA", 100),
            bar(d(1), "AAA", 100, low=94, close=99),
            bar(d(2), "AAA", 99),
        ]
    )
    strategy = ScriptedStrategy({d(0): [Order("buy", "AAA", budget=1_000_000, stop=95.0)]})

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    trade = result.trades.row(0, named=True)
    assert trade["entry_day"] == d(1)
    assert trade["exit_day"] == d(1)
    assert trade["holding_days"] == 0
    assert trade["reason"] == "stop"
    assert trade["exit_price"] == pytest.approx(95.0)


def test_second_buy_merges_position():
    panel = build_panel(
        [
            bar(d(0), "AAA", 100),
            bar(d(1), "AAA", 100),
            bar(d(2), "AAA", 120, close=120),
            bar(d(3), "AAA", 120),
        ]
    )
    strategy = ScriptedStrategy(
        {
            d(0): [Order("buy", "AAA", budget=10_000)],
            d(1): [Order("buy", "AAA", budget=12_000)],
            d(2): [Order("sell", "AAA")],
        }
    )

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    assert result.rejections.is_empty()
    assert result.trades.height == 1
    trade = result.trades.row(0, named=True)
    assert trade["entry_day"] == d(1)
    assert trade["entry_price"] == pytest.approx(110.0)
    assert trade["entry_notional"] == pytest.approx(22_000.0)
    assert trade["exit_notional"] == pytest.approx(24_000.0)
    assert trade["pnl"] == pytest.approx(2_000.0)


def test_update_order_moves_stop():
    panel = build_panel(
        [
            bar(d(0), "AAA", 100),
            bar(d(1), "AAA", 100),
            bar(d(2), "AAA", 100, low=97, close=100),
            bar(d(3), "AAA", 100),
        ]
    )
    strategy = ScriptedStrategy(
        {
            d(0): [Order("buy", "AAA", budget=1_000_000, stop=90.0)],
            d(1): [Order("update", "AAA", stop=98.0)],
        }
    )

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    trade = result.trades.row(0, named=True)
    assert trade["exit_day"] == d(2)
    assert trade["reason"] == "stop"
    assert trade["exit_price"] == pytest.approx(98.0)


def test_insufficient_cash_rejected():
    panel = build_panel([bar(d(0), "AAA", 10_000), bar(d(1), "AAA", 10_000)])
    strategy = ScriptedStrategy({d(0): [Order("buy", "AAA", budget=1_000_000)]})

    result = run_backtest(
        panel,
        strategy,
        config=EngineConfig(initial_cash=1_000.0, slippage_pct=0.0),
        costs=ZeroCost(),
    )

    assert result.rejections.row(0, named=True)["reason"] == "no-cash"


def test_volume_cap_partially_fills():
    panel = build_panel([bar(d(0), "AAA", 100, volume=100), bar(d(1), "AAA", 100, volume=100)])
    strategy = ScriptedStrategy({d(0): [Order("buy", "AAA", budget=5_000)]})

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    assert result.rejections.is_empty()
    day1 = result.equity.filter(pl.col("day") == d(1)).row(0, named=True)
    assert day1["position_value"] == pytest.approx(10 * 100.0)


def test_split_factor_sizes_shares_on_raw_price():
    panel = build_panel(
        [
            bar(d(0), "AAA", 53_000, factor=0.02),
            bar(d(1), "AAA", 53_000, factor=0.02),
            bar(d(2), "AAA", 53_000, factor=1.0),
            bar(d(3), "AAA", 54_000, factor=1.0),
        ]
    )
    strategy = ScriptedStrategy(
        {
            d(0): [Order("buy", "AAA", budget=3_000_000)],
            d(2): [Order("sell", "AAA")],
        }
    )

    result = run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    day1 = result.equity.filter(pl.col("day") == d(1)).row(0, named=True)
    assert day1["cash"] == pytest.approx(10_000_000 - 2_650_000)
    assert day1["position_value"] == pytest.approx(2_650_000.0)
    trade = result.trades.row(0, named=True)
    assert trade["entry_notional"] == pytest.approx(2_650_000.0)
    assert trade["exit_notional"] == pytest.approx(50 * 54_000.0)
    assert trade["pnl"] == pytest.approx(50 * 54_000.0 - 2_650_000.0)


def test_strategy_view_never_sees_future():
    panel = build_panel(flat_series("AAA", range(5)))
    strategy = ScriptedStrategy()

    run_backtest(panel, strategy, config=NO_SLIP, costs=ZeroCost())

    assert len(strategy.observations) == 4
    for view, portfolio in strategy.observations:
        assert portfolio.day == view.day
        assert view.history("AAA")["day"].max() == view.day
        assert view.cross_section()["day"].unique().to_list() == [view.day]


def test_backtest_is_deterministic():
    panel = build_panel(
        [
            bar(d(0), "AAA", 100),
            bar(d(1), "AAA", 102, close=104),
            bar(d(2), "AAA", 103, close=101, low=99),
            bar(d(3), "AAA", 101, close=105),
        ]
    )

    def make():
        return ScriptedStrategy(
            {
                d(0): [Order("buy", "AAA", budget=2_000_000, stop=95.0, target=110.0)],
                d(2): [Order("sell", "AAA")],
            }
        )

    first = run_backtest(panel, make(), costs=KrCostModel())
    second = run_backtest(panel, make(), costs=KrCostModel())

    assert first.equity.equals(second.equity)
    assert first.trades.equals(second.trades)
    assert first.rejections.equals(second.rejections)
    assert first.stats == second.stats


def test_empty_panel_raises():
    with pytest.raises(ValueError):
        run_backtest(pl.DataFrame(), ScriptedStrategy())

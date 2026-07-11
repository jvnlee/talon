from datetime import date, timedelta

import polars as pl
import pytest

from talon.backtest.engine import EngineConfig, run_backtest
from talon.quant.core import QuantCore, closed_trades_frame
from talon.quant.regime import Regime
from talon.quant.risk import RiskConfig, RiskGate
from talon.quant.signals import StrategySpec
from talon.quant.universe import LiquidityUniverse

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


class BullStub:
    def columns(self):
        return {}

    def assess(self, day_frame):
        return Regime(label="bull", exposure=1.0, breadth=None, weights={})


def make_core(panel, spec):
    return QuantCore(panel, strategies=[spec], regime_filter=BullStub(), gate=RiskGate())


def run(panel, core):
    return run_backtest(panel, core, config=NO_SLIP, costs=ZeroCost())


def test_entry_is_sized_and_held_symbol_not_rebought():
    spec = StrategySpec(
        name="teststrat",
        entry=("close >= 100",),
        score="close",
        stop="close - 10",
        target="close + 20",
        max_hold_days=50,
    )
    panel = build_panel([bar(d(i), "AAA", 100.0) for i in range(5)])
    core = make_core(panel, spec)
    result = run(panel, core)

    assert result.trades.is_empty()
    assert result.stats.open_positions == 1
    day1 = result.equity.filter(pl.col("day") == d(1)).row(0, named=True)
    assert day1["position_value"] == pytest.approx(10_000 * 100.0)
    assert core.owner_of("AAA") == "teststrat"
    assert any(item.reason == "already-held" for item in core.interventions)


def test_three_stop_outs_trigger_cooldown():
    spec = StrategySpec(
        name="teststrat",
        entry=("close > 1",),
        score="close",
        stop="close - 5",
        target="close + 100",
        max_hold_days=50,
    )
    panel = build_panel(
        [
            bar(d(0), "AAA", 100.0),
            bar(d(1), "AAA", 100.0, close=96.0, high=100.0, low=94.0),
            bar(d(2), "AAA", 96.0, close=92.0, high=96.0, low=90.0),
            bar(d(3), "AAA", 92.0, close=88.0, high=92.0, low=86.0),
            bar(d(4), "AAA", 88.0),
            bar(d(5), "AAA", 88.0),
        ]
    )
    core = QuantCore(
        panel,
        strategies=[spec],
        regime_filter=BullStub(),
        gate=RiskGate(RiskConfig(cooldown_after_losses=3)),
    )
    result = run(panel, core)

    assert result.trades.height == 3
    assert result.trades["reason"].unique().to_list() == ["stop"]
    actions = [item.action for item in core.interventions]
    assert "cooldown" in actions
    blocked = [item for item in core.interventions if item.reason == "cooldown"]
    assert {item.day for item in blocked} == {d(3), d(4)}
    assert result.stats.open_positions == 0
    assert core.owner_of("AAA") is None


def test_rejected_buy_releases_ownership():
    spec = StrategySpec(
        name="teststrat",
        entry=("close == 100",),
        score="close",
        stop="close - 10",
        target="close + 20",
        max_hold_days=50,
    )
    panel = build_panel(
        [
            bar(d(0), "AAA", 100.0),
            bar(d(1), "AAA", 130.0),
            bar(d(2), "AAA", 130.0),
        ]
    )
    core = make_core(panel, spec)
    result = run(panel, core)

    assert result.trades.is_empty()
    assert result.stats.open_positions == 0
    assert result.rejections.filter(pl.col("reason") == "limit-up").height == 1
    assert core.owner_of("AAA") is None


def test_max_hold_days_forces_time_exit():
    spec = StrategySpec(
        name="teststrat",
        entry=("close == 100",),
        score="close",
        stop="close - 10",
        target="close + 20",
        max_hold_days=2,
    )
    rows = [bar(d(0), "AAA", 100.0)]
    rows += [bar(d(i), "AAA", 100.0, close=101.0) for i in range(1, 5)]
    panel = build_panel(rows)
    core = make_core(panel, spec)
    result = run(panel, core)

    trade = result.trades.row(0, named=True)
    assert trade["entry_day"] == d(1)
    assert trade["exit_day"] == d(4)
    assert trade["reason"] == "strategy"


def test_rule_exit_sells_position():
    spec = StrategySpec(
        name="teststrat",
        entry=("close == 100",),
        score="close",
        stop="close - 50",
        target="close + 50",
        exit="close < 95",
        max_hold_days=50,
    )
    panel = build_panel(
        [
            bar(d(0), "AAA", 100.0),
            bar(d(1), "AAA", 100.0, close=94.0, low=94.0),
            bar(d(2), "AAA", 94.0),
            bar(d(3), "AAA", 94.0),
        ]
    )
    core = make_core(panel, spec)
    result = run(panel, core)

    trade = result.trades.row(0, named=True)
    assert trade["reason"] == "strategy"
    assert trade["exit_day"] == d(2)
    assert core.owner_of("AAA") is None
    assert core.closed_trades[0][0] == "teststrat"
    assert core.trades_by("teststrat") == 1
    assert core.trades_by("other") == 0


def test_duplicate_strategy_names_rejected():
    spec = StrategySpec(
        name="teststrat",
        entry=("close > 0",),
        score="close",
        stop="close - 10",
        target="close + 20",
    )
    panel = build_panel([bar(d(0), "AAA", 100.0)])
    with pytest.raises(ValueError, match="중복"):
        QuantCore(panel, strategies=[spec, spec], regime_filter=BullStub())


def test_universe_gates_entries_but_not_exits():
    spec = StrategySpec(
        name="teststrat",
        entry=("close >= 100",),
        score="close",
        stop="close - 50",
        target="close + 50",
        exit="close < 95",
        max_hold_days=50,
    )
    rows = []
    for i in range(4):
        close = 94.0 if i >= 2 else 100.0
        rows.append(bar(d(i), "000010", close, volume=1e6))
        rows.append(bar(d(i), "000020", 100.0, volume=1.0))
        rows.append(bar(d(i), "000015", 100.0, volume=1e6))
    panel = build_panel(rows)
    core = QuantCore(
        panel,
        strategies=[spec],
        regime_filter=BullStub(),
        gate=RiskGate(),
        universe=LiquidityUniverse(size=1, min_value=0.0),
    )
    result = run(panel, core)

    trade = result.trades.row(0, named=True)
    assert trade["symbol"] == "000010"
    assert trade["reason"] == "strategy"
    assert trade["exit_day"] == d(3)
    assert result.stats.open_positions == 0


def test_closed_trades_frame_carries_strategy():
    spec = StrategySpec(
        name="teststrat",
        entry=("close == 100",),
        score="close",
        stop="close - 50",
        target="close + 50",
        exit="close < 95",
        max_hold_days=50,
    )
    panel = build_panel(
        [
            bar(d(0), "AAA", 100.0),
            bar(d(1), "AAA", 100.0, close=94.0, low=94.0),
            bar(d(2), "AAA", 94.0),
            bar(d(3), "AAA", 94.0),
        ]
    )
    core = make_core(panel, spec)
    run(panel, core)

    frame = closed_trades_frame(core.closed_trades)
    row = frame.row(0, named=True)
    assert row["strategy"] == "teststrat"
    assert row["symbol"] == "AAA"
    assert row["reason"] == "strategy"
    assert closed_trades_frame([]).is_empty()

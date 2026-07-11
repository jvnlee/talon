from datetime import date, timedelta

import polars as pl

from talon.backtest.engine import EngineConfig, Order
from talon.backtest.lookahead import pick_cuts, verify_factors, verify_replay
from talon.factors.ops import REGISTRY, TIME_SERIES, Op
from talon.quant.core import QuantCore
from talon.quant.regime import Regime
from talon.quant.risk import RiskGate
from talon.quant.signals import StrategySpec

BASE = date(2026, 1, 5)

NO_SLIP = EngineConfig(slippage_pct=0.0)


def d(i):
    return BASE + timedelta(days=i)


def bar(day, symbol, open_, close=None, high=None, low=None, volume=1e9):
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
        "raw_close": float(close),
        "factor": 1.0,
    }


def build_panel(rows):
    return (
        pl.DataFrame(rows)
        .sort("symbol", "day")
        .with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))
        .sort("day", "symbol")
    )


def wavy_panel(days=30, symbols=("AAA", "BBB")):
    rows = []
    for i in range(days):
        for j, symbol in enumerate(symbols):
            price = 100.0 + 10.0 * ((i + j) % 5) + i * 0.5
            rows.append(bar(d(i), symbol, price, close=price + (1 if i % 2 else -1)))
    return build_panel(rows)


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


def test_pick_cuts_interior_and_count():
    days = [d(i) for i in range(10)]
    assert pick_cuts(days, 0) == []
    assert pick_cuts([d(0)], 3) == []
    assert pick_cuts([d(0), d(1)], 3) == []
    single = pick_cuts(days, 1)
    assert single == [d(5)]
    three = pick_cuts(days, 3)
    assert len(three) == 3
    assert all(d(0) < cut < d(9) for cut in three)
    assert pick_cuts(days, 99) == [d(i) for i in range(1, 9)]


def test_causal_factors_are_prefix_invariant():
    panel = wavy_panel()
    factors = {
        "ma": "Mean(close, 5)",
        "ema": "EMA(close, 4)",
        "rank": "CSRank(Delta(close, 3))",
        "band": "(close - Mean(close, 5)) / Std(close, 5)",
    }
    assert verify_factors(panel, factors, pick_cuts(panel["day"].to_list(), 3)) == []


def test_future_shift_is_detected_at_cut_boundary():
    REGISTRY["LeadRef"] = Op(
        "LeadRef",
        1,
        0,
        TIME_SERIES,
        1,
        lambda c, p: c[0].shift(-1),
        lambda c, p: 0,
    )
    try:
        panel = wavy_panel()
        cut = pick_cuts(panel["day"].to_list(), 1)[0]
        violations = verify_factors(panel, {"leak": "LeadRef(close)"}, [cut])
    finally:
        del REGISTRY["LeadRef"]

    assert violations
    assert {v.day for v in violations} == {cut}
    assert all(v.prefix_value is None for v in violations)


def test_centered_window_is_detected():
    REGISTRY["CenterMean"] = Op(
        "CenterMean",
        1,
        1,
        TIME_SERIES,
        1,
        lambda c, p: c[0].rolling_mean(p[0], center=True),
        lambda c, p: p[0],
    )
    try:
        panel = wavy_panel()
        cuts = pick_cuts(panel["day"].to_list(), 2)
        violations = verify_factors(panel, {"leak": "CenterMean(close, 5)"}, cuts)
    finally:
        del REGISTRY["CenterMean"]

    assert violations


def always_in_spec():
    return StrategySpec(
        name="teststrat",
        entry=("close > 1",),
        score="close",
        stop="close - 5",
        target="close + 100",
        max_hold_days=4,
    )


def quant_core_builder(panel):
    return QuantCore(
        panel, strategies=[always_in_spec()], regime_filter=BullStub(), gate=RiskGate()
    )


def test_quant_core_replay_is_prefix_invariant():
    panel = wavy_panel(days=25)
    cuts = pick_cuts(panel["day"].to_list(), 3)
    violations = verify_replay(panel, quant_core_builder, cuts, config=NO_SLIP, costs=ZeroCost())
    assert violations == []


class OneDayPeeker:
    def __init__(self, panel):
        self.panel = panel

    def decide(self, view, portfolio):
        held = "AAA" in portfolio.positions
        today = self.panel.filter((pl.col("symbol") == "AAA") & (pl.col("day") == view.day))
        future = (
            self.panel.filter((pl.col("symbol") == "AAA") & (pl.col("day") > view.day))
            .sort("day")
            .head(1)
        )
        if today.is_empty() or future.is_empty():
            return []
        rising = future.get_column("close").item() > today.get_column("close").item()
        if rising and not held:
            return [Order("buy", "AAA", budget=1_000_000.0)]
        if not rising and held:
            return [Order("sell", "AAA")]
        return []


def test_one_day_peek_is_detected():
    panel = wavy_panel(days=20)
    cuts = pick_cuts(panel["day"].to_list(), 3)
    violations = verify_replay(panel, OneDayPeeker, cuts, config=NO_SLIP, costs=ZeroCost())

    assert violations
    assert any(v.kind == "decision" and v.day == v.cut for v in violations)


def test_replay_skips_symbols_without_bars_around_cut():
    rows = [bar(d(i), "AAA", 100.0 + i) for i in range(10)]
    rows += [bar(d(i), "LATE", 50.0) for i in range(6, 10)]
    rows += [bar(d(i), "GONE", 70.0) for i in range(3)]
    panel = build_panel(rows)

    violations = verify_replay(panel, quant_core_builder, [d(4)], config=NO_SLIP, costs=ZeroCost())
    assert violations == []

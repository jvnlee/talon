from datetime import date, timedelta

import pytest

from talon.backtest.engine import ClosedTrade, PortfolioView, PositionView
from talon.quant.regime import Regime
from talon.quant.risk import RiskConfig, RiskGate, interventions_frame
from talon.quant.signals import Signal

D0 = date(2026, 1, 5)

BULL = Regime(label="bull", exposure=1.0, breadth=0.7, weights={})
BEAR = Regime(label="bear", exposure=0.0, breadth=0.2, weights={})


def d(i):
    return D0 + timedelta(days=i)


def signal(
    symbol="AAA",
    strategy="s1",
    score=0.5,
    ref=10_000.0,
    stop=9_500.0,
    target=11_000.0,
    min_open=None,
    execution="open",
):
    return Signal(
        strategy=strategy,
        symbol=symbol,
        score=score,
        ref_price=ref,
        stop=stop,
        target=target,
        min_open=min_open,
        execution=execution,
    )


def position(symbol, value, entry_price=100.0, shares=None, entry_day=D0):
    shares = shares if shares is not None else value / entry_price
    return PositionView(
        symbol=symbol,
        shares=shares,
        entry_day=entry_day,
        entry_price=entry_price,
        stop=None,
        target=None,
        value=value,
    )


def portfolio(day=D0, cash=10_000_000.0, positions=()):
    return PortfolioView(
        day=day,
        cash=cash,
        equity=cash + sum(p.value for p in positions),
        positions={p.symbol: p for p in positions},
    )


def closed(symbol, exit_day, pnl, entry_notional=1_000_000.0, reason="stop"):
    return ClosedTrade(
        symbol=symbol,
        entry_day=exit_day - timedelta(days=1),
        exit_day=exit_day,
        entry_notional=entry_notional,
        pnl=pnl,
        return_pct=pnl / entry_notional,
        reason=reason,
    )


def reasons(gate):
    return [item.reason for item in gate.interventions]


def test_r_sizing_creates_budgeted_buy():
    gate = RiskGate()
    result = gate.apply(D0, portfolio(), [signal()], BULL)

    order = result.orders[0]
    assert order.kind == "buy"
    assert order.symbol == "AAA"
    assert order.budget == pytest.approx(200 * 10_000.0)
    assert order.stop == 9_500.0
    assert order.target == 11_000.0
    assert [s.symbol for s in result.approved] == ["AAA"]
    assert gate.interventions == []


def test_weight_cap_trims_budget():
    gate = RiskGate()
    result = gate.apply(D0, portfolio(), [signal(stop=9_990.0)], BULL)

    assert result.orders[0].budget == pytest.approx(2_000_000.0)
    trims = [i for i in gate.interventions if i.action == "trim"]
    assert [t.reason for t in trims] == ["weight-cap"]


def test_overnight_signal_without_target_sizes_by_gap_anchor():
    gate = RiskGate()
    overnight = signal(target=None, execution="close_overnight")
    result = gate.apply(D0, portfolio(), [overnight], BULL)

    order = result.orders[0]
    assert order.budget == pytest.approx(200 * 10_000.0)
    assert order.target is None
    assert order.fill_at == "close"
    assert order.exit_next_open is True
    assert gate.interventions == []


def test_open_signal_without_target_is_still_rejected():
    gate = RiskGate()
    result = gate.apply(D0, portfolio(), [signal(target=None)], BULL)

    assert result.orders == []
    assert reasons(gate) == ["no-stop-target"]


def test_rejects_invalid_levels():
    gate = RiskGate()
    bad = [
        signal(symbol="AAA", stop=None),
        signal(symbol="BBB", target=None),
        signal(symbol="CCC", stop=10_500.0),
        signal(symbol="DDD", target=9_000.0),
        signal(symbol="EEE", stop=-1.0),
        signal(symbol="FFF", min_open=float("nan")),
    ]
    result = gate.apply(D0, portfolio(), bad, BULL)

    assert result.orders == []
    assert set(reasons(gate)) == {
        "no-stop-target",
        "stop-not-below-entry",
        "target-not-above-entry",
        "stop-not-positive",
        "non-finite-levels",
    }
    assert all(item.action == "reject" for item in gate.interventions)


def test_buy_order_carries_min_open():
    gate = RiskGate()
    result = gate.apply(D0, portfolio(), [signal(min_open=10_100.0)], BULL)

    assert result.orders[0].min_open == 10_100.0


def test_close_overnight_signal_maps_to_close_fill_order():
    gate = RiskGate()
    overnight = Signal(
        strategy="s1",
        symbol="AAA",
        score=0.5,
        ref_price=10_000.0,
        stop=9_500.0,
        target=11_000.0,
        execution="close_overnight",
    )
    result = gate.apply(D0, portfolio(), [overnight], BULL)

    order = result.orders[0]
    assert order.fill_at == "close"
    assert order.exit_next_open is True


def test_max_positions_blocks_when_full():
    gate = RiskGate()
    held = [position(s, 100_000.0) for s in ("P1", "P2", "P3", "P4", "P5")]
    result = gate.apply(D0, portfolio(positions=held), [signal(symbol="NEW")], BULL)

    assert result.orders == []
    assert reasons(gate) == ["max-positions"]


def test_remaining_slots_go_to_best_scores():
    gate = RiskGate()
    held = [position(s, 100_000.0) for s in ("P1", "P2", "P3", "P4")]
    signals = [signal(symbol="LOW", score=0.2), signal(symbol="HIGH", score=0.9)]
    result = gate.apply(D0, portfolio(positions=held), signals, BULL)

    assert [o.symbol for o in result.orders] == ["HIGH"]
    blocked = [i for i in gate.interventions if i.reason == "max-positions"]
    assert [b.symbol for b in blocked] == ["LOW"]


def test_already_held_blocked():
    gate = RiskGate()
    result = gate.apply(D0, portfolio(positions=[position("AAA", 500_000.0)]), [signal()], BULL)

    assert result.orders == []
    assert reasons(gate) == ["already-held"]


def test_duplicate_symbol_keeps_best_weighted_score():
    gate = RiskGate()
    signals = [signal(strategy="s1", score=0.9), signal(strategy="s2", score=0.4)]
    result = gate.apply(D0, portfolio(), signals, BULL)

    assert len(result.orders) == 1
    assert result.approved[0].strategy == "s1"
    duplicates = [i for i in gate.interventions if i.reason == "duplicate"]
    assert [item.strategy for item in duplicates] == ["s2"]


def test_regime_bear_blocks_all():
    gate = RiskGate()
    result = gate.apply(D0, portfolio(), [signal()], BEAR)

    assert result.orders == []
    assert reasons(gate) == ["regime-bear"]


def test_regime_weight_zero_blocks_and_partial_scales():
    gate = RiskGate()
    regime = Regime(label="neutral", exposure=1.0, breadth=0.5, weights={"s1": 0.0, "s2": 0.5})
    signals = [signal(strategy="s1", symbol="AAA"), signal(strategy="s2", symbol="BBB")]
    result = gate.apply(D0, portfolio(), signals, regime)

    assert [o.symbol for o in result.orders] == ["BBB"]
    assert result.orders[0].budget == pytest.approx(100 * 10_000.0)
    assert reasons(gate) == ["regime-weight"]


def test_exposure_cap_trims_then_blocks():
    gate = RiskGate()
    regime = Regime(label="neutral", exposure=0.6, breadth=0.5, weights={})
    held = [position("P1", 3_000_000.0), position("P2", 2_500_000.0)]
    result = gate.apply(D0, portfolio(cash=4_500_000.0, positions=held), [signal()], regime)

    assert result.orders[0].budget == pytest.approx(500_000.0)
    trims = [i for i in gate.interventions if i.action == "trim"]
    assert [t.reason for t in trims] == ["exposure-cap"]

    gate = RiskGate()
    held = [position("P1", 5_995_000.0)]
    result = gate.apply(D0, portfolio(cash=4_005_000.0, positions=held), [signal()], regime)
    assert result.orders == []
    assert "exposure-cap" in reasons(gate)


def test_daily_breaker_blocks_new_entries():
    gate = RiskGate()
    gate.apply(d(0), portfolio(), [], BULL)
    gate.record_close(closed("AAA", d(1), -210_000.0), "s1")
    result = gate.apply(d(1), portfolio(cash=9_790_000.0), [signal(symbol="BBB")], BULL)

    assert result.orders == []
    assert "daily-loss" in reasons(gate)
    assert "daily-breaker" in reasons(gate)


def test_weekly_breaker_blocks_rest_of_week_then_resets():
    gate = RiskGate()
    gate.apply(d(0), portfolio(), [], BULL)
    gate.record_close(closed("AAA", d(1), -300_000.0), "s1")
    gate.record_close(closed("BBB", d(2), -250_000.0), "s2")

    blocked = gate.apply(d(3), portfolio(cash=9_450_000.0), [signal(symbol="CCC")], BULL)
    assert blocked.orders == []
    assert "weekly-loss" in reasons(gate)
    assert "weekly-breaker" in reasons(gate)

    next_week = gate.apply(d(7), portfolio(cash=9_450_000.0), [signal(symbol="CCC")], BULL)
    assert len(next_week.orders) == 1


def test_cooldown_after_consecutive_losses():
    gate = RiskGate(RiskConfig(cooldown_after_losses=3))
    for i in (1, 2, 3):
        gate.record_close(closed("AAA", d(i), -1_000.0), "s1")

    cooldowns = [i for i in gate.interventions if i.action == "cooldown"]
    assert [c.strategy for c in cooldowns] == ["s1"]

    mixed = [signal(strategy="s1"), signal(symbol="BBB", strategy="s2")]
    blocked = gate.apply(d(4), portfolio(), mixed, BULL)
    assert [o.symbol for o in blocked.orders] == ["BBB"]
    assert "cooldown" in reasons(gate)

    still = gate.apply(d(8), portfolio(), [signal(strategy="s1", symbol="CCC")], BULL)
    assert still.orders == []

    resumed = gate.apply(d(9), portfolio(), [signal(strategy="s1", symbol="DDD")], BULL)
    assert len(resumed.orders) == 1


def test_win_resets_loss_streak():
    gate = RiskGate()
    gate.record_close(closed("AAA", d(1), -1_000.0), "s1")
    gate.record_close(closed("BBB", d(2), -1_000.0), "s1")
    gate.record_close(closed("CCC", d(3), 5_000.0), "s1")
    gate.record_close(closed("DDD", d(4), -1_000.0), "s1")

    assert all(item.action != "cooldown" for item in gate.interventions)


def test_drawdown_reduce_halves_new_risk():
    gate = RiskGate()
    gate.apply(d(0), portfolio(), [], BULL)
    result = gate.apply(d(1), portfolio(cash=8_900_000.0), [signal()], BULL)

    assert any(item.action == "reduce" for item in gate.interventions)
    assert result.orders[0].budget == pytest.approx(89 * 10_000.0)


def test_drawdown_reduce_sells_worst_positions_first():
    gate = RiskGate()
    gate.apply(d(0), portfolio(), [], BULL)
    losing = position("BAD", 2_400_000.0, entry_price=100.0, shares=30_000)
    winning = position("GOOD", 2_100_000.0, entry_price=100.0, shares=20_000)
    result = gate.apply(d(1), portfolio(cash=4_400_000.0, positions=[losing, winning]), [], BULL)

    sells = [o for o in result.orders if o.kind == "sell"]
    assert [o.symbol for o in sells] == ["BAD"]


def test_drawdown_liquidate_halts_permanently():
    gate = RiskGate()
    gate.apply(d(0), portfolio(), [], BULL)
    held = [position("AAA", 1_000_000.0)]
    result = gate.apply(
        d(1), portfolio(cash=7_400_000.0, positions=held), [signal(symbol="BBB")], BULL
    )

    assert gate.halted
    assert [o.kind for o in result.orders] == ["sell"]
    assert any(item.action == "liquidate" for item in gate.interventions)
    assert "halted" in reasons(gate)

    again = portfolio(cash=7_400_000.0, positions=held)
    later = gate.apply(d(2), again, [signal(symbol="CCC")], BULL)
    assert [o.kind for o in later.orders] == ["sell"]
    assert reasons(gate).count("halted") == 2


def test_interventions_frame_schema():
    gate = RiskGate()
    gate.apply(D0, portfolio(), [signal(stop=None)], BULL)
    frame = interventions_frame(gate.interventions)

    assert frame.height == 1
    row = frame.row(0, named=True)
    assert row["reason"] == "no-stop-target"
    assert row["symbol"] == "AAA"

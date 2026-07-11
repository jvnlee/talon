import pytest

from talon.backtest.crosscheck import run_crosscheck

pytest.importorskip("vectorbt")


def test_engine_matches_vectorbt_on_random_scenarios():
    report = run_crosscheck(seed=11, scenarios=3, symbols=2, days=80)

    assert report.trades > 5
    assert report.mismatches == []
    assert report.ok


def test_comparator_detects_slippage_distortion():
    report = run_crosscheck(seed=11, scenarios=1, symbols=1, days=80, vbt_slippage_override=0.0)

    assert not report.ok
    kinds = {mismatch.kind for mismatch in report.mismatches}
    assert kinds & {"trade-pnl", "equity"}


def test_zero_cost_scenarios_match():
    class FreeCost:
        def buy_fee(self, notional, day):
            return 0.0

        def sell_fee(self, notional, day):
            return 0.0

    report = run_crosscheck(seed=3, scenarios=2, symbols=1, days=80, costs=FreeCost())

    assert report.trades > 0
    assert report.ok

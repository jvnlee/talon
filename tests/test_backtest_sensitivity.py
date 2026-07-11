import pytest

from talon.backtest.metrics import BacktestStats
from talon.backtest.sensitivity import neighbors, run_sweep


def make_stats(sharpe=None, total_return_pct=0.0, trades=0):
    return BacktestStats(
        initial_cash=10_000_000.0,
        final_equity=10_000_000.0,
        total_return_pct=total_return_pct,
        mdd_pct=5.0,
        sharpe=sharpe,
        trades=trades,
        wins=0,
        total_fees=0.0,
        open_positions=0,
    )


def test_neighbors_int_scales_and_clamps():
    assert neighbors(60) == [45, 75]
    assert neighbors(5) == [4, 6]
    assert neighbors(1) == [2]


def test_neighbors_float_keeps_sign():
    assert neighbors(2.0) == [1.5, 2.5]
    assert neighbors(-2.0) == [-1.5, -2.5]


def test_run_sweep_judges_retention_per_param():
    base = make_stats(sharpe=2.0, total_return_pct=50.0, trades=100)
    outcomes = {
        ("s1", "fast", 15.0): make_stats(sharpe=1.5),
        ("s1", "fast", 25.0): make_stats(sharpe=1.2),
        ("s1", "slow", 45.0): make_stats(sharpe=0.4),
        ("s1", "slow", 75.0): make_stats(sharpe=1.8),
    }
    seen = []

    def runner(strategy, param, value):
        return outcomes[(strategy, param, float(value))], 5

    report = run_sweep(
        base_stats=base,
        params={"s1": {"fast": 20, "slow": 60}},
        runner=runner,
        progress=seen.append,
    )

    assert len(seen) == 4
    verdicts = {(v.strategy, v.param): v for v in report.params}
    fast = verdicts[("s1", "fast")]
    assert fast.robust
    assert fast.active
    assert fast.base_value == 20.0
    assert fast.runs[0].retention == pytest.approx(0.75)
    assert fast.runs[0].strategy_trades == 5
    slow = verdicts[("s1", "slow")]
    assert not slow.robust
    assert slow.runs[0].retention == pytest.approx(0.2)
    assert report.base_sharpe == 2.0
    assert report.robust is False


def test_run_sweep_without_positive_base_requires_positive_neighbors():
    base = make_stats(sharpe=None)

    def runner(strategy, param, value):
        return make_stats(sharpe=0.1 if value > 20 else -0.1), 0

    report = run_sweep(base_stats=base, params={"s1": {"win": 20}}, runner=runner)

    runs = report.params[0].runs
    assert not runs[0].ok
    assert runs[1].ok
    assert runs[1].retention is None
    assert not report.params[0].active
    assert not report.robust


def test_run_sweep_all_robust_with_unknown_attribution():
    base = make_stats(sharpe=1.0)

    def runner(strategy, param, value):
        return make_stats(sharpe=0.9), None

    report = run_sweep(base_stats=base, params={"s1": {"a": 10}, "s2": {"b": 4.0}}, runner=runner)

    assert report.robust
    assert [v.param for v in report.params] == ["a", "b"]
    assert all(v.active for v in report.params)

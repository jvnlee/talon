from datetime import date

import polars as pl
import pytest

from talon.backtest.grid import approx_pct, clamp_is_end, describe, run_grid
from talon.backtest.metrics import BacktestStats


def stats(sharpe=1.0, trades=50):
    return BacktestStats(
        initial_cash=10_000_000.0,
        final_equity=11_000_000.0,
        total_return_pct=10.0,
        mdd_pct=5.0,
        sharpe=sharpe,
        trades=trades,
        wins=trades // 2,
        total_fees=0.0,
        open_positions=0,
    )


def wobbly_curve(scale=1.0, steps=60):
    equity = 10_000_000.0
    values = []
    for index in range(steps):
        equity *= 1 + (0.02 * scale if index % 2 == 0 else -0.01 * scale)
        values.append(equity)
    return pl.Series(values)


def test_clamp_is_end_defaults_to_the_eve_of_oos():
    assert clamp_is_end(None, date(2024, 1, 1)) == date(2023, 12, 31)
    assert clamp_is_end(date(2023, 12, 29), date(2024, 1, 1)) == date(2023, 12, 29)


def test_clamp_is_end_refuses_the_sealed_zone():
    with pytest.raises(ValueError, match="OOS 봉인"):
        clamp_is_end(date(2024, 1, 1), date(2024, 1, 1))
    with pytest.raises(ValueError, match="OOS 봉인"):
        clamp_is_end(date(2025, 6, 1), date(2024, 1, 1))


def test_describe_is_compact_and_ordered():
    params = {"strength_pct": 2.0, "volume_mult": 1.5, "tail_max": 0.3}
    expected = "close_bet_v1(strength_pct=2,volume_mult=1.5,tail_max=0.3)"
    assert describe("close_bet_v1", params) == expected


def test_approx_pct_counts_non_exact_rows():
    panel = pl.DataFrame({"intraday_exact": [True, False, False, True]})
    assert approx_pct(panel) == pytest.approx(50.0)
    assert approx_pct(pl.DataFrame({"intraday_exact": []})) == 0.0


def test_run_grid_records_every_combo_and_deflates_the_best():
    grid = ({"a": 1.0}, {"a": 2.0}, {"a": 3.0})

    def runner(params):
        return stats(sharpe=params["a"]), wobbly_curve(), int(params["a"])

    report = run_grid(
        strategy="probe",
        grid=grid,
        runner=runner,
        initial_cash=10_000_000.0,
        oos_start=date(2024, 1, 1),
        panel_approx_pct=100.0,
        trial_sharpes=lambda: [0.01, 0.02, 0.05],
    )

    assert [run.trial for run in report.runs] == [1, 2, 3]
    assert report.best == "probe(a=3)"
    assert report.approx_pct == 100.0
    assert report.deflated is not None
    assert report.deflated.trials == 3


def test_run_grid_without_any_sharpe_has_no_best():
    def runner(params):
        return stats(sharpe=None, trades=0), wobbly_curve(), 7

    report = run_grid(
        strategy="probe",
        grid=({"a": 1.0},),
        runner=runner,
        initial_cash=10_000_000.0,
        oos_start=date(2024, 1, 1),
        panel_approx_pct=0.0,
        trial_sharpes=lambda: [0.01, 0.02],
    )

    assert report.best is None
    assert report.deflated is None


def test_run_grid_reports_progress_in_order():
    seen = []

    def runner(params):
        return stats(sharpe=params["a"]), wobbly_curve(), int(params["a"])

    run_grid(
        strategy="probe",
        grid=({"a": 2.0}, {"a": 1.0}),
        runner=runner,
        initial_cash=10_000_000.0,
        oos_start=date(2024, 1, 1),
        panel_approx_pct=0.0,
        trial_sharpes=lambda: [],
        progress=lambda run: seen.append(run.description),
    )

    assert seen == ["probe(a=2)", "probe(a=1)"]

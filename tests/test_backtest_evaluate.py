from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from talon.backtest.benchmark import BenchmarkStats
from talon.backtest.engine import Order
from talon.backtest.evaluate import WindowReport, _build_checks, evaluate_gate1
from talon.backtest.metrics import BacktestStats

BASE = date(2026, 1, 5)


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


def flat_benchmark(days, close=100.0):
    return pl.DataFrame({"day": days, "close": [close] * len(days)})


class FakeCore:
    def __init__(self, script):
        self.script = script
        self.gate = SimpleNamespace(halted=False)
        self.interventions = []

    def decide(self, view, portfolio):
        return self.script.get(view.day, [])


def test_evaluate_splits_windows_and_compares_benchmark():
    rows = [bar(d(i), "AAA", 100.0 + i) for i in range(-2, 30)]
    panel = build_panel(rows)
    benchmark = flat_benchmark([d(i) for i in range(-2, 30)])
    script = {
        d(1): [Order("buy", "AAA", budget=1_000_000)],
        d(3): [Order("sell", "AAA")],
        d(21): [Order("buy", "AAA", budget=1_000_000)],
        d(23): [Order("sell", "AAA")],
    }
    created = []

    def make_core(core_panel):
        created.append(core_panel)
        return FakeCore(script)

    evaluation = evaluate_gate1(
        panel,
        make_core=make_core,
        benchmark_daily=benchmark,
        oos_start=d(20),
        trading_start=d(0),
    )
    report = evaluation.report

    assert created[0]["day"].min() == d(-2)
    assert created[0]["day"].max() == d(19)
    assert created[1]["day"].min() == d(-2)
    assert created[1]["day"].max() == d(29)

    assert report.trading_start == d(0)
    assert report.oos_start == d(20)
    assert report.end == d(29)
    assert report.in_sample.stats.start == d(0)
    assert report.in_sample.stats.end == d(19)
    assert report.out_of_sample.stats.start == d(20)
    assert report.out_of_sample.stats.end == d(29)
    assert report.in_sample.stats.trades == 1
    assert report.out_of_sample.stats.trades == 1

    assert report.in_sample.benchmark is not None
    assert report.in_sample.benchmark.return_pct == pytest.approx(0.0)
    assert report.in_sample.excess_return_pct == pytest.approx(
        report.in_sample.stats.total_return_pct
    )

    checks = {check.name: check for check in report.checks}
    assert not checks["coverage"].passed
    assert checks["oos-vs-kospi"].passed
    assert checks["mdd"].passed
    assert not checks["trades"].passed
    assert checks["profit-factor"].passed
    assert report.passed is False

    assert evaluation.in_sample.trades.height == 1
    assert evaluation.out_of_sample.trades.height == 1


def test_evaluate_rejects_oos_start_outside_range():
    panel = build_panel([bar(d(i), "AAA", 100.0) for i in range(10)])
    benchmark = flat_benchmark([d(i) for i in range(10)])

    def make_core(core_panel):
        return FakeCore({})

    for bad in (d(0), d(40)):
        with pytest.raises(ValueError):
            evaluate_gate1(
                panel,
                make_core=make_core,
                benchmark_daily=benchmark,
                oos_start=bad,
            )


def make_stats(**overrides):
    base = {
        "initial_cash": 10_000_000.0,
        "final_equity": 11_000_000.0,
        "total_return_pct": 10.0,
        "mdd_pct": 10.0,
        "trades": 150,
        "wins": 100,
        "profit_factor": 1.5,
        "total_fees": 0.0,
        "open_positions": 0,
    }
    base.update(overrides)
    return BacktestStats(**base)


def make_window(label, benchmark_return=5.0, benchmark=True, **overrides):
    bench = (
        BenchmarkStats(
            name="KOSPI",
            start=date(2015, 1, 2),
            end=date(2026, 6, 30),
            return_pct=benchmark_return,
            mdd_pct=15.0,
        )
        if benchmark
        else None
    )
    return WindowReport(label=label, stats=make_stats(**overrides), benchmark=bench)


LONG_START = date(2015, 1, 2)
LONG_OOS = date(2023, 7, 3)
LONG_END = date(2026, 6, 30)


def checks_by_name(is_window, oos_window, start=LONG_START, oos=LONG_OOS, end=LONG_END):
    return {check.name: check for check in _build_checks(start, oos, end, is_window, oos_window)}


def test_checks_pass_on_healthy_windows():
    checks = checks_by_name(make_window("in_sample"), make_window("out_of_sample"))
    assert all(check.passed for check in checks.values())


def test_coverage_requires_ten_years_and_two_year_oos():
    checks = checks_by_name(
        make_window("in_sample"),
        make_window("out_of_sample"),
        start=date(2020, 1, 2),
    )
    assert not checks["coverage"].passed
    checks = checks_by_name(
        make_window("in_sample"),
        make_window("out_of_sample"),
        oos=date(2025, 1, 2),
    )
    assert not checks["coverage"].passed


def test_oos_must_strictly_beat_benchmark():
    checks = checks_by_name(
        make_window("in_sample"),
        make_window("out_of_sample", total_return_pct=5.0, benchmark_return=5.0),
    )
    assert not checks["oos-vs-kospi"].passed
    checks = checks_by_name(
        make_window("in_sample"),
        make_window("out_of_sample", benchmark=False),
    )
    assert not checks["oos-vs-kospi"].passed


def test_mdd_boundary_is_inclusive():
    checks = checks_by_name(
        make_window("in_sample", mdd_pct=20.0),
        make_window("out_of_sample", mdd_pct=20.0),
    )
    assert checks["mdd"].passed
    checks = checks_by_name(
        make_window("in_sample"),
        make_window("out_of_sample", mdd_pct=20.01),
    )
    assert not checks["mdd"].passed


def test_trades_summed_across_windows():
    checks = checks_by_name(
        make_window("in_sample", trades=100),
        make_window("out_of_sample", trades=100),
    )
    assert checks["trades"].passed
    checks = checks_by_name(
        make_window("in_sample", trades=100),
        make_window("out_of_sample", trades=99),
    )
    assert not checks["trades"].passed


def test_profit_factor_boundary_and_lossless_fallback():
    checks = checks_by_name(
        make_window("in_sample", profit_factor=1.3),
        make_window("out_of_sample", profit_factor=1.3),
    )
    assert checks["profit-factor"].passed
    checks = checks_by_name(
        make_window("in_sample", profit_factor=1.29),
        make_window("out_of_sample"),
    )
    assert not checks["profit-factor"].passed
    checks = checks_by_name(
        make_window("in_sample", profit_factor=None, trades=10, wins=10),
        make_window("out_of_sample"),
    )
    assert checks["profit-factor"].passed
    checks = checks_by_name(
        make_window("in_sample", profit_factor=None, trades=0, wins=0),
        make_window("out_of_sample"),
    )
    assert not checks["profit-factor"].passed

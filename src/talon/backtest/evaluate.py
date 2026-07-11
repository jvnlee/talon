from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol, cast

import polars as pl
from pydantic import BaseModel

from talon.backtest.benchmark import BenchmarkStats, buy_and_hold
from talon.backtest.data import MarketView
from talon.backtest.engine import (
    BacktestResult,
    EngineConfig,
    Order,
    PortfolioView,
    run_backtest,
)
from talon.backtest.metrics import BacktestStats, DeflatedSharpe, deflated_sharpe

MIN_TOTAL_YEARS = 10.0
MIN_OOS_YEARS = 2.0
MAX_MDD_PCT = 20.0
MIN_TRADES = 200
MIN_PROFIT_FACTOR = 1.3
MIN_TRIALS_FOR_DSR = 2


class GateState(Protocol):
    @property
    def halted(self) -> bool: ...


class GateIntervention(Protocol):
    @property
    def action(self) -> str: ...


class EvaluableCore(Protocol):
    @property
    def gate(self) -> GateState: ...

    @property
    def interventions(self) -> Sequence[GateIntervention]: ...

    def decide(self, view: MarketView, portfolio: PortfolioView) -> list[Order]: ...


CoreFactory = Callable[[pl.DataFrame], EvaluableCore]


class WindowReport(BaseModel):
    label: str
    stats: BacktestStats
    benchmark: BenchmarkStats | None = None
    excess_return_pct: float | None = None
    halted: bool = False
    interventions: dict[str, int] = {}


class GateCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class Gate1Report(BaseModel):
    trading_start: date
    oos_start: date
    end: date
    in_sample: WindowReport
    out_of_sample: WindowReport
    deflated: DeflatedSharpe | None = None
    checks: list[GateCheck]
    passed: bool


@dataclass
class Gate1Evaluation:
    report: Gate1Report
    in_sample: BacktestResult
    out_of_sample: BacktestResult


def _run_window(
    label: str,
    trading_frame: pl.DataFrame,
    core_panel: pl.DataFrame,
    make_core: CoreFactory,
    benchmark_daily: pl.DataFrame,
    config: EngineConfig,
) -> tuple[WindowReport, BacktestResult]:
    core = make_core(core_panel)
    result = run_backtest(trading_frame, core, config=config)
    window_benchmark: BenchmarkStats | None = None
    excess: float | None = None
    if result.stats.start is not None and result.stats.end is not None:
        window_frame = benchmark_daily.filter(
            pl.col("day").is_between(result.stats.start, result.stats.end)
        )
        window_benchmark = buy_and_hold(window_frame)
        if window_benchmark is not None:
            excess = result.stats.total_return_pct - window_benchmark.return_pct
    report = WindowReport(
        label=label,
        stats=result.stats,
        benchmark=window_benchmark,
        excess_return_pct=excess,
        halted=core.gate.halted,
        interventions=dict(Counter(item.action for item in core.interventions)),
    )
    return report, result


def _profit_factor_ok(stats: BacktestStats) -> bool:
    if stats.profit_factor is not None:
        return stats.profit_factor >= MIN_PROFIT_FACTOR
    return stats.trades > 0 and stats.wins > 0


def _format_pf(stats: BacktestStats) -> str:
    if stats.profit_factor is not None:
        return f"{stats.profit_factor:.2f}"
    return "무손실" if stats.trades > 0 and stats.wins > 0 else "N/A"


def _build_checks(
    trading_start: date,
    oos_start: date,
    end: date,
    in_sample: WindowReport,
    out_of_sample: WindowReport,
) -> list[GateCheck]:
    total_years = (end - trading_start).days / 365.25
    oos_years = (end - oos_start).days / 365.25
    checks = [
        GateCheck(
            name="coverage",
            passed=total_years >= MIN_TOTAL_YEARS and oos_years >= MIN_OOS_YEARS,
            detail=(
                f"전체 {total_years:.1f}년 (최소 {MIN_TOTAL_YEARS:.0f}년), "
                f"OOS {oos_years:.1f}년 (최소 {MIN_OOS_YEARS:.0f}년)"
            ),
        )
    ]
    oos_stats = out_of_sample.stats
    if out_of_sample.benchmark is None:
        checks.append(
            GateCheck(
                name="oos-vs-kospi",
                passed=False,
                detail="OOS 구간 KOSPI 벤치마크 데이터 부족 (talon index backfill 확인)",
            )
        )
    else:
        checks.append(
            GateCheck(
                name="oos-vs-kospi",
                passed=oos_stats.total_return_pct > out_of_sample.benchmark.return_pct,
                detail=(
                    f"OOS 수익률 {oos_stats.total_return_pct:.1f}% vs "
                    f"KOSPI {out_of_sample.benchmark.return_pct:.1f}%"
                ),
            )
        )
    checks.append(
        GateCheck(
            name="mdd",
            passed=in_sample.stats.mdd_pct <= MAX_MDD_PCT and oos_stats.mdd_pct <= MAX_MDD_PCT,
            detail=(
                f"IS {in_sample.stats.mdd_pct:.1f}% / OOS {oos_stats.mdd_pct:.1f}% "
                f"(한도 {MAX_MDD_PCT:.0f}%)"
            ),
        )
    )
    total_trades = in_sample.stats.trades + oos_stats.trades
    checks.append(
        GateCheck(
            name="trades",
            passed=total_trades >= MIN_TRADES,
            detail=(
                f"IS {in_sample.stats.trades}건 + OOS {oos_stats.trades}건 = "
                f"{total_trades}건 (최소 {MIN_TRADES}건)"
            ),
        )
    )
    checks.append(
        GateCheck(
            name="profit-factor",
            passed=_profit_factor_ok(in_sample.stats) and _profit_factor_ok(oos_stats),
            detail=(
                f"IS {_format_pf(in_sample.stats)} / OOS {_format_pf(oos_stats)} "
                f"(최소 {MIN_PROFIT_FACTOR})"
            ),
        )
    )
    return checks


def _deflated_check(deflated: DeflatedSharpe | None, trial_count: int) -> GateCheck:
    if trial_count < MIN_TRIALS_FOR_DSR:
        return GateCheck(
            name="deflated-sharpe",
            passed=False,
            detail=(
                f"튜닝 시도 기록 부족: {trial_count}회 "
                f"(최소 {MIN_TRIALS_FOR_DSR}회, talon backtest 실행이 기록됨)"
            ),
        )
    if deflated is None:
        return GateCheck(
            name="deflated-sharpe",
            passed=False,
            detail="IS 수익률 분포로 Deflated Sharpe를 계산할 수 없습니다",
        )
    return GateCheck(
        name="deflated-sharpe",
        passed=deflated.margin > 0,
        detail=(
            f"IS 일간 Sharpe {deflated.sharpe_daily:.4f} vs "
            f"기대 최대 {deflated.expected_max_daily:.4f} "
            f"(시도 {deflated.trials}회, 확률 {deflated.probability:.2f})"
        ),
    )


def evaluate_gate1(
    panel: pl.DataFrame,
    *,
    make_core: CoreFactory,
    benchmark_daily: pl.DataFrame,
    oos_start: date,
    trading_start: date | None = None,
    config: EngineConfig | None = None,
    trial_sharpes: Sequence[float] | None = None,
) -> Gate1Evaluation:
    if panel.is_empty():
        raise ValueError("빈 패널로는 관문 평가를 실행할 수 없습니다")
    engine_config = config if config is not None else EngineConfig()
    days = panel["day"]
    start = trading_start if trading_start is not None else cast(date, days.min())
    end = cast(date, days.max())
    if not start < oos_start <= end:
        raise ValueError(
            f"OOS 시작일이 평가 구간을 벗어났습니다: {start} < {oos_start} <= {end} 필요"
        )
    is_frame = panel.filter((pl.col("day") >= start) & (pl.col("day") < oos_start))
    oos_frame = panel.filter(pl.col("day") >= oos_start)
    if is_frame.is_empty() or oos_frame.is_empty():
        raise ValueError("IS/OOS 구간 중 한쪽에 거래일이 없습니다")
    in_sample, is_result = _run_window(
        "in_sample",
        is_frame,
        panel.filter(pl.col("day") < oos_start),
        make_core,
        benchmark_daily,
        engine_config,
    )
    out_of_sample, oos_result = _run_window(
        "out_of_sample",
        oos_frame,
        panel,
        make_core,
        benchmark_daily,
        engine_config,
    )
    checks = _build_checks(start, oos_start, end, in_sample, out_of_sample)
    sharpes = list(trial_sharpes) if trial_sharpes is not None else []
    deflated = (
        deflated_sharpe(is_result.equity["equity"], engine_config.initial_cash, sharpes)
        if len(sharpes) >= MIN_TRIALS_FOR_DSR
        else None
    )
    checks.append(_deflated_check(deflated, len(sharpes)))
    report = Gate1Report(
        trading_start=start,
        oos_start=oos_start,
        end=end,
        in_sample=in_sample,
        out_of_sample=out_of_sample,
        deflated=deflated,
        checks=checks,
        passed=all(check.passed for check in checks),
    )
    return Gate1Evaluation(report=report, in_sample=is_result, out_of_sample=oos_result)

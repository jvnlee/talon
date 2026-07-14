from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import cast

import polars as pl
from pydantic import BaseModel

from talon.backtest.metrics import BacktestStats, DeflatedSharpe, deflated_sharpe

DEFAULT_OOS_START = date(2024, 1, 1)

GridRunner = Callable[[dict[str, float]], tuple[BacktestStats, pl.Series, int]]


class GridRun(BaseModel):
    description: str
    params: dict[str, float]
    trial: int
    stats: BacktestStats


class GridReport(BaseModel):
    strategy: str
    oos_start: date
    approx_pct: float
    runs: list[GridRun]
    best: str | None
    deflated: DeflatedSharpe | None


def describe(strategy: str, params: dict[str, float]) -> str:
    inner = ",".join(f"{key}={value:g}" for key, value in params.items())
    return f"{strategy}({inner})"


def clamp_is_end(end: date | None, oos_start: date) -> date:
    if end is None:
        return oos_start - timedelta(days=1)
    if end >= oos_start:
        raise ValueError(
            f"격자 실행은 IS 전용입니다: 종료일 {end}이(가) OOS 봉인 시작일 {oos_start} 이후입니다"
        )
    return end


def approx_pct(panel: pl.DataFrame) -> float:
    if panel.is_empty():
        return 0.0
    exact = cast(float, panel.get_column("intraday_exact").cast(pl.Float64).mean() or 0.0)
    return (1.0 - exact) * 100


def run_grid(
    *,
    strategy: str,
    grid: Sequence[dict[str, float]],
    runner: GridRunner,
    initial_cash: float,
    oos_start: date,
    panel_approx_pct: float,
    trial_sharpes: Callable[[], Sequence[float]],
    progress: Callable[[GridRun], None] | None = None,
) -> GridReport:
    runs: list[GridRun] = []
    best_run: GridRun | None = None
    best_curve: pl.Series | None = None
    for params in grid:
        stats, curve, trial = runner(params)
        run = GridRun(
            description=describe(strategy, params),
            params=params,
            trial=trial,
            stats=stats,
        )
        runs.append(run)
        if progress is not None:
            progress(run)
        if stats.sharpe is not None and (
            best_run is None
            or best_run.stats.sharpe is None
            or stats.sharpe > best_run.stats.sharpe
        ):
            best_run = run
            best_curve = curve
    deflated = None
    if best_curve is not None:
        sharpes = list(trial_sharpes())
        if len(sharpes) >= 2:
            deflated = deflated_sharpe(best_curve, initial_cash, sharpes)
    return GridReport(
        strategy=strategy,
        oos_start=oos_start,
        approx_pct=panel_approx_pct,
        runs=runs,
        best=best_run.description if best_run is not None else None,
        deflated=deflated,
    )

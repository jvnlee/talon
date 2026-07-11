from collections.abc import Callable, Mapping

from pydantic import BaseModel

from talon.backtest.metrics import BacktestStats

NEIGHBOR_STEP = 0.25
RETENTION_MIN = 0.5

SweepValue = int | float
SweepRunner = Callable[[str, str, SweepValue], tuple[BacktestStats, int | None]]


class SweepRun(BaseModel):
    strategy: str
    param: str
    value: float
    sharpe: float | None = None
    total_return_pct: float
    mdd_pct: float
    profit_factor: float | None = None
    trades: int
    strategy_trades: int | None = None
    retention: float | None = None
    ok: bool


class ParamVerdict(BaseModel):
    strategy: str
    param: str
    base_value: float
    robust: bool
    active: bool
    runs: list[SweepRun]


class SensitivityReport(BaseModel):
    base_sharpe: float | None = None
    base_return_pct: float
    base_trades: int
    params: list[ParamVerdict]
    robust: bool


def neighbors(value: SweepValue) -> list[SweepValue]:
    if isinstance(value, int):
        delta = max(1, round(abs(value) * NEIGHBOR_STEP))
        return [candidate for candidate in (value - delta, value + delta) if candidate >= 1]
    return [value * (1 - NEIGHBOR_STEP), value * (1 + NEIGHBOR_STEP)]


def _neighbor_ok(base_sharpe: float | None, sharpe: float | None) -> tuple[bool, float | None]:
    if sharpe is None or sharpe <= 0:
        return False, None
    if base_sharpe is None or base_sharpe <= 0:
        return True, None
    retention = sharpe / base_sharpe
    return retention >= RETENTION_MIN, retention


def run_sweep(
    *,
    base_stats: BacktestStats,
    params: Mapping[str, Mapping[str, SweepValue]],
    runner: SweepRunner,
    progress: Callable[[SweepRun], None] | None = None,
) -> SensitivityReport:
    verdicts: list[ParamVerdict] = []
    for strategy, strategy_params in params.items():
        for param, base_value in strategy_params.items():
            runs: list[SweepRun] = []
            for value in neighbors(base_value):
                stats, strategy_trades = runner(strategy, param, value)
                ok, retention = _neighbor_ok(base_stats.sharpe, stats.sharpe)
                run = SweepRun(
                    strategy=strategy,
                    param=param,
                    value=float(value),
                    sharpe=stats.sharpe,
                    total_return_pct=stats.total_return_pct,
                    mdd_pct=stats.mdd_pct,
                    profit_factor=stats.profit_factor,
                    trades=stats.trades,
                    strategy_trades=strategy_trades,
                    retention=retention,
                    ok=ok,
                )
                runs.append(run)
                if progress is not None:
                    progress(run)
            verdicts.append(
                ParamVerdict(
                    strategy=strategy,
                    param=param,
                    base_value=float(base_value),
                    robust=bool(runs) and all(run.ok for run in runs),
                    active=any(
                        run.strategy_trades is None or run.strategy_trades > 0 for run in runs
                    ),
                    runs=runs,
                )
            )
    return SensitivityReport(
        base_sharpe=base_stats.sharpe,
        base_return_pct=base_stats.total_return_pct,
        base_trades=base_stats.trades,
        params=verdicts,
        robust=all(verdict.robust for verdict in verdicts),
    )

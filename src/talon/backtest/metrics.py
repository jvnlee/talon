import math
import statistics
from collections.abc import Sequence
from datetime import date
from typing import cast

import polars as pl
from pydantic import BaseModel

TRADING_DAYS_PER_YEAR = 252
EULER_MASCHERONI = 0.5772156649015329
_NORMAL = statistics.NormalDist()


class BacktestStats(BaseModel):
    start: date | None = None
    end: date | None = None
    initial_cash: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float | None = None
    mdd_pct: float
    sharpe: float | None = None
    trades: int
    wins: int
    win_rate_pct: float | None = None
    profit_factor: float | None = None
    avg_return_pct: float | None = None
    avg_holding_days: float | None = None
    total_fees: float
    open_positions: int


def _drawdown_pct(curve: pl.Series) -> float:
    if curve.is_empty():
        return 0.0
    drawdown = (curve / curve.cum_max() - 1.0).min()
    return abs(cast(float, drawdown)) * 100 if drawdown is not None else 0.0


def _daily_returns(curve: pl.Series, initial_cash: float) -> pl.Series:
    full = pl.concat([pl.Series([initial_cash]), curve])
    return (full / full.shift(1) - 1.0).drop_nulls()


def _sharpe(curve: pl.Series, initial_cash: float) -> float | None:
    returns = _daily_returns(curve, initial_cash)
    if returns.len() < 2:
        return None
    std = returns.std()
    if std is None or std == 0:
        return None
    mean = returns.mean()
    assert mean is not None
    return cast(float, mean) / cast(float, std) * TRADING_DAYS_PER_YEAR**0.5


class DeflatedSharpe(BaseModel):
    trials: int
    sharpe_daily: float
    expected_max_daily: float
    margin: float
    probability: float


def expected_max_sharpe(trials: int, sharpe_variance: float) -> float:
    if trials < 2 or sharpe_variance < 0:
        raise ValueError("시도 2회 이상과 음수가 아닌 분산이 필요합니다")
    return math.sqrt(sharpe_variance) * (
        (1 - EULER_MASCHERONI) * _NORMAL.inv_cdf(1 - 1 / trials)
        + EULER_MASCHERONI * _NORMAL.inv_cdf(1 - 1 / (trials * math.e))
    )


def deflated_sharpe(
    curve: pl.Series,
    initial_cash: float,
    trial_sharpes: Sequence[float],
) -> DeflatedSharpe | None:
    if len(trial_sharpes) < 2:
        return None
    returns = _daily_returns(curve, initial_cash)
    observations = returns.len()
    if observations < 3:
        return None
    std = returns.std()
    if std is None or std == 0:
        return None
    mean = returns.mean()
    assert mean is not None
    sharpe = cast(float, mean) / cast(float, std)
    skew = cast(float, returns.skew() or 0.0)
    kurtosis = cast(float, returns.kurtosis(fisher=False) or 3.0)
    variance_term = 1 - skew * sharpe + (kurtosis - 1) / 4 * sharpe**2
    if variance_term <= 0:
        return None
    star = expected_max_sharpe(len(trial_sharpes), statistics.variance(trial_sharpes))
    z = (sharpe - star) * math.sqrt(observations - 1) / math.sqrt(variance_term)
    return DeflatedSharpe(
        trials=len(trial_sharpes),
        sharpe_daily=sharpe,
        expected_max_daily=star,
        margin=sharpe - star,
        probability=_NORMAL.cdf(z),
    )


def summarize(
    equity: pl.DataFrame,
    trades: pl.DataFrame,
    *,
    initial_cash: float,
    open_positions: int,
) -> BacktestStats:
    if equity.is_empty():
        return BacktestStats(
            initial_cash=initial_cash,
            final_equity=initial_cash,
            total_return_pct=0.0,
            mdd_pct=0.0,
            trades=0,
            wins=0,
            total_fees=0.0,
            open_positions=open_positions,
        )
    curve = equity["equity"]
    start: date = equity.item(0, "day")
    end: date = equity.item(equity.height - 1, "day")
    final = float(curve[-1])
    span_days = (end - start).days
    cagr = (
        ((final / initial_cash) ** (365.25 / span_days) - 1.0) * 100
        if span_days > 0 and final > 0
        else None
    )
    wins = trades.filter(pl.col("pnl") > 0).height
    gross_win = float(trades.filter(pl.col("pnl") > 0)["pnl"].sum())
    gross_loss = abs(float(trades.filter(pl.col("pnl") < 0)["pnl"].sum()))
    return BacktestStats(
        start=start,
        end=end,
        initial_cash=initial_cash,
        final_equity=final,
        total_return_pct=(final / initial_cash - 1.0) * 100,
        cagr_pct=cagr,
        mdd_pct=_drawdown_pct(pl.concat([pl.Series([initial_cash]), curve])),
        sharpe=_sharpe(curve, initial_cash),
        trades=trades.height,
        wins=wins,
        win_rate_pct=wins / trades.height * 100 if trades.height else None,
        profit_factor=gross_win / gross_loss if gross_loss > 0 else None,
        avg_return_pct=cast(float, trades["return_pct"].mean()) * 100 if trades.height else None,
        avg_holding_days=cast(float, trades["holding_days"].mean()) if trades.height else None,
        total_fees=float(trades["fees"].sum()) if trades.height else 0.0,
        open_positions=open_positions,
    )

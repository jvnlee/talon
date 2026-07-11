import warnings
from pathlib import Path
from typing import Any

import polars as pl

from talon.backtest.engine import BacktestResult
from talon.errors import TalonError


def _require_quantstats() -> Any:
    try:
        import quantstats
    except ImportError as exc:
        raise TalonError(
            "quantstats가 설치되어 있지 않습니다 (uv sync --group dev 후 재시도)"
        ) from exc
    return quantstats


def daily_returns(equity: pl.DataFrame, initial_cash: float) -> Any:
    import pandas as pd

    if equity.is_empty():
        raise ValueError("빈 에쿼티 곡선으로는 수익률을 만들 수 없습니다")
    curve = equity.sort("day")
    values = curve["equity"].to_list()
    previous = [initial_cash, *values[:-1]]
    index = pd.DatetimeIndex(curve["day"].to_list())
    return pd.Series(
        [value / prev - 1.0 for value, prev in zip(values, previous, strict=True)],
        index=index,
    )


def write_tearsheet(
    result: BacktestResult,
    path: Path,
    *,
    title: str = "talon backtest",
) -> Path:
    quantstats = _require_quantstats()
    returns = daily_returns(result.equity, result.stats.initial_cash)
    path.parent.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        quantstats.reports.html(returns, output=str(path), title=title)
    return path

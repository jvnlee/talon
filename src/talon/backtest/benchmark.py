from datetime import date
from typing import cast

import polars as pl
from pydantic import BaseModel

from talon.data.store import INDEX_DAILY, ParquetStore


class BenchmarkStats(BaseModel):
    name: str
    start: date
    end: date
    return_pct: float
    mdd_pct: float


def load_index_daily(
    series: ParquetStore,
    name: str = "KOSPI",
    *,
    start: date | None = None,
    end: date | None = None,
) -> pl.DataFrame:
    frame = series.read(INDEX_DAILY, name)
    if frame is None:
        raise ValueError(f"{name} 지수 일봉이 없습니다 (talon index backfill 먼저 실행)")
    if start is not None:
        frame = frame.filter(pl.col("day") >= start)
    if end is not None:
        frame = frame.filter(pl.col("day") <= end)
    return frame.sort("day")


def buy_and_hold(frame: pl.DataFrame, name: str = "KOSPI") -> BenchmarkStats | None:
    if frame.height < 2:
        return None
    closes = frame["close"]
    first = float(closes[0])
    last = float(closes[-1])
    drawdown = (closes / closes.cum_max() - 1.0).min()
    return BenchmarkStats(
        name=name,
        start=frame.item(0, "day"),
        end=frame.item(frame.height - 1, "day"),
        return_pct=(last / first - 1.0) * 100,
        mdd_pct=abs(cast(float, drawdown)) * 100 if drawdown is not None else 0.0,
    )

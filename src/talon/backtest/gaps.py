from typing import cast

import polars as pl
from pydantic import BaseModel

from talon.quant.universe import TRADABLE_STOCK

GAP_QUANTILES = (0.005, 0.01, 0.05, 0.10, 0.25, 0.50)


class GapStats(BaseModel):
    strength_floor_pct: float | None
    count: int
    mean_pct: float
    quantiles_pct: dict[str, float]


def _universe_mask(size: int, min_value: float) -> pl.Expr:
    return (
        (pl.col("volume") > 0)
        & pl.col(TRADABLE_STOCK)
        & (pl.col("value") >= min_value)
        & (pl.col("value").rank(method="ordinal", descending=True).over("day") <= size)
    )


def overnight_gap_stats(
    panel: pl.DataFrame,
    *,
    universe_size: int = 300,
    min_value: float = 1_000_000_000.0,
    strength_floor_pct: float | None = None,
) -> GapStats:
    frame = (
        panel.sort("symbol", "day")
        .with_columns(
            (pl.col("open").shift(-1).over("symbol") / pl.col("close") - 1).alias("next_gap"),
            (pl.col("close") / pl.col("prev_close") - 1).alias("day_return"),
        )
        .filter(_universe_mask(universe_size, min_value) & pl.col("next_gap").is_not_null())
    )
    if strength_floor_pct is not None:
        frame = frame.filter(pl.col("day_return") >= strength_floor_pct / 100)
    gaps = frame.get_column("next_gap")
    if gaps.is_empty():
        return GapStats(
            strength_floor_pct=strength_floor_pct, count=0, mean_pct=0.0, quantiles_pct={}
        )
    quantiles = {
        f"p{quantile * 100:g}": cast(float, gaps.quantile(quantile, "linear") or 0.0) * 100
        for quantile in GAP_QUANTILES
    }
    return GapStats(
        strength_floor_pct=strength_floor_pct,
        count=gaps.len(),
        mean_pct=cast(float, gaps.mean() or 0.0) * 100,
        quantiles_pct=quantiles,
    )

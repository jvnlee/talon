import polars as pl

DEFAULT_JUMP_THRESHOLD = 0.005

FACTOR_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "factor": pl.Float64(),
}


def stepwise_factors(
    raw: pl.DataFrame,
    adjusted: pl.DataFrame,
    *,
    jump_threshold: float = DEFAULT_JUMP_THRESHOLD,
) -> pl.DataFrame:
    joined = (
        raw.select("day", pl.col("close").alias("raw_close"))
        .join(
            adjusted.select("day", pl.col("close").alias("adj_close")),
            on="day",
            how="inner",
        )
        .filter((pl.col("raw_close") > 0) & (pl.col("adj_close") > 0))
        .sort("day")
    )
    if joined.is_empty():
        return pl.DataFrame(schema=FACTOR_SCHEMA)
    return (
        joined.with_columns((pl.col("adj_close") / pl.col("raw_close")).alias("ratio"))
        .with_columns(
            ((pl.col("ratio") / pl.col("ratio").shift(1) - 1).abs() > jump_threshold)
            .fill_null(False)
            .cum_sum()
            .alias("segment")
        )
        .with_columns(pl.col("ratio").median().over("segment").alias("factor"))
        .select("day", "factor")
    )


def apply_factors(daily: pl.DataFrame, factors: pl.DataFrame) -> pl.DataFrame:
    adjusted = daily.join(factors, on="day", how="inner")
    price_columns = [c for c in ("open", "high", "low", "close") if c in daily.columns]
    return adjusted.with_columns(
        *[pl.col(column) * pl.col("factor") for column in price_columns],
        *([pl.col("volume") / pl.col("factor")] if "volume" in daily.columns else []),
    ).drop("factor")

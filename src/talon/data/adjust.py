import polars as pl

DEFAULT_JUMP_THRESHOLD = 0.005
DEFAULT_BASE_GAP_THRESHOLD = 0.005
DEFAULT_MISSED_RATIO_THRESHOLD = 0.02

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
    stepped = (
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
    return (
        raw.filter(pl.col("close") > 0)
        .select("day")
        .sort("day")
        .join(stepped, on="day", how="left")
        .with_columns(pl.col("factor").forward_fill().backward_fill())
    )


def rebase_missed_events(
    factors: pl.DataFrame,
    raw: pl.DataFrame,
    *,
    base_gap_threshold: float = DEFAULT_BASE_GAP_THRESHOLD,
    missed_ratio_threshold: float = DEFAULT_MISSED_RATIO_THRESHOLD,
) -> pl.DataFrame:
    if factors.is_empty():
        return factors
    bases = (
        raw.filter(pl.col("close") > 0)
        .sort("day")
        .with_columns(pl.col("close").shift(1).alias("prev_close"))
        .with_columns((pl.col("close") / (1 + pl.col("change_pct") / 100)).alias("implied_base"))
        .select("day", "prev_close", "implied_base")
    )
    joined = (
        factors.sort("day")
        .join(bases, on="day", how="left")
        .with_columns(pl.col("factor").shift(1).alias("prev_factor"))
        .with_columns((pl.col("implied_base") / pl.col("prev_close")).alias("required"))
        .with_columns(
            (pl.col("required") * pl.col("factor") / pl.col("prev_factor")).alias("missed")
        )
    )
    usable = (
        (pl.col("prev_close") > 0)
        & pl.col("implied_base").is_finite()
        & (pl.col("implied_base") > 0)
        & pl.col("missed").is_finite()
        & (pl.col("missed") > 0)
    )
    reevaluated = (pl.col("required") - 1).abs() > base_gap_threshold
    unadjusted = (pl.col("missed") - 1).abs() > missed_ratio_threshold
    return (
        joined.with_columns(
            pl.when(usable & reevaluated & unadjusted)
            .then(pl.col("missed"))
            .otherwise(1.0)
            .alias("bridge")
        )
        .with_columns(
            (
                pl.col("factor")
                * pl.col("bridge").reverse().cum_prod().reverse().shift(-1).fill_null(1.0)
            ).alias("factor")
        )
        .select("day", "factor")
    )


def apply_factors(daily: pl.DataFrame, factors: pl.DataFrame) -> pl.DataFrame:
    adjusted = daily.join(factors, on="day", how="inner")
    price_columns = [c for c in ("open", "high", "low", "close") if c in daily.columns]
    return adjusted.with_columns(
        *[pl.col(column) * pl.col("factor") for column in price_columns],
        *([pl.col("volume") / pl.col("factor")] if "volume" in daily.columns else []),
    ).drop("factor")

from datetime import date

import polars as pl

TICK_UNIFICATION_DAY = date(2023, 1, 25)

_PRE_KOSPI_BANDS: tuple[tuple[int, int], ...] = (
    (1_000, 1),
    (5_000, 5),
    (10_000, 10),
    (50_000, 50),
    (100_000, 100),
    (500_000, 500),
)
_PRE_KOSPI_TOP = 1_000

_PRE_KOSDAQ_BANDS: tuple[tuple[int, int], ...] = (
    (1_000, 1),
    (5_000, 5),
    (10_000, 10),
    (50_000, 50),
)
_PRE_KOSDAQ_TOP = 100

_UNIFIED_BANDS: tuple[tuple[int, int], ...] = (
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
)
_UNIFIED_TOP = 1_000


def _band_tick(price: pl.Expr, bands: tuple[tuple[int, int], ...], top: int) -> pl.Expr:
    tick: pl.Expr = pl.lit(top, dtype=pl.Float64)
    for threshold, size in reversed(bands):
        tick = pl.when(price < threshold).then(pl.lit(size, dtype=pl.Float64)).otherwise(tick)
    return tick


def tick_size_expr(price: pl.Expr, market: pl.Expr, day: pl.Expr) -> pl.Expr:
    return (
        pl.when(day >= TICK_UNIFICATION_DAY)
        .then(_band_tick(price, _UNIFIED_BANDS, _UNIFIED_TOP))
        .when(market == "KOSPI")
        .then(_band_tick(price, _PRE_KOSPI_BANDS, _PRE_KOSPI_TOP))
        .otherwise(_band_tick(price, _PRE_KOSDAQ_BANDS, _PRE_KOSDAQ_TOP))
    )


def price_limit_exprs(base: pl.Expr, market: pl.Expr, day: pl.Expr) -> tuple[pl.Expr, pl.Expr]:
    amount = pl.when(market == "KONEX").then(base * 15 / 100).otherwise(base * 3 / 10)
    tick = tick_size_expr(base, market, day)
    band = (amount / tick).floor() * tick
    return base + band, base - band

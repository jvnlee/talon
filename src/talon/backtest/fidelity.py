from typing import cast

import polars as pl
from pydantic import BaseModel

from talon.factors.engine import compute_factors
from talon.quant.signals import StrategySpec
from talon.quant.universe import TRADABLE_STOCK

SELECTION_TOP_N = 3
SETTLE_SESSIONS = 21


class SelectionOverlap(BaseModel):
    days: int
    active_days: int
    mean_jaccard: float | None
    exact_picks: int
    approx_picks: int
    common_picks: int


class FidelityReport(BaseModel):
    exact_days: int
    settled_days: int
    exact_row_pct: float
    universe_exact_row_pct: float
    price_error_abs_pct: dict[str, float]
    volume_ratio: dict[str, float]
    overlaps: dict[str, SelectionOverlap]
    settled_overlaps: dict[str, SelectionOverlap]


def approximate_panel(panel: pl.DataFrame) -> pl.DataFrame:
    return panel.with_columns(
        pl.col("close").alias("close_1510"),
        pl.col("high").alias("high_1510"),
        pl.col("low").alias("low_1510"),
        pl.col("volume").alias("volume_1510"),
        pl.lit(False).alias("intraday_exact"),
    )


def _universe_mask(size: int, min_value: float) -> pl.Expr:
    return (
        (pl.col("volume") > 0)
        & pl.col(TRADABLE_STOCK)
        & (pl.col("value") >= min_value)
        & (pl.col("value").rank(method="ordinal", descending=True).over("day") <= size)
    )


def daily_selections(
    panel: pl.DataFrame,
    spec: StrategySpec,
    *,
    top_n: int = SELECTION_TOP_N,
    universe_size: int = 300,
    min_value: float = 1_000_000_000.0,
) -> pl.DataFrame:
    frame = compute_factors(panel, spec.columns())
    entry = pl.all_horizontal(
        [
            pl.col(f"{spec.name}__entry{index}").fill_null(False)
            for index in range(len(spec.entry))
        ]
    )
    return (
        frame.filter(entry & _universe_mask(universe_size, min_value))
        .sort("day", f"{spec.name}__score", "symbol", descending=[False, True, False])
        .group_by("day", maintain_order=True)
        .head(top_n)
        .select("day", "symbol")
    )


def selection_overlap(
    exact: pl.DataFrame,
    approx: pl.DataFrame,
    days: list,
) -> SelectionOverlap:
    exact_sets = {
        day: set(symbols)
        for day, symbols in exact.group_by("day").agg("symbol").iter_rows()
    }
    approx_sets = {
        day: set(symbols)
        for day, symbols in approx.group_by("day").agg("symbol").iter_rows()
    }
    jaccards: list[float] = []
    exact_picks = approx_picks = common_picks = 0
    active_days = 0
    for day in days:
        chosen_exact = exact_sets.get(day, set())
        chosen_approx = approx_sets.get(day, set())
        union = chosen_exact | chosen_approx
        if not union:
            continue
        active_days += 1
        common = chosen_exact & chosen_approx
        jaccards.append(len(common) / len(union))
        exact_picks += len(chosen_exact)
        approx_picks += len(chosen_approx)
        common_picks += len(common)
    return SelectionOverlap(
        days=len(days),
        active_days=active_days,
        mean_jaccard=sum(jaccards) / len(jaccards) if jaccards else None,
        exact_picks=exact_picks,
        approx_picks=approx_picks,
        common_picks=common_picks,
    )


def _abs_quantiles(values: pl.Series) -> dict[str, float]:
    if values.is_empty():
        return {}
    magnitudes = values.abs()
    return {
        "p50": cast(float, magnitudes.quantile(0.5, "linear") or 0.0) * 100,
        "p90": cast(float, magnitudes.quantile(0.9, "linear") or 0.0) * 100,
        "p99": cast(float, magnitudes.quantile(0.99, "linear") or 0.0) * 100,
        "max": cast(float, magnitudes.max() or 0.0) * 100,
    }


def price_error_stats(panel: pl.DataFrame) -> dict[str, float]:
    exact = panel.filter(pl.col("intraday_exact"))
    if exact.is_empty():
        return {}
    errors = exact.select(
        ((pl.col("close") - pl.col("close_1510")) / pl.col("close_1510")).alias("error")
    ).get_column("error")
    return _abs_quantiles(errors)


def volume_ratio_stats(panel: pl.DataFrame) -> dict[str, float]:
    exact = panel.filter(pl.col("intraday_exact") & (pl.col("volume") > 0))
    if exact.is_empty():
        return {}
    ratios = exact.select(
        (pl.col("volume_1510") / pl.col("volume")).alias("ratio")
    ).get_column("ratio")
    return {
        "p10": cast(float, ratios.quantile(0.1, "linear") or 0.0),
        "p50": cast(float, ratios.quantile(0.5, "linear") or 0.0),
        "p90": cast(float, ratios.quantile(0.9, "linear") or 0.0),
    }


def exact_days(panel: pl.DataFrame) -> list:
    return sorted(
        panel.filter(pl.col("intraday_exact")).get_column("day").unique().to_list()
    )


def measure_fidelity(
    panel: pl.DataFrame,
    specs: dict[str, StrategySpec],
    *,
    universe_size: int = 300,
    min_value: float = 1_000_000_000.0,
) -> FidelityReport:
    days = exact_days(panel)
    if not days:
        raise ValueError("정확한 15:10 상태가 있는 날이 없습니다 (분봉 적재 구간을 확인하세요)")
    settled = days[SETTLE_SESSIONS:]
    approx = approximate_panel(panel)
    universe_rows = panel.filter(_universe_mask(universe_size, min_value))
    overlaps: dict[str, SelectionOverlap] = {}
    settled_overlaps: dict[str, SelectionOverlap] = {}
    for name, spec in specs.items():
        exact_sel = daily_selections(
            panel, spec, universe_size=universe_size, min_value=min_value
        ).filter(pl.col("day").is_in(days))
        approx_sel = daily_selections(
            approx, spec, universe_size=universe_size, min_value=min_value
        ).filter(pl.col("day").is_in(days))
        overlaps[name] = selection_overlap(exact_sel, approx_sel, days)
        settled_overlaps[name] = selection_overlap(
            exact_sel.filter(pl.col("day").is_in(settled)),
            approx_sel.filter(pl.col("day").is_in(settled)),
            settled,
        )
    in_window = panel.filter(pl.col("day").is_in(days))
    exact_row_share = cast(
        float, in_window.get_column("intraday_exact").cast(pl.Float64).mean() or 0.0
    )
    universe_exact_share = cast(
        float,
        universe_rows.filter(pl.col("day").is_in(days))
        .get_column("intraday_exact")
        .cast(pl.Float64)
        .mean()
        or 0.0,
    )
    return FidelityReport(
        exact_days=len(days),
        settled_days=len(settled),
        exact_row_pct=exact_row_share * 100,
        universe_exact_row_pct=universe_exact_share * 100,
        price_error_abs_pct=price_error_stats(panel),
        volume_ratio=volume_ratio_stats(panel),
        overlaps=overlaps,
        settled_overlaps=settled_overlaps,
    )

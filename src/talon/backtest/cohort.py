from datetime import date
from typing import cast

import polars as pl
from pydantic import BaseModel

from talon.factors.engine import compute_factors, warmup_periods
from talon.quant.universe import TRADABLE_STOCK

LIMIT_UP_RATIO = 0.29
MAX_GAP_CALENDAR_DAYS = 7
SURVIVAL_MIN_N = 100
SURVIVAL_MIN_T = 2.0
TERCILE_COUNT = 3

BASELINE_LABEL = "baseline"

H1_PIECES: tuple[str, ...] = (
    "Ref(Max(high, 40), 1) >= Ref(Max(high, 250), 1)",
    "Ref(Max(high, 10), 1) < Ref(Max(high, 250), 1)",
    "Min(close - Mean(close, 20) * 0.97, 10) >= 0",
    "close >= Ref(Max(high, 250), 1) * 0.95",
    "close > prev_close",
    "volume >= Ref(Mean(volume, 20), 1) * 1.5",
)

H2_PIECES: tuple[str, ...] = (
    "Ref(Max(If(close >= Ref(close, 1) * 1.10, 1, 0) * "
    "If(close - open >= (high - low) * 0.6, 1, 0), 10), 1) >= 1",
    "Min(close - Mean(close, 5), 5) >= 0",
    "close > open",
    "close > prev_close",
)

H1_NAME = "h1"
H2_NAME = "h2"

_SIGNAL_FEATURES = frozenset({"close", "open", "high", "low", "volume", "prev_close"})


class CohortStats(BaseModel):
    n: int
    mean_pct: float
    median_pct: float
    std_pct: float
    win_rate_pct: float
    p10_pct: float
    p90_pct: float


class CohortRow(BaseModel):
    label: str
    signal: str
    tercile: int | None
    baseline_label: str
    stats: CohortStats
    delta_mean_pct: float
    welch_t: float
    verdict: str


class CohortReport(BaseModel):
    start: date | None
    end: date | None
    universe_pairs: int
    baseline: CohortStats
    halt_excluded: int
    limit_up_excluded: dict[str, int]
    rows: list[CohortRow]


def _piece_names(prefix: str, pieces: tuple[str, ...]) -> list[str]:
    return [f"{prefix}_{index}" for index in range(len(pieces))]


_H1_COLUMNS = _piece_names(H1_NAME, H1_PIECES)
_H2_COLUMNS = _piece_names(H2_NAME, H2_PIECES)


def signal_factors() -> dict[str, str]:
    return {
        **dict(zip(_H1_COLUMNS, H1_PIECES, strict=True)),
        **dict(zip(_H2_COLUMNS, H2_PIECES, strict=True)),
    }


def signal_warmup() -> int:
    return max(warmup_periods(signal_factors(), set(_SIGNAL_FEATURES)).values())


def _all_true(names: list[str]) -> pl.Expr:
    return pl.all_horizontal([pl.col(name).fill_null(False) for name in names])


def with_signals(panel: pl.DataFrame) -> pl.DataFrame:
    augmented = compute_factors(panel, signal_factors())
    return augmented.with_columns(
        _all_true(_H1_COLUMNS).alias(H1_NAME),
        _all_true(_H2_COLUMNS).alias(H2_NAME),
    ).drop(_H1_COLUMNS + _H2_COLUMNS)


def _universe_mask(size: int, min_value: float) -> pl.Expr:
    return (
        (pl.col("volume") > 0)
        & pl.col(TRADABLE_STOCK)
        & (pl.col("value") >= min_value)
        & (pl.col("value").rank(method="ordinal", descending=True).over("day") <= size)
    )


def _f(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _describe(gaps: pl.Series) -> tuple[CohortStats, float, float]:
    n = gaps.len()
    if n == 0:
        return CohortStats(
            n=0,
            mean_pct=0.0,
            median_pct=0.0,
            std_pct=0.0,
            win_rate_pct=0.0,
            p10_pct=0.0,
            p90_pct=0.0,
        ), 0.0, 0.0
    mean = _f(gaps.mean())
    variance = _f(gaps.var())
    stats = CohortStats(
        n=n,
        mean_pct=mean * 100,
        median_pct=_f(gaps.median()) * 100,
        std_pct=_f(gaps.std()) * 100,
        win_rate_pct=_f((gaps > 0).mean()) * 100,
        p10_pct=_f(gaps.quantile(0.10, "linear")) * 100,
        p90_pct=_f(gaps.quantile(0.90, "linear")) * 100,
    )
    return stats, mean, variance


def _welch_t(
    mean_a: float, var_a: float, n_a: int, mean_b: float, var_b: float, n_b: int
) -> float:
    if n_a < 2 or n_b < 2:
        return 0.0
    standard_error = (var_a / n_a + var_b / n_b) ** 0.5
    if standard_error == 0:
        return 0.0
    return (mean_a - mean_b) / standard_error


def _verdict(*, context: bool, n: int, delta: float, welch_t: float) -> str:
    if context:
        return "문맥"
    if n < SURVIVAL_MIN_N:
        return "보류"
    if delta > 0 and welch_t >= SURVIVAL_MIN_T:
        return "생존"
    return "탈락"


def _row(
    *,
    label: str,
    signal: str,
    tercile: int | None,
    cohort: pl.Series,
    baseline_gaps: pl.Series,
    baseline_stat: tuple[CohortStats, float, float],
    baseline_label: str,
    context: bool,
) -> CohortRow:
    cohort_stats, cohort_mean, cohort_var = _describe(cohort)
    _, baseline_mean, baseline_var = baseline_stat
    delta = cohort_mean - baseline_mean
    welch_t = _welch_t(
        cohort_mean, cohort_var, cohort.len(), baseline_mean, baseline_var, baseline_gaps.len()
    )
    return CohortRow(
        label=label,
        signal=signal,
        tercile=tercile,
        baseline_label=baseline_label,
        stats=cohort_stats,
        delta_mean_pct=delta * 100,
        welch_t=welch_t,
        verdict=_verdict(context=context, n=cohort.len(), delta=delta, welch_t=welch_t),
    )


def _assign_terciles(pool: pl.DataFrame) -> pl.DataFrame:
    capped = pool.filter(pl.col("cap").is_not_null())
    if capped.is_empty():
        return capped.with_columns(pl.lit(None, dtype=pl.Int32).alias("_tercile"))
    ranked = capped.with_columns(
        pl.col("cap").rank("min").over("day").alias("_rank"),
        pl.len().over("day").alias("_n_day"),
    )
    lower = pl.col("_n_day") / TERCILE_COUNT
    upper = 2 * pl.col("_n_day") / TERCILE_COUNT
    return ranked.with_columns(
        pl.when(pl.col("_rank") <= lower)
        .then(1)
        .when(pl.col("_rank") <= upper)
        .then(2)
        .otherwise(3)
        .alias("_tercile")
    )


def diagnose_cohorts(
    panel: pl.DataFrame,
    *,
    start: date | None = None,
    end: date | None = None,
    universe_size: int = 300,
    min_value: float = 1_000_000_000.0,
) -> CohortReport:
    signals = with_signals(panel).sort("symbol", "day")
    frame = signals.with_columns(
        pl.col("open").shift(-1).over("symbol").alias("_next_open"),
        pl.col("day").shift(-1).over("symbol").alias("_next_day"),
    ).with_columns(
        (pl.col("_next_open") / pl.col("close") - 1).alias("_gap"),
        (pl.col("close") / pl.col("prev_close") - 1).alias("_day_return"),
        _universe_mask(universe_size, min_value).alias("_in_universe"),
    )
    frame = frame.with_columns(
        (pl.col("_next_day") - pl.col("day")).dt.total_days().alias("_gap_days")
    )
    if start is not None:
        frame = frame.filter(pl.col("day") >= start)
    if end is not None:
        frame = frame.filter(pl.col("day") <= end)

    universe = frame.filter(pl.col("_in_universe"))
    has_next = pl.col("_next_day").is_not_null()
    within_window = pl.col("_gap_days") <= MAX_GAP_CALENDAR_DAYS
    beyond_window = pl.col("_gap_days") > MAX_GAP_CALENDAR_DAYS
    halt_excluded = universe.filter(has_next & beyond_window).height
    pool = universe.filter(has_next & within_window & pl.col("_gap").is_not_null())

    not_limit_up = pl.col("_day_return") < LIMIT_UP_RATIO
    limit_up_excluded = {
        H1_NAME: pool.filter(pl.col(H1_NAME) & (pl.col("_day_return") >= LIMIT_UP_RATIO)).height,
        H2_NAME: pool.filter(pl.col(H2_NAME) & (pl.col("_day_return") >= LIMIT_UP_RATIO)).height,
    }

    baseline_gaps = pool.get_column("_gap")
    baseline_stat = _describe(baseline_gaps)
    capped = _assign_terciles(pool)

    rows: list[CohortRow] = []
    for signal in (H1_NAME, H2_NAME):
        cohort = pool.filter(pl.col(signal) & not_limit_up).get_column("_gap")
        rows.append(
            _row(
                label=signal,
                signal=signal,
                tercile=None,
                cohort=cohort,
                baseline_gaps=baseline_gaps,
                baseline_stat=baseline_stat,
                baseline_label=BASELINE_LABEL,
                context=False,
            )
        )
    tercile_baselines: dict[int, tuple[pl.Series, tuple[CohortStats, float, float]]] = {}
    for tercile in range(1, TERCILE_COUNT + 1):
        gaps = capped.filter(pl.col("_tercile") == tercile).get_column("_gap")
        tercile_baselines[tercile] = (gaps, _describe(gaps))
    for signal in (H1_NAME, H2_NAME):
        for tercile in range(1, TERCILE_COUNT + 1):
            cohort = capped.filter(
                pl.col(signal) & not_limit_up & (pl.col("_tercile") == tercile)
            ).get_column("_gap")
            base_gaps, base_stat = tercile_baselines[tercile]
            rows.append(
                _row(
                    label=f"{signal}_cap{tercile}",
                    signal=signal,
                    tercile=tercile,
                    cohort=cohort,
                    baseline_gaps=base_gaps,
                    baseline_stat=base_stat,
                    baseline_label=f"{BASELINE_LABEL}_cap{tercile}",
                    context=False,
                )
            )
    for tercile in range(1, TERCILE_COUNT + 1):
        base_gaps, _ = tercile_baselines[tercile]
        rows.append(
            _row(
                label=f"{BASELINE_LABEL}_cap{tercile}",
                signal=BASELINE_LABEL,
                tercile=tercile,
                cohort=base_gaps,
                baseline_gaps=baseline_gaps,
                baseline_stat=baseline_stat,
                baseline_label=BASELINE_LABEL,
                context=True,
            )
        )

    days = pool.get_column("day")
    return CohortReport(
        start=cast(date, days.min()) if pool.height else None,
        end=cast(date, days.max()) if pool.height else None,
        universe_pairs=universe.height,
        baseline=baseline_stat[0],
        halt_excluded=halt_excluded,
        limit_up_excluded=limit_up_excluded,
        rows=rows,
    )

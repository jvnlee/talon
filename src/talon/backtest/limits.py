import polars as pl
from pydantic import BaseModel

from talon.markets.kr_limits import TICK_UNIFICATION_DAY, tick_size_expr

_EPSILON = 1e-6
_SUSPECT_MAX_TICKS = 2


class LimitsEraStats(BaseModel):
    checked: int
    close_at_upper: int
    close_at_lower: int
    touched_upper: int
    touched_lower: int
    violations: int


class LimitsAuditReport(BaseModel):
    rows: int
    checked: int
    skipped: int
    upper_violations: int
    lower_violations: int
    no_limit_session_violations: int
    rule_suspects: int
    suspect_symbols: list[str]
    eras: dict[str, LimitsEraStats]
    samples: list[dict[str, str]]


def _era_stats(frame: pl.DataFrame) -> LimitsEraStats:
    return LimitsEraStats(
        checked=frame.height,
        close_at_upper=int(frame.get_column("limit_up").sum() or 0),
        close_at_lower=int(frame.get_column("limit_down").sum() or 0),
        touched_upper=int(frame.get_column("limit_up_touch").sum() or 0),
        touched_lower=int(frame.get_column("limit_down_touch").sum() or 0),
        violations=frame.filter(pl.col("_violation")).height,
    )


def audit_price_limits(
    panel: pl.DataFrame,
    *,
    delisting: pl.DataFrame | None = None,
    sample_size: int = 20,
) -> LimitsAuditReport:
    checked = panel.filter(pl.col("limit_up_price").is_not_null()).with_columns(
        tick_size_expr(pl.col("raw_prev_close"), pl.col("market"), pl.col("day")).alias("_tick"),
        (pl.col("raw_high") - pl.col("limit_up_price")).alias("_upper_excess"),
        (pl.col("limit_down_price") - pl.col("raw_low")).alias("_lower_excess"),
    )
    checked = checked.with_columns(
        ((pl.col("_upper_excess") > _EPSILON) | (pl.col("_lower_excess") > _EPSILON)).alias(
            "_violation"
        ),
        pl.max_horizontal("_upper_excess", "_lower_excess").alias("_excess"),
    )
    violations = checked.filter(pl.col("_violation"))
    delisted_symbols: set[str] = set()
    if delisting is not None and not delisting.is_empty():
        delisted_symbols = set(delisting.get_column("symbol").to_list())
    suspects = violations.filter(
        (pl.col("_excess") <= _SUSPECT_MAX_TICKS * pl.col("_tick"))
        & ~pl.col("symbol").is_in(sorted(delisted_symbols))
    )
    samples = [
        {
            "day": str(row["day"]),
            "symbol": row["symbol"],
            "market": row["market"],
            "base": f"{row['raw_prev_close']:g}",
            "high": f"{row['raw_high']:g}",
            "low": f"{row['raw_low']:g}",
            "upper": f"{row['limit_up_price']:g}",
            "lower": f"{row['limit_down_price']:g}",
        }
        for row in suspects.sort("day").head(sample_size).iter_rows(named=True)
    ]
    eras = {
        "pre_unification": _era_stats(checked.filter(pl.col("day") < TICK_UNIFICATION_DAY)),
        "post_unification": _era_stats(checked.filter(pl.col("day") >= TICK_UNIFICATION_DAY)),
    }
    return LimitsAuditReport(
        rows=panel.height,
        checked=checked.height,
        skipped=panel.height - checked.height,
        upper_violations=violations.filter(pl.col("_upper_excess") > _EPSILON).height,
        lower_violations=violations.filter(pl.col("_lower_excess") > _EPSILON).height,
        no_limit_session_violations=violations.height - suspects.height,
        rule_suspects=suspects.height,
        suspect_symbols=sorted(suspects.get_column("symbol").unique().to_list()),
        eras=eras,
        samples=samples,
    )

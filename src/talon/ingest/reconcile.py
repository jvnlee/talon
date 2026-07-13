import logging
from datetime import date

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import DAILY_CANDLES, MARKET_CAP, DatePartitionedStore
from talon.errors import SourceError
from talon.markets.kr import KrxCalendar
from talon.models import ReconcileDay, ReconcileSummary
from talon.notify.telegram import Alerter
from talon.sources.krx_openapi import KrxOpenApiSource

log = logging.getLogger(__name__)

DAILY_FIELDS = ("open", "high", "low", "close", "volume", "value", "change_pct")
CAP_FIELDS = ("close", "cap", "volume", "value", "shares")
FIELD_TOLERANCES = {"change_pct": 0.011}
DEFAULT_TOLERANCE = 0.5


def _tolerance(field: str) -> float:
    return FIELD_TOLERANCES.get(field, DEFAULT_TOLERANCE)


def apply_official(
    ours: pl.DataFrame,
    official: pl.DataFrame,
    fields: tuple[str, ...],
) -> tuple[pl.DataFrame | None, dict[str, int], int]:
    """Fold the official snapshot into ours.

    Every symbol the official source covers is overwritten with its values. Symbols we
    hold that the official source does not cover (ETN and friends) are left untouched.
    Returns (merged, per-field correction counts, added rows) with merged None when
    nothing changed.
    """
    renamed = official.select(["symbol", *fields]).rename({f: f"_{f}" for f in fields})
    joined = ours.join(renamed, on="symbol", how="left")

    corrections: dict[str, int] = {}
    for field in fields:
        differs = pl.col(f"_{field}").is_not_null() & (
            pl.col(field).is_null()
            | ((pl.col(field) - pl.col(f"_{field}")).abs() > _tolerance(field))
        )
        count = joined.filter(differs).height
        if count:
            corrections[field] = count

    added = official.join(ours.select("symbol"), on="symbol", how="anti")
    if not corrections and added.is_empty():
        return None, {}, 0

    merged = joined.with_columns(
        [pl.coalesce(pl.col(f"_{field}"), pl.col(field)).alias(field) for field in fields]
    ).select(ours.columns)
    if not added.is_empty():
        merged = pl.concat([merged, added.select(ours.columns)], how="vertical_relaxed")
    return merged.sort("symbol"), corrections, added.height


def _reconcile_day(
    source: KrxOpenApiSource,
    snapshots: DatePartitionedStore,
    day: date,
) -> ReconcileDay:
    try:
        official_daily, official_caps = source.snapshot(day)
    except SourceError as exc:
        log.warning("reconcile failed for %s: %s", day, exc)
        return ReconcileDay(day=day, status="error", detail=str(exc))

    if official_daily.is_empty():
        return ReconcileDay(day=day, status="unavailable")

    ours = snapshots.read_date(DAILY_CANDLES, day)
    if ours is None:
        snapshots.write_date(DAILY_CANDLES, day, official_daily)
        snapshots.write_date(MARKET_CAP, day, official_caps)
        return ReconcileDay(day=day, status="filled", rows=official_daily.height)

    corrections: dict[str, int] = {}

    merged, daily_corrections, added = apply_official(ours, official_daily, DAILY_FIELDS)
    if merged is not None:
        snapshots.write_date(DAILY_CANDLES, day, merged)
        corrections |= daily_corrections

    our_caps = snapshots.read_date(MARKET_CAP, day)
    if our_caps is None:
        snapshots.write_date(MARKET_CAP, day, official_caps)
        corrections["cap"] = official_caps.height
    else:
        merged_caps, cap_corrections, _ = apply_official(our_caps, official_caps, CAP_FIELDS)
        if merged_caps is not None:
            snapshots.write_date(MARKET_CAP, day, merged_caps)
            for field, count in cap_corrections.items():
                corrections[f"cap.{field}"] = count

    if not corrections and not added:
        return ReconcileDay(day=day, status="ok", rows=ours.height)
    return ReconcileDay(
        day=day,
        status="corrected",
        rows=ours.height,
        corrections=corrections,
        added=added,
    )


def reconcile_daily(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    alerter: Alerter,
    start: date,
    end: date,
    source: KrxOpenApiSource | None = None,
) -> ReconcileSummary:
    sessions = cal.sessions_between(start, end)
    run_id = state.start_job("reconcile")
    owned = source is None
    if source is None:
        source = KrxOpenApiSource(
            cfg.krx_api_key,
            base_url=cfg.krx_openapi_base_url,
            throttle=cfg.krx_openapi_throttle_seconds,
        )
    days: list[ReconcileDay] = []
    try:
        for day in sessions:
            days.append(_reconcile_day(source, snapshots, day))
    finally:
        if owned:
            source.close()

    filled = [d for d in days if d.status == "filled"]
    corrected = [d for d in days if d.status == "corrected"]
    errors = [d for d in days if d.status == "error"]
    unavailable = [d for d in days if d.status == "unavailable"]

    status = "error" if errors and not (filled or corrected) else "ok"
    if errors and (filled or corrected):
        status = "partial"
    summary = ReconcileSummary(
        status=status,
        sessions=len(sessions),
        days=days,
        filled=[d.day.isoformat() for d in filled],
        corrected=[d.day.isoformat() for d in corrected],
        unavailable=[d.day.isoformat() for d in unavailable],
        errors=[f"{d.day}: {d.detail}" for d in errors],
    )
    detail = summary.model_dump(mode="json", exclude={"days"})
    state.heartbeat("reconcile", status != "error", detail)
    state.finish_job(run_id, status != "error", detail)
    _notify(alerter, filled, corrected, errors)
    return summary


def _notify(
    alerter: Alerter,
    filled: list[ReconcileDay],
    corrected: list[ReconcileDay],
    errors: list[ReconcileDay],
) -> None:
    for day in filled:
        alerter.alert(
            f"reconcile-filled-{day.day}",
            f"{day.day} 누락 일봉을 KRX 공식 데이터로 채웠습니다 ({day.rows}종목)",
        )
    for day in corrected:
        detail = ", ".join(f"{field} {count}종목" for field, count in day.corrections.items())
        extra = f", 신규 {day.added}종목" if day.added else ""
        alerter.alert(
            f"reconcile-corrected-{day.day}",
            f"{day.day} 일봉을 KRX 공식 데이터로 교정했습니다: {detail}{extra}",
        )
    if errors:
        alerter.alert("reconcile-error", f"KRX 공식 대조 실패: {errors[0].detail}")

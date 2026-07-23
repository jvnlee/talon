import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    KR_EVENTS,
    KR_EVENTS_HISTORY,
    KR_EVENTS_HISTORY_NAME,
    KR_EVENTS_HISTORY_SCHEMA,
    KR_EVENTS_SCHEMA,
    DatePartitionedStore,
    ParquetStore,
)
from talon.markets.kr import KrxCalendar
from talon.markets.kr_events import DETAILS, TIERS, KrEvent, kr_events_between
from talon.models import KrEventsBackfillSummary, KrEventsDailySummary, KrEventsVerifyReport
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

JOB = "kr-events"
BACKFILL_START = date(2015, 1, 1)
HISTORY_SELF_HEAL_DAYS = 60


def _snapshot_frame(events: list[KrEvent], day: date, captured_at: datetime) -> pl.DataFrame:
    records = [
        {
            "day": day,
            "event_day": event.event_day,
            "category": event.category,
            "tier": TIERS[event.category],
            "source": "rule",
            "detail": DETAILS[event.category],
            "captured_at": captured_at,
        }
        for event in events
    ]
    return pl.DataFrame(records, schema=KR_EVENTS_SCHEMA)


def _history_frame(events: list[KrEvent]) -> pl.DataFrame:
    records = [
        {
            "event_key": event.event_key,
            "event_day": event.event_day,
            "category": event.category,
            "tier": TIERS[event.category],
            "source": "rule",
            "detail": DETAILS[event.category],
        }
        for event in events
    ]
    return pl.DataFrame(records, schema=KR_EVENTS_HISTORY_SCHEMA)


def _collect(
    cfg: TalonSettings,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    day: date,
    captured_at: datetime,
    *,
    backfill: bool,
) -> tuple[int, int]:
    horizon = day + timedelta(days=cfg.kr_events_forward_days)
    forward = kr_events_between(cal, day, horizon)
    snapshot_rows = 0
    if forward:
        frame = _snapshot_frame(forward, day, captured_at)
        snapshots.write_date(KR_EVENTS, day, frame)
        snapshot_rows = frame.height
    hist_start = BACKFILL_START if backfill else day - timedelta(days=HISTORY_SELF_HEAL_DAYS)
    past = kr_events_between(cal, hist_start, day)
    history_rows = 0
    if past:
        history_rows = series.upsert(
            KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME, _history_frame(past), key="event_key"
        )
    return snapshot_rows, history_rows


def backfill_kr_events(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    today: date | None = None,
    now: Callable[[], datetime] = now_utc,
) -> KrEventsBackfillSummary:
    run_id = state.start_job(JOB)
    captured_at = now()
    wall_clock = captured_at.astimezone(KST).date()
    day = min(today, wall_clock) if today is not None else wall_clock
    snapshot_rows, history_rows = _collect(
        cfg, cal, snapshots, series, day, captured_at, backfill=True
    )
    summary = KrEventsBackfillSummary(
        status="ok", day=day, snapshot_rows=snapshot_rows, history_rows=history_rows
    )
    detail = summary.model_dump(mode="json")
    state.heartbeat(JOB, True, detail)
    state.finish_job(run_id, True, detail)
    return summary


def daily_kr_events(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    today: date | None = None,
    now: Callable[[], datetime] = now_utc,
) -> KrEventsDailySummary:
    captured_at = now()
    wall_clock = captured_at.astimezone(KST).date()
    day = min(today, wall_clock) if today is not None else wall_clock
    snapshot_rows, history_rows = _collect(
        cfg, cal, snapshots, series, day, captured_at, backfill=False
    )
    return KrEventsDailySummary(
        status="ok", day=day, snapshot_rows=snapshot_rows, history_rows=history_rows
    )


def _coverage(frame: pl.DataFrame) -> str:
    if frame.is_empty():
        return "0 rows"
    low = frame.select(pl.col("event_day").min()).item()
    high = frame.select(pl.col("event_day").max()).item()
    return f"{frame.height} rows, {low}..{high}"


def _frame_issues(frame: pl.DataFrame, cal: KrxCalendar) -> list[str]:
    issues: list[str] = []
    unknown = frame.filter(~pl.col("category").is_in(list(TIERS))).height
    if unknown:
        issues.append(f"unknown-category {unknown}")
    bad_tier = 0
    for category, tier in TIERS.items():
        bad_tier += frame.filter(
            (pl.col("category") == category) & (pl.col("tier") != tier)
        ).height
    if bad_tier:
        issues.append(f"bad-tier {bad_tier}")
    non_rule = frame.filter(pl.col("source") != "rule").height
    if non_rule:
        issues.append(f"non-rule {non_rule}")
    non_session = sum(
        1 for day in frame["event_day"].unique().to_list() if not cal.is_trading_day(day)
    )
    if non_session:
        issues.append(f"non-session {non_session}")
    return issues


def verify_kr_events(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
) -> KrEventsVerifyReport:
    hist = series.read(KR_EVENTS_HISTORY, KR_EVENTS_HISTORY_NAME)
    counts: dict[str, int] = {}
    all_issues: list[str] = []
    if hist is None or hist.is_empty():
        history_status = "empty"
    else:
        counts = {
            str(row[0]): int(row[1]) for row in hist.group_by("category").len().iter_rows()
        }
        hist_issues = _frame_issues(hist, cal)
        duplicate = hist.height - hist.select("event_key").n_unique()
        if duplicate:
            hist_issues.append(f"dup-key {duplicate}")
        history_status = (
            _coverage(hist) if not hist_issues else "issues: " + "; ".join(hist_issues)
        )
        all_issues += hist_issues
    scan = snapshots.scan(KR_EVENTS)
    if scan is None:
        snapshot_status = "empty"
    else:
        snap = scan.collect()
        snap_issues = _frame_issues(snap, cal)
        stale = snap.filter(pl.col("event_day") < pl.col("day")).height
        if stale:
            snap_issues.append(f"stale-forward {stale}")
        snapshot_status = (
            _coverage(snap) if not snap_issues else "issues: " + "; ".join(snap_issues)
        )
        all_issues += snap_issues
    if all_issues:
        status = "issues"
    elif hist is None or hist.is_empty():
        status = "empty"
    else:
        status = "ok"
    return KrEventsVerifyReport(
        status=status, snapshot=snapshot_status, history=history_status, counts=counts
    )

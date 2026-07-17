import logging
from collections.abc import Callable
from datetime import date, datetime

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import DatePartitionedStore
from talon.ingest.pulse import collect_macro
from talon.markets.kr import KrxCalendar
from talon.models import BriefingSnapshotSummary
from talon.notify.telegram import Alerter
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

JOB = "briefing-snapshot"
SLOT = "07:30"


def run_briefing_snapshot(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    alerter: Alerter,
    today: date | None = None,
    force: bool = False,
    now: Callable[[], datetime] = now_utc,
) -> BriefingSnapshotSummary:
    captured_at = now()
    day = today or captured_at.astimezone(KST).date()
    if not force and not cal.is_trading_day(day):
        return BriefingSnapshotSummary(status="skipped-holiday", day=day)
    run_id = state.start_job(JOB)
    summary = BriefingSnapshotSummary(status="ok", day=day)
    try:
        status, rows = collect_macro(snapshots, SLOT, day, captured_at)
    except Exception as exc:
        log.exception("briefing snapshot failed")
        status, rows = f"error: {exc}", 0
    summary.parts["macro"] = status
    summary.rows["macro"] = rows
    if status.startswith("error"):
        summary.status = "error"
        alerter.error("briefing-snapshot-error", f"{day} 07:30 매크로 스냅샷 실패: {status}")
    ok = summary.status == "ok"
    detail = summary.model_dump(mode="json")
    state.heartbeat(JOB, ok, detail)
    state.finish_job(run_id, ok, detail)
    return summary

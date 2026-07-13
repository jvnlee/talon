import logging
from collections.abc import Callable
from datetime import date

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import STOCK_INFO, DatePartitionedStore
from talon.errors import SourceError
from talon.markets.kr import KrxCalendar
from talon.models import BackfillSummary
from talon.sources.krx_openapi import KrxOpenApiSource

log = logging.getLogger(__name__)


def backfill_stock_info(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    source: KrxOpenApiSource | None = None,
    force: bool = False,
    progress: Callable[[int, int, date], None] | None = None,
) -> BackfillSummary:
    sessions = cal.sessions_between(start, end)
    run_id = state.start_job("stock-info-backfill")
    owned = source is None
    if source is None:
        source = KrxOpenApiSource(
            cfg.krx_api_key,
            base_url=cfg.krx_openapi_base_url,
            throttle=cfg.krx_openapi_throttle_seconds,
        )
    loaded = 0
    skipped = 0
    failed: list[str] = []
    try:
        for index, day in enumerate(sessions, start=1):
            if not force and snapshots.has_date(STOCK_INFO, day):
                skipped += 1
            else:
                try:
                    frame = source.stock_info(day)
                except SourceError as exc:
                    failed.append(day.isoformat())
                    log.warning("stock info backfill failed for %s: %s", day, exc)
                else:
                    if frame.is_empty():
                        log.info("stock info empty for %s (휴장일로 추정)", day)
                    else:
                        snapshots.write_date(STOCK_INFO, day, frame)
                        loaded += 1
            if progress is not None:
                progress(index, len(sessions), day)
    finally:
        if owned:
            source.close()

    status = "ok" if not failed else "partial"
    summary = BackfillSummary(
        status=status,
        sessions=len(sessions),
        loaded=loaded,
        skipped=skipped,
        failed=failed,
    )
    state.finish_job(run_id, status == "ok", summary.model_dump(mode="json"))
    return summary

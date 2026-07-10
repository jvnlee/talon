import logging
from collections.abc import Callable
from datetime import date

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import DAILY_CANDLES, MARKET_CAP, DatePartitionedStore
from talon.errors import SourceError
from talon.markets.kr import KrxCalendar
from talon.models import BackfillSummary
from talon.sources.marcap_daily import MarcapSource

log = logging.getLogger(__name__)

DailyFetcher = Callable[[date], tuple[pl.DataFrame, pl.DataFrame]]


def backfill_daily(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    fetch: DailyFetcher | None = None,
    progress: Callable[[int, int, date], None] | None = None,
) -> BackfillSummary:
    sessions = cal.sessions_between(start, end)
    run_id = state.start_job("backfill-daily")
    source: MarcapSource | None = None
    if fetch is None:
        source = MarcapSource(cfg.marcap_cache_dir)
        fetch = source.snapshot
    loaded = 0
    skipped = 0
    failed: list[str] = []
    try:
        for index, day in enumerate(sessions, start=1):
            if snapshots.has_date(DAILY_CANDLES, day) and snapshots.has_date(MARKET_CAP, day):
                skipped += 1
            else:
                try:
                    daily, caps = fetch(day)
                    snapshots.write_date(DAILY_CANDLES, day, daily)
                    snapshots.write_date(MARKET_CAP, day, caps)
                    loaded += 1
                except SourceError as exc:
                    failed.append(day.isoformat())
                    log.warning("backfill failed for %s: %s", day, exc)
            if progress is not None:
                progress(index, len(sessions), day)
    finally:
        if source is not None:
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

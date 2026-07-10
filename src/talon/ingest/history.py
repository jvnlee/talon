import logging
import time
from collections.abc import Callable
from datetime import date

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import DAILY_CANDLES, MARKET_CAP, DatePartitionedStore
from talon.errors import SourceError
from talon.markets.kr import KrxCalendar
from talon.models import BackfillSummary
from talon.sources.krx_daily import fetch_daily_ohlcv, fetch_market_cap

log = logging.getLogger(__name__)


def backfill_daily(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[int, int, date], None] | None = None,
) -> BackfillSummary:
    sessions = cal.sessions_between(start, end)
    run_id = state.start_job("backfill-daily")
    loaded = 0
    skipped = 0
    failed: list[str] = []
    for index, day in enumerate(sessions, start=1):
        if snapshots.has_date(DAILY_CANDLES, day) and snapshots.has_date(MARKET_CAP, day):
            skipped += 1
        else:
            try:
                snapshots.write_date(DAILY_CANDLES, day, fetch_daily_ohlcv(day))
                snapshots.write_date(MARKET_CAP, day, fetch_market_cap(day))
                loaded += 1
                sleep(cfg.backfill_sleep_seconds)
            except SourceError as exc:
                failed.append(day.isoformat())
                log.warning("backfill failed for %s: %s", day, exc)
        if progress is not None:
            progress(index, len(sessions), day)
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

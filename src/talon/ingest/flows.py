import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import INVESTOR_FLOWS, DatePartitionedStore
from talon.errors import SourceError
from talon.markets.kr import KrxCalendar
from talon.models import BackfillSummary
from talon.sources.krx_daily import KrxCredentials
from talon.sources.krx_flows import clearing_residual_pct, fetch_investor_flows
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

CONFIRMED_READY = time(18, 5)
CLEARING_TOLERANCE_PCT = 0.01
MAX_CONSECUTIVE_FAILURES = 3
DAILY_LOOKBACK_SESSIONS = 7

FlowsFetcher = Callable[[date], pl.DataFrame]


def _default_fetcher(cfg: TalonSettings) -> FlowsFetcher:
    credentials = KrxCredentials(cfg.krx_id, cfg.krx_password)

    def fetch(day: date) -> pl.DataFrame:
        return fetch_investor_flows(day, credentials=credentials, pause=cfg.krx_flows_pause_seconds)

    return fetch


def _check_clearing(day: date, frame: pl.DataFrame) -> None:
    residual = clearing_residual_pct(frame)
    if residual > CLEARING_TOLERANCE_PCT:
        log.warning("%s 수급 시장청산 잔차 %.4f%% (기대 0)", day, residual)


def backfill_flows(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    fetch: FlowsFetcher | None = None,
    progress: Callable[[int, int, date], None] | None = None,
) -> BackfillSummary:
    sessions = cal.sessions_between(start, end)
    run_id = state.start_job("backfill-flows")
    if fetch is None:
        fetch = _default_fetcher(cfg)
    loaded = 0
    skipped = 0
    failed: list[str] = []
    streak = 0
    aborted = False
    for index, day in enumerate(sessions, start=1):
        if snapshots.has_date(INVESTOR_FLOWS, day):
            skipped += 1
        else:
            try:
                frame = fetch(day)
                _check_clearing(day, frame)
                snapshots.write_date(INVESTOR_FLOWS, day, frame)
                loaded += 1
                streak = 0
            except SourceError as exc:
                failed.append(day.isoformat())
                streak += 1
                log.warning("flows backfill failed for %s: %s", day, exc)
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    log.error("연속 %d일 실패로 수급 백필을 중단합니다", streak)
        if progress is not None:
            progress(index, len(sessions), day)
        if aborted:
            break
    status = "aborted" if aborted else ("ok" if not failed else "partial")
    summary = BackfillSummary(
        status=status,
        sessions=len(sessions),
        loaded=loaded,
        skipped=skipped,
        failed=failed,
    )
    state.finish_job(run_id, status == "ok", summary.model_dump(mode="json"))
    return summary


def daily_flows(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    now: datetime | None = None,
    lookback_sessions: int = DAILY_LOOKBACK_SESSIONS,
    fetch: FlowsFetcher | None = None,
) -> str:
    moment = (now or now_utc()).astimezone(KST)
    today = moment.date()
    end = cal.latest_trading_day(today)
    sessions = cal.sessions_between(end - timedelta(days=lookback_sessions * 2 + 7), end)
    recent = sessions[-lookback_sessions:]
    eligible = [day for day in recent if day < today or moment.time() >= CONFIRMED_READY]
    missing = [day for day in eligible if not snapshots.has_date(INVESTOR_FLOWS, day)]
    if not missing:
        return "up-to-date"
    if fetch is None:
        fetch = _default_fetcher(cfg)
    done = 0
    rows = 0
    errors: list[str] = []
    for day in missing:
        try:
            frame = fetch(day)
            _check_clearing(day, frame)
            snapshots.write_date(INVESTOR_FLOWS, day, frame)
            done += 1
            rows += frame.height
        except SourceError as exc:
            errors.append(f"{day}: {exc}")
            log.warning("daily flows failed for %s: %s", day, exc)
    result = f"{done}/{len(missing)} days, {rows} rows"
    if errors:
        result += f", errors: {len(errors)}"
    return result

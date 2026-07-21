import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dtime

import polars as pl

from talon.config import TalonSettings
from talon.data.store import VKOSPI_1D, VKOSPI_1D_SCHEMA, ParquetStore
from talon.errors import SourceError
from talon.markets.kr import KrxCalendar
from talon.models import VkospiBackfillSummary, VkospiStatusReport
from talon.sources.krx_daily import KrxCredentials
from talon.sources.krx_index import (
    VKOSPI_SANE_RANGE,
    VkospiDailyBar,
    fetch_vkospi_daily,
)
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

VKOSPI_NAME = "VKOSPI"
VKOSPI_SOURCE = "krx"
VKOSPI_READY = dtime(16, 0)
MAX_CONSECUTIVE_FAILURES = 3
DAILY_LOOKBACK_SESSIONS = 7
CHAIN_TOLERANCE = 0.005

VkospiFetcher = Callable[[date], VkospiDailyBar]


def _default_fetcher(cfg: TalonSettings) -> VkospiFetcher:
    credentials = KrxCredentials(cfg.krx_id, cfg.krx_password)

    def fetch(day: date) -> VkospiDailyBar:
        return fetch_vkospi_daily(day, credentials=credentials)

    return fetch


def _stored_days(series: ParquetStore) -> set[date]:
    frame = series.read(VKOSPI_1D, VKOSPI_NAME)
    if frame is None:
        return set()
    return set(frame["day"].to_list())


def _bars_frame(bars: list[VkospiDailyBar], fetched_at: datetime) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "day": bar.day,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "change": bar.change,
                "change_pct": bar.change_pct,
                "source": VKOSPI_SOURCE,
                "fetched_at": fetched_at,
            }
            for bar in bars
        ],
        schema=VKOSPI_1D_SCHEMA,
    )


def find_chain_violations(frame: pl.DataFrame, cal: KrxCalendar) -> list[str]:
    ordered = frame.sort("day")
    days = ordered["day"].to_list()
    if len(days) < 2:
        return []
    closes = ordered["close"].to_list()
    changes = ordered["change"].to_list()
    position = {day: index for index, day in enumerate(cal.sessions_between(days[0], days[-1]))}
    violations: list[str] = []
    for index in range(1, len(days)):
        change = changes[index]
        if change is None:
            continue
        current = position.get(days[index])
        previous = position.get(days[index - 1])
        if current is None or previous is None or current - previous != 1:
            continue
        if abs(closes[index - 1] - (closes[index] - change)) > CHAIN_TOLERANCE:
            violations.append(days[index].isoformat())
    return violations


def _range_violations(frame: pl.DataFrame) -> list[str]:
    low_bound, high_bound = VKOSPI_SANE_RANGE
    violations: list[str] = []
    for row in frame.iter_rows(named=True):
        close = row["close"]
        if close is None or not low_bound <= close <= high_bound:
            violations.append(row["day"].isoformat())
            continue
        opened, high, low = row["open"], row["high"], row["low"]
        if None not in (opened, high, low) and (
            low > min(opened, close) or high < max(opened, close)
        ):
            violations.append(row["day"].isoformat())
    return violations


def backfill_vkospi(
    cfg: TalonSettings,
    series: ParquetStore,
    cal: KrxCalendar,
    *,
    start: date,
    end: date,
    force: bool = False,
    fetch: VkospiFetcher | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> VkospiBackfillSummary:
    sessions = cal.sessions_between(start, end)
    if fetch is None:
        fetch = _default_fetcher(cfg)
    fetched_at = now or now_utc()
    stored = set() if force else _stored_days(series)
    pending = [day for day in sessions if force or day not in stored]
    loaded = 0
    failed: list[str] = []
    streak = 0
    aborted = False
    bars: list[VkospiDailyBar] = []
    for position, day in enumerate(pending):
        try:
            bars.append(fetch(day))
            loaded += 1
            streak = 0
        except SourceError as exc:
            failed.append(day.isoformat())
            streak += 1
            log.warning("vkospi backfill failed for %s: %s", day, exc)
            if streak >= MAX_CONSECUTIVE_FAILURES:
                aborted = True
                log.error("연속 %d일 실패로 VKOSPI 백필을 중단합니다", streak)
                break
        if position < len(pending) - 1:
            sleep(cfg.krx_vkospi_pause_seconds)
    if bars:
        series.upsert(VKOSPI_1D, VKOSPI_NAME, _bars_frame(bars, fetched_at), key="day")
    stored_frame = series.read(VKOSPI_1D, VKOSPI_NAME)
    violations = find_chain_violations(stored_frame, cal) if stored_frame is not None else []
    status = "aborted" if aborted else ("ok" if not failed else "partial")
    return VkospiBackfillSummary(
        status=status,
        sessions=len(sessions),
        loaded=loaded,
        skipped=len(sessions) - len(pending),
        failed=failed,
        chain_violations=violations,
    )


def daily_vkospi(
    cfg: TalonSettings,
    *,
    series: ParquetStore,
    cal: KrxCalendar,
    now: datetime | None = None,
    lookback_sessions: int = DAILY_LOOKBACK_SESSIONS,
    fetch: VkospiFetcher | None = None,
) -> str:
    moment = (now or now_utc()).astimezone(KST)
    today = moment.date()
    end = cal.latest_trading_day(today)
    sessions = cal.sessions_between(end - timedelta(days=lookback_sessions * 2 + 7), end)
    recent = sessions[-lookback_sessions:]
    eligible = [day for day in recent if day < today or moment.time() >= VKOSPI_READY]
    stored = _stored_days(series)
    missing = [day for day in eligible if day not in stored]
    if not missing:
        return "up-to-date"
    if fetch is None:
        fetch = _default_fetcher(cfg)
    fetched_at = now or now_utc()
    done = 0
    errors: list[str] = []
    bars: list[VkospiDailyBar] = []
    for day in missing:
        try:
            bars.append(fetch(day))
            done += 1
        except SourceError as exc:
            errors.append(f"{day}: {exc}")
            log.warning("daily vkospi failed for %s: %s", day, exc)
    if bars:
        series.upsert(VKOSPI_1D, VKOSPI_NAME, _bars_frame(bars, fetched_at), key="day")
    result = f"{done}/{len(missing)} days"
    if errors:
        result += f", errors: {len(errors)}"
    return result


def vkospi_status(series: ParquetStore, cal: KrxCalendar) -> VkospiStatusReport:
    frame = series.read(VKOSPI_1D, VKOSPI_NAME)
    if frame is None or frame.is_empty():
        return VkospiStatusReport(status="empty")
    ordered = frame.sort("day")
    days = ordered["day"].to_list()
    first_day, last_day = days[0], days[-1]
    stored = set(days)
    missing = [d.isoformat() for d in cal.sessions_between(first_day, last_day) if d not in stored]
    chain = find_chain_violations(ordered, cal)
    ranges = _range_violations(ordered)
    last_fetched = ordered["fetched_at"].to_list()[-1]
    status = "ok" if not (missing or chain or ranges) else "issues"
    return VkospiStatusReport(
        status=status,
        rows=frame.height,
        first_day=first_day,
        last_day=last_day,
        last_fetched_at=last_fetched,
        missing_sessions=missing,
        chain_violations=chain,
        range_violations=ranges,
    )

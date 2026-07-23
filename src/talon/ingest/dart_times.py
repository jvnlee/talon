import logging
import time as time_module
from collections.abc import Callable
from datetime import date, datetime, timedelta

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    DART_FILING_TIMES,
    DART_FILING_TIMES_SCHEMA,
    DART_FILINGS,
    DatePartitionedStore,
)
from talon.errors import SourceError
from talon.models import (
    DartTimesBackfillSummary,
    DartTimesDailySummary,
    DartTimesVerifyReport,
)
from talon.sources.dart_web import (
    DART_WEB_HORIZON,
    DisclosureRow,
    fetch_disclosure_day,
)
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

JOB = "dart-times"
BACKFILL_START = date(2016, 7, 1)
DAILY_SELF_HEAL_DAYS = 7
MAX_CONSECUTIVE_FAILURES = 3
SOURCE = "dart_web"

DayFetcher = Callable[[date], list[DisclosureRow]]

__all__ = [
    "BACKFILL_START",
    "DART_WEB_HORIZON",
    "backfill_dart_times",
    "daily_dart_times",
    "verify_dart_times",
]


def _frame(rows: list[DisclosureRow], day: date, fetched_at: datetime) -> pl.DataFrame:
    records = [
        {
            "day": day,
            "rcept_no": row.rcept_no,
            "received_time": row.received_time,
            "corp_name": row.corp_name,
            "title": row.title,
            "source": SOURCE,
            "fetched_at": fetched_at,
        }
        for row in rows
    ]
    frame = pl.DataFrame(records, schema=DART_FILING_TIMES_SCHEMA)
    if frame.is_empty():
        return frame
    return frame.unique(subset=["rcept_no"], keep="first").sort("rcept_no")


def _default_fetcher(sleep: Callable[[float], None]) -> DayFetcher:
    def fetch(day: date) -> list[DisclosureRow]:
        return fetch_disclosure_day(day, sleep=sleep)

    return fetch


def backfill_dart_times(
    cfg: TalonSettings,
    *,
    state: StateDB,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    fetch: DayFetcher | None = None,
    sleep: Callable[[float], None] = time_module.sleep,
    now: Callable[[], datetime] = now_utc,
    progress: Callable[[int, int, date], None] | None = None,
) -> DartTimesBackfillSummary:
    run_id = state.start_job(JOB)
    if fetch is None:
        fetch = _default_fetcher(sleep)
    loaded = 0
    skipped = 0
    out_of_horizon = 0
    failed: list[str] = []
    streak = 0
    aborted = False
    total = (end - start).days + 1
    index = 0
    day = start
    while day <= end:
        index += 1
        if day < DART_WEB_HORIZON:
            out_of_horizon += 1
        elif snapshots.has_date(DART_FILING_TIMES, day):
            skipped += 1
            streak = 0
        else:
            try:
                frame = _frame(fetch(day), day, now())
                snapshots.write_date(DART_FILING_TIMES, day, frame)
                loaded += 1
                streak = 0
            except SourceError as exc:
                failed.append(day.isoformat())
                streak += 1
                log.warning("dart-times backfill failed for %s: %s", day, exc)
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    log.error("연속 %d일 실패로 DART 접수시각 백필을 중단합니다", streak)
        if progress is not None:
            progress(index, total, day)
        if aborted:
            break
        day += timedelta(days=1)
    status = "aborted" if aborted else ("ok" if not failed else "partial")
    summary = DartTimesBackfillSummary(
        status=status,
        days=index,
        loaded=loaded,
        skipped=skipped,
        out_of_horizon=out_of_horizon,
        failed=failed,
    )
    detail = summary.model_dump(mode="json")
    state.heartbeat(JOB, status != "aborted", detail)
    state.finish_job(run_id, status != "aborted", detail)
    return summary


def daily_dart_times(
    cfg: TalonSettings,
    *,
    snapshots: DatePartitionedStore,
    today: date | None = None,
    fetch: DayFetcher | None = None,
    sleep: Callable[[float], None] = time_module.sleep,
    now: Callable[[], datetime] = now_utc,
    lookback_days: int = DAILY_SELF_HEAL_DAYS,
) -> DartTimesDailySummary:
    moment = now()
    day_today = today if today is not None else moment.astimezone(KST).date()
    if fetch is None:
        fetch = _default_fetcher(sleep)
    window = [day_today - timedelta(days=offset) for offset in range(lookback_days)]
    days = sorted(day for day in window if day >= DART_WEB_HORIZON)
    written = 0
    failed: list[str] = []
    for day in days:
        try:
            frame = _frame(fetch(day), day, now())
            snapshots.write_date(DART_FILING_TIMES, day, frame)
            written += frame.height
        except SourceError as exc:
            failed.append(day.isoformat())
            log.warning("dart-times daily failed for %s: %s", day, exc)
    status = "ok" if not failed else "partial"
    return DartTimesDailySummary(status=status, days=len(days), rows=written, failed=failed)


def _valid_time() -> pl.Expr:
    return pl.col("received_time").str.contains(r"^\d{2}:\d{2}(:\d{2})?$")


def _coverage(times: pl.DataFrame, filings: pl.DataFrame) -> dict[str, str]:
    if filings.is_empty():
        return {}
    ids = times.select("rcept_no").unique()
    tagged = filings.select("day", "rcept_no").with_columns(
        pl.col("day").dt.year().alias("_year")
    )
    matched = tagged.join(ids, on="rcept_no", how="inner")
    totals = dict(tagged.group_by("_year").len().iter_rows())
    hits = dict(matched.group_by("_year").len().iter_rows())
    coverage: dict[str, str] = {}
    for year in sorted(totals):
        total = totals[year]
        hit = hits.get(year, 0)
        pct = 100.0 * hit / total if total else 0.0
        coverage[str(year)] = f"{hit}/{total} = {pct:.1f}%"
    return coverage


def verify_dart_times(
    cfg: TalonSettings,
    *,
    snapshots: DatePartitionedStore,
) -> DartTimesVerifyReport:
    scan = snapshots.scan(DART_FILING_TIMES)
    if scan is None:
        return DartTimesVerifyReport(status="empty")
    frame = scan.collect()
    if frame.is_empty():
        return DartTimesVerifyReport(status="empty")
    days = frame.select("day").n_unique()
    bad_time = frame.filter(
        pl.col("received_time").is_null() | ~_valid_time()
    ).height
    cross_day = frame.filter(
        pl.col("rcept_no").str.slice(0, 8) != pl.col("day").dt.strftime("%Y%m%d")
    ).height
    duplicate = int(
        frame.group_by("day")
        .agg((pl.len() - pl.col("rcept_no").n_unique()).alias("dup"))
        .get_column("dup")
        .sum()
    )
    filings_scan = snapshots.scan(DART_FILINGS)
    coverage = (
        _coverage(frame, filings_scan.collect()) if filings_scan is not None else {}
    )
    examples: list[str] = []
    if bad_time:
        sample = frame.filter(pl.col("received_time").is_null() | ~_valid_time()).head(3)
        examples += [
            f"bad-time {row['rcept_no']}={row['received_time']}"
            for row in sample.iter_rows(named=True)
        ]
    issues: list[str] = []
    if bad_time:
        issues.append(f"bad-time {bad_time}")
    if duplicate:
        issues.append(f"dup-key {duplicate}")
    status = "issues: " + "; ".join(issues) if issues else "ok"
    return DartTimesVerifyReport(
        status=status,
        days=days,
        rows=frame.height,
        bad_time_format=bad_time,
        cross_day_rows=cross_day,
        duplicate_keys=duplicate,
        coverage=coverage,
        examples=examples,
    )

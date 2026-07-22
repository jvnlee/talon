import logging
import time as time_module
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import NamedTuple

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    MARKET_ALERTS,
    SHORT_OVERHEAT,
    SHORT_OVERHEAT_SCHEMA,
    TRADING_HALTS,
    VI_EVENTS,
    VI_EVENTS_SCHEMA,
    DatePartitionedStore,
)
from talon.errors import SourceError
from talon.markets.kr import KrxCalendar
from talon.models import (
    ActionsBackfillSummary,
    ActionsDailySummary,
    ActionsVerifyReport,
    BackfillSummary,
)
from talon.sources.krx_actions import (
    ALERT_LEVELS,
    OVERHEAT_DTEC_TYPES,
    VI_KINDS,
    fetch_halt_history,
    fetch_market_alerts,
    fetch_short_overheat,
    fetch_trading_halts,
    fetch_vi_events,
)
from talon.sources.krx_daily import KrxCredentials
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

ALL_PARTS = ("vi", "alerts", "overheat", "halts")
BACKFILL_PARTS = ("vi", "overheat")
FORWARD_ONLY_PARTS = ("alerts", "halts")

ACTIONS_READY = time(16, 0)
DAILY_LOOKBACK_SESSIONS = 7
MAX_CONSECUTIVE_FAILURES = 3
HALTS_HISTORY_PAUSE = 0.5

VI_INSTITUTION_START = date(2014, 9, 1)
OVERHEAT_START = date(2017, 3, 27)

VI_KEY = ("symbol", "vi_kind", "trigger_time")
OVERHEAT_KEY = ("symbol",)
HALTS_KEY = ("symbol",)

VI_MIN_PER_DAY = 5.0
VI_MAX_PER_DAY = 5000.0
OVERHEAT_MAX_PER_DAY = 500.0

RangeFetcher = Callable[[date, date], pl.DataFrame]
SnapshotFetcher = Callable[[date], pl.DataFrame]
HaltsFetcher = Callable[[], pl.DataFrame]
HaltsHistoryFetcher = Callable[[str, date, date], dict[date, date]]


class ActionsFetchers(NamedTuple):
    vi: RangeFetcher
    alerts: SnapshotFetcher
    overheat: RangeFetcher
    halts: HaltsFetcher
    halt_history: HaltsHistoryFetcher


def _default_fetchers(cfg: TalonSettings) -> ActionsFetchers:
    credentials = KrxCredentials(cfg.krx_id, cfg.krx_password)
    pause = cfg.krx_flows_pause_seconds

    def vi(start: date, end: date) -> pl.DataFrame:
        return fetch_vi_events(start, end, credentials=credentials)

    def alerts(day: date) -> pl.DataFrame:
        return fetch_market_alerts(day, credentials=credentials, pause=pause)

    def overheat(start: date, end: date) -> pl.DataFrame:
        return fetch_short_overheat(start, end, credentials=credentials)

    def halts() -> pl.DataFrame:
        return fetch_trading_halts(credentials=credentials)

    def halt_history(isin: str, start: date, end: date) -> dict[date, date]:
        return fetch_halt_history(isin, start, end, credentials=credentials)

    return ActionsFetchers(vi, alerts, overheat, halts, halt_history)


def _month_end(day: date) -> date:
    if day.month == 12:
        return date(day.year, 12, 31)
    return date(day.year, day.month + 1, 1) - timedelta(days=1)


def _next_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def _month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        chunks.append((max(cursor, start), min(_month_end(cursor), end)))
        cursor = _next_month(cursor)
    return chunks


def _year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    year = start.year
    while date(year, 1, 1) <= end:
        chunks.append((max(date(year, 1, 1), start), min(date(year, 12, 31), end)))
        year += 1
    return chunks


def _split_by_day(frame: pl.DataFrame) -> dict[date, pl.DataFrame]:
    result: dict[date, pl.DataFrame] = {}
    if frame.is_empty():
        return result
    for day in frame.get_column("day").unique().to_list():
        if day is None:
            continue
        result[day] = frame.filter(pl.col("day") == day)
    return result


def backfill_actions(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    parts: tuple[str, ...] = BACKFILL_PARTS,
    fetchers: ActionsFetchers | None = None,
    progress: Callable[[int, int, date], None] | None = None,
) -> ActionsBackfillSummary:
    run_id = state.start_job("backfill-actions")
    if fetchers is None:
        fetchers = _default_fetchers(cfg)
    results: dict[str, BackfillSummary] = {}
    for part in parts:
        if part == "vi":
            results[part] = _backfill_range(
                cal,
                snapshots,
                VI_EVENTS,
                VI_EVENTS_SCHEMA,
                _month_chunks(max(start, VI_INSTITUTION_START), end),
                fetchers.vi,
                VI_KEY,
                progress,
            )
        elif part == "overheat":
            results[part] = _backfill_range(
                cal,
                snapshots,
                SHORT_OVERHEAT,
                SHORT_OVERHEAT_SCHEMA,
                _year_chunks(max(start, OVERHEAT_START), end),
                fetchers.overheat,
                OVERHEAT_KEY,
                progress,
            )
        else:
            results[part] = BackfillSummary(status="forward-only")
    if any(r.status == "aborted" for r in results.values()):
        status = "aborted"
    elif all(r.status in ("ok", "forward-only") for r in results.values()):
        status = "ok"
    else:
        status = "partial"
    summary = ActionsBackfillSummary(status=status, parts=results)
    state.finish_job(run_id, status == "ok", summary.model_dump(mode="json"))
    return summary


def _backfill_range(
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    dataset: str,
    schema: dict[str, pl.DataType],
    chunks: list[tuple[date, date]],
    fetch: RangeFetcher,
    key: tuple[str, ...],
    progress: Callable[[int, int, date], None] | None,
) -> BackfillSummary:
    empty = pl.DataFrame(schema=schema)
    loaded = 0
    skipped = 0
    failed: list[str] = []
    streak = 0
    aborted = False
    total = len(chunks)
    session_count = 0
    for index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        sessions = cal.sessions_between(chunk_start, chunk_end)
        session_count += len(sessions)
        if sessions and all(snapshots.has_date(dataset, day) for day in sessions):
            skipped += 1
            streak = 0
        else:
            try:
                by_day = _split_by_day(fetch(chunk_start, chunk_end))
                covered = set(sessions)
                for day in sessions:
                    part = by_day.get(day, empty)
                    snapshots.write_date(dataset, day, part)
                    if part.height:
                        loaded += 1
                for day, part in by_day.items():
                    if day not in covered:
                        snapshots.upsert_date(dataset, day, part, key)
                        loaded += 1
                streak = 0
            except SourceError as exc:
                failed.append(f"{chunk_start}..{chunk_end}")
                streak += 1
                log.warning("actions backfill failed for %s..%s: %s", chunk_start, chunk_end, exc)
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    log.error("연속 %d청크 실패로 시장조치 백필을 중단합니다", streak)
        if progress is not None:
            progress(index, total, chunk_end)
        if aborted:
            break
    status = "aborted" if aborted else ("ok" if not failed else "partial")
    return BackfillSummary(
        status=status, sessions=session_count, loaded=loaded, skipped=skipped, failed=failed
    )


def daily_actions(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    now: datetime | None = None,
    parts: tuple[str, ...] = ALL_PARTS,
    lookback_sessions: int = DAILY_LOOKBACK_SESSIONS,
    fetchers: ActionsFetchers | None = None,
    sleep: Callable[[float], None] = time_module.sleep,
) -> ActionsDailySummary:
    moment = (now or now_utc()).astimezone(KST)
    today = moment.date()
    end = cal.latest_trading_day(today)
    if fetchers is None:
        fetchers = _default_fetchers(cfg)
    part_msgs: dict[str, str] = {}
    part_rows: dict[str, int] = {}
    for part in parts:
        try:
            if part == "vi":
                msg, rows = _daily_range(
                    cal, snapshots, VI_EVENTS, VI_EVENTS_SCHEMA,
                    fetchers.vi, end, today, moment, lookback_sessions,
                )
            elif part == "overheat":
                msg, rows = _daily_range(
                    cal, snapshots, SHORT_OVERHEAT, SHORT_OVERHEAT_SCHEMA,
                    fetchers.overheat, end, today, moment, lookback_sessions,
                )
            elif part == "alerts":
                msg, rows = _daily_alerts(snapshots, fetchers.alerts, end, today, moment)
            elif part == "halts":
                msg, rows = _daily_halts(
                    snapshots, fetchers.halts, fetchers.halt_history, end, sleep
                )
            else:
                msg, rows = "unknown part", 0
            part_msgs[part] = msg
            part_rows[part] = rows
        except Exception as exc:
            part_msgs[part] = f"error: {exc}"
            part_rows[part] = 0
            log.warning("daily actions %s failed: %s", part, exc)
    status = "ok" if all(not m.startswith("error") for m in part_msgs.values()) else "partial"
    return ActionsDailySummary(status=status, parts=part_msgs, rows=part_rows)


def _daily_range(
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    dataset: str,
    schema: dict[str, pl.DataType],
    fetch: RangeFetcher,
    end: date,
    today: date,
    moment: datetime,
    lookback: int,
) -> tuple[str, int]:
    empty = pl.DataFrame(schema=schema)
    sessions = cal.sessions_between(end - timedelta(days=lookback * 2 + 7), end)
    recent = sessions[-lookback:]
    eligible = [day for day in recent if day < today or moment.time() >= ACTIONS_READY]
    missing = [day for day in eligible if not snapshots.has_date(dataset, day)]
    if not missing:
        return "up-to-date", 0
    by_day = _split_by_day(fetch(missing[0], missing[-1]))
    written = 0
    for day in missing:
        part = by_day.get(day)
        if part is not None and not part.is_empty():
            snapshots.write_date(dataset, day, part)
            written += part.height
        elif day < today:
            snapshots.write_date(dataset, day, empty)
    return f"{len(missing)} days, {written} rows", written


def _daily_alerts(
    snapshots: DatePartitionedStore,
    fetch: SnapshotFetcher,
    end: date,
    today: date,
    moment: datetime,
) -> tuple[str, int]:
    if end >= today and moment.time() < ACTIONS_READY:
        return "not-ready", 0
    if snapshots.has_date(MARKET_ALERTS, end):
        return "up-to-date", 0
    frame = fetch(end)
    snapshots.write_date(MARKET_ALERTS, end, frame)
    return f"{frame.height} rows", frame.height


def _daily_halts(
    snapshots: DatePartitionedStore,
    fetch: HaltsFetcher,
    fetch_history: HaltsHistoryFetcher,
    end: date,
    sleep: Callable[[float], None],
) -> tuple[str, int]:
    snapshot = fetch()
    today_symbols = (
        set(snapshot.get_column("symbol").to_list()) if not snapshot.is_empty() else set()
    )
    written = 0
    for day, part in _split_by_day(snapshot).items():
        snapshots.upsert_date(TRADING_HALTS, day, part, HALTS_KEY)
        written += part.height
    resumed = _refresh_resumes(snapshots, fetch_history, today_symbols, end, sleep)
    return f"{written} halted, {resumed} resumed", written


def _refresh_resumes(
    snapshots: DatePartitionedStore,
    fetch_history: HaltsHistoryFetcher,
    today_symbols: set[str],
    end: date,
    sleep: Callable[[float], None],
) -> int:
    scan = snapshots.scan(TRADING_HALTS)
    if scan is None:
        return 0
    open_halts = (
        scan.filter(pl.col("resume_day").is_null()).select("day", "symbol", "isin").collect()
    )
    if open_halts.is_empty():
        return 0
    dropped = open_halts.filter(~pl.col("symbol").is_in(list(today_symbols)))
    resumed = 0
    for row in dropped.iter_rows(named=True):
        isin = row["isin"]
        if not isin:
            continue
        resume = fetch_history(isin, row["day"], end).get(row["day"])
        if resume is not None:
            _fill_resume(snapshots, row["day"], row["symbol"], resume)
            resumed += 1
        sleep(HALTS_HISTORY_PAUSE)
    return resumed


def _fill_resume(
    snapshots: DatePartitionedStore,
    halt_start: date,
    symbol: str,
    resume: date,
) -> None:
    frame = snapshots.read_date(TRADING_HALTS, halt_start)
    if frame is None:
        return
    updated = frame.with_columns(
        pl.when(pl.col("symbol") == symbol)
        .then(pl.lit(resume, dtype=pl.Date))
        .otherwise(pl.col("resume_day"))
        .alias("resume_day")
    )
    snapshots.write_date(TRADING_HALTS, halt_start, updated)


def verify_actions(
    cfg: TalonSettings,
    *,
    snapshots: DatePartitionedStore,
    parts: tuple[str, ...] = ALL_PARTS,
) -> ActionsVerifyReport:
    part_status: dict[str, str] = {}
    coverage: dict[str, str] = {}
    for part in parts:
        if part == "vi":
            status, cover = _verify_vi(snapshots)
        elif part == "alerts":
            status, cover = _verify_alerts(snapshots)
        elif part == "overheat":
            status, cover = _verify_overheat(snapshots)
        elif part == "halts":
            status, cover = _verify_halts(snapshots)
        else:
            status, cover = "unknown part", ""
        part_status[part] = status
        coverage[part] = cover
    overall = "ok" if all(s in ("ok", "empty") for s in part_status.values()) else "issues"
    return ActionsVerifyReport(status=overall, parts=part_status, coverage=coverage)


def _coverage(frame: pl.DataFrame) -> str:
    days = frame.select("day").n_unique()
    low = frame.select(pl.col("day").min()).item()
    high = frame.select(pl.col("day").max()).item()
    return f"{days} days, {frame.height} rows, {low}..{high}"


def _valid_clock(text: object) -> bool:
    if not isinstance(text, str):
        return False
    try:
        datetime.strptime(text, "%H:%M:%S")
    except ValueError:
        return False
    return True


def _bad_times(frame: pl.DataFrame, column: str, required: bool) -> int:
    bad = 0
    for value in frame.get_column(column).to_list():
        if value is None:
            if required:
                bad += 1
        elif not _valid_clock(value):
            bad += 1
    return bad


def _verify_vi(snapshots: DatePartitionedStore) -> tuple[str, str]:
    scan = snapshots.scan(VI_EVENTS)
    if scan is None:
        return "empty", "no data"
    frame = scan.collect()
    if frame.is_empty():
        return "ok", "0 rows"
    issues: list[str] = []
    earliest = frame.select(pl.col("day").min()).item()
    if earliest < VI_INSTITUTION_START:
        issues.append(f"before-institution {earliest}")
    bad_kind = frame.filter(~pl.col("vi_kind").is_in(list(VI_KINDS))).height
    if bad_kind:
        issues.append(f"bad-kind {bad_kind}")
    bad_time = _bad_times(frame, "trigger_time", True) + _bad_times(frame, "release_time", False)
    if bad_time:
        issues.append(f"bad-time {bad_time}")
    per_day = frame.height / frame.select("day").n_unique()
    if not VI_MIN_PER_DAY <= per_day <= VI_MAX_PER_DAY:
        issues.append(f"rarity {per_day:.0f}/day")
    return _status(issues), _coverage(frame)


def _verify_overheat(snapshots: DatePartitionedStore) -> tuple[str, str]:
    scan = snapshots.scan(SHORT_OVERHEAT)
    if scan is None:
        return "empty", "no data"
    frame = scan.collect()
    if frame.is_empty():
        return "ok", "0 rows"
    issues: list[str] = []
    earliest = frame.select(pl.col("day").min()).item()
    if earliest < OVERHEAT_START:
        issues.append(f"before-institution {earliest}")
    unknown = frame.filter(
        pl.col("dtec_type").is_not_null() & ~pl.col("dtec_type").is_in(list(OVERHEAT_DTEC_TYPES))
    ).height
    if unknown:
        issues.append(f"unknown-type {unknown}")
    per_day = frame.height / frame.select("day").n_unique()
    if per_day > OVERHEAT_MAX_PER_DAY:
        issues.append(f"rarity {per_day:.1f}/day")
    return _status(issues), _coverage(frame)


def _verify_alerts(snapshots: DatePartitionedStore) -> tuple[str, str]:
    scan = snapshots.scan(MARKET_ALERTS)
    if scan is None:
        return "empty", "no data"
    frame = scan.collect()
    if frame.is_empty():
        return "ok", "0 rows"
    issues: list[str] = []
    bad_level = frame.filter(~pl.col("level").is_in(list(ALERT_LEVELS))).height
    if bad_level:
        issues.append(f"bad-level {bad_level}")
    no_design = frame.filter(pl.col("design_dd").is_null()).height
    if no_design:
        issues.append(f"no-design {no_design}")
    return _status(issues), _coverage(frame)


def _verify_halts(snapshots: DatePartitionedStore) -> tuple[str, str]:
    scan = snapshots.scan(TRADING_HALTS)
    if scan is None:
        return "empty", "no data"
    frame = scan.collect()
    if frame.is_empty():
        return "ok", "0 rows"
    open_count = frame.filter(pl.col("resume_day").is_null()).height
    no_reason = frame.filter(pl.col("reason").is_null()).height
    return "ok", f"{_coverage(frame)}, open {open_count}, no-reason {no_reason}"


def _status(issues: list[str]) -> str:
    return "ok" if not issues else "issues: " + "; ".join(issues)

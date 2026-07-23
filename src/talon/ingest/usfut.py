import bisect
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta

import polars as pl

from talon.data.store import (
    US_DAILY,
    US_FUTURES_1510,
    US_FUTURES_1510_SCHEMA,
    DatePartitionedStore,
    ParquetStore,
)
from talon.errors import SourceError
from talon.markets.kr import KrxCalendar
from talon.models import UsFutBackfillSummary, UsFutVerifyReport
from talon.sources.dukascopy import (
    DukascopyBar,
    ProxyBar,
    fetch_1510_bars,
    select_1510_bar,
)
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

SYMBOLS: tuple[str, ...] = ("US500", "USTEC")
SOURCE = "dukascopy_cfd"
BACKFILL_START = date(2011, 9, 19)
MISSING_STREAK_LIMIT = 14
DAILY_LOOKBACK_DAYS = 7
PACING_SECONDS = 0.3
LEVEL_BAND = 0.07
LEVEL_INDEX: dict[str, str] = {"US500": "^GSPC", "USTEC": "^IXIC"}

DukascopyFetcher = Callable[[str, date], "list[DukascopyBar] | None"]


def _default_fetch(symbol: str, day: date) -> list[DukascopyBar] | None:
    return fetch_1510_bars(symbol, day)


def _weekdays(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _recent_weekdays(today: date, count: int) -> list[date]:
    days: list[date] = []
    current = today
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    return sorted(days)


def _stored_symbols(snapshots: DatePartitionedStore, day: date) -> set[str]:
    frame = snapshots.read_date(US_FUTURES_1510, day)
    if frame is None or frame.is_empty():
        return set()
    return set(frame["symbol"].to_list())


def _row(day: date, symbol: str, bar: ProxyBar, fetched_at: datetime) -> dict[str, object]:
    return {
        "day": day,
        "symbol": symbol,
        "price": bar.close,
        "bar_ts": bar.bar_ts,
        "stale_minutes": bar.stale_minutes,
        "source": SOURCE,
        "fetched_at": fetched_at,
    }


def _write_day(
    snapshots: DatePartitionedStore, day: date, records: list[dict[str, object]]
) -> int:
    frame = pl.DataFrame(records, schema=US_FUTURES_1510_SCHEMA)
    return snapshots.upsert_date(US_FUTURES_1510, day, frame, ("symbol",))


def _long_gaps(days: list[date], unavailable: set[date]) -> list[str]:
    gaps: list[str] = []
    run: list[date] = []
    for day in days:
        if day in unavailable:
            run.append(day)
            continue
        if len(run) >= MISSING_STREAK_LIMIT:
            gaps.append(f"{run[0].isoformat()}..{run[-1].isoformat()} ({len(run)}d)")
        run = []
    if len(run) >= MISSING_STREAK_LIMIT:
        gaps.append(f"{run[0].isoformat()}..{run[-1].isoformat()} ({len(run)}d)")
    return gaps


def backfill_usfut(
    *,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    fetch: DukascopyFetcher | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    pause: float = PACING_SECONDS,
    progress: Callable[[int, int, date], None] | None = None,
) -> UsFutBackfillSummary:
    if fetch is None:
        fetch = _default_fetch
    fetched_at = now or now_utc()
    days = _weekdays(start, end)
    total = len(days)
    loaded_days = 0
    rows = 0
    skipped_days = 0
    stale_days = 0
    unavailable_days = 0
    failed: list[str] = []
    unavailable: list[date] = []
    for index, day in enumerate(days, start=1):
        stored = _stored_symbols(snapshots, day)
        if set(SYMBOLS) <= stored:
            skipped_days += 1
            if progress is not None:
                progress(index, total, day)
            continue
        records: list[dict[str, object]] = []
        available = False
        day_failed = False
        for symbol in SYMBOLS:
            if symbol in stored:
                available = True
                continue
            try:
                bars = fetch(symbol, day)
            except SourceError as exc:
                failed.append(f"{day.isoformat()} {symbol}")
                day_failed = True
                log.warning("usfut backfill failed for %s %s: %s", day, symbol, exc)
                continue
            if bars is None:
                continue
            available = True
            bar = select_1510_bar(bars, day)
            if bar is not None:
                records.append(_row(day, symbol, bar, fetched_at))
        if records:
            rows += _write_day(snapshots, day, records)
            loaded_days += 1
        elif available and not day_failed:
            stale_days += 1
        if not available and not day_failed:
            unavailable_days += 1
            unavailable.append(day)
        if pause:
            sleep(pause)
        if progress is not None:
            progress(index, total, day)
    status = "partial" if failed else "ok"
    return UsFutBackfillSummary(
        status=status,
        days=total,
        loaded_days=loaded_days,
        rows=rows,
        skipped_days=skipped_days,
        stale_days=stale_days,
        unavailable_days=unavailable_days,
        failed=failed,
        long_gaps=_long_gaps(days, set(unavailable)),
    )


def daily_usfut(
    *,
    snapshots: DatePartitionedStore,
    now: datetime | None = None,
    lookback_days: int = DAILY_LOOKBACK_DAYS,
    fetch: DukascopyFetcher | None = None,
    sleep: Callable[[float], None] = time.sleep,
    pause: float = PACING_SECONDS,
) -> str:
    today = (now or now_utc()).astimezone(KST).date()
    recent = _recent_weekdays(today, lookback_days)
    pending = [day for day in recent if not set(SYMBOLS) <= _stored_symbols(snapshots, day)]
    if not pending:
        return "up-to-date"
    if fetch is None:
        fetch = _default_fetch
    fetched_at = now or now_utc()
    done = 0
    not_ready = 0
    errors = 0
    for day in pending:
        stored = _stored_symbols(snapshots, day)
        records: list[dict[str, object]] = []
        symbol_missing = False
        for symbol in SYMBOLS:
            if symbol in stored:
                continue
            try:
                bars = fetch(symbol, day)
            except SourceError as exc:
                errors += 1
                log.warning("daily usfut failed for %s %s: %s", day, symbol, exc)
                continue
            if bars is None:
                symbol_missing = True
                continue
            bar = select_1510_bar(bars, day)
            if bar is not None:
                records.append(_row(day, symbol, bar, fetched_at))
        if records:
            _write_day(snapshots, day, records)
            done += 1
        elif symbol_missing:
            not_ready += 1
        if pause:
            sleep(pause)
    result = f"{done}/{len(pending)} days"
    if not_ready:
        result += f", not-ready {not_ready}"
    if errors:
        result += f", errors {errors}"
    return result


def _easter(year: int) -> date:
    golden = year % 19
    century, year_of_century = divmod(year, 100)
    century_div4, century_mod4 = divmod(century, 4)
    leap_correction = (century + 8) // 25
    correction = (century - leap_correction + 1) // 3
    epact = (19 * golden + century - century_div4 - correction + 15) % 30
    year_div4, year_mod4 = divmod(year_of_century, 4)
    weekday = (32 + 2 * century_mod4 + 2 * year_div4 - epact - year_mod4) % 7
    offset = (golden + 11 * epact + 22 * weekday) // 451
    month = (epact + weekday - 7 * offset + 114) // 31
    day = ((epact + weekday - 7 * offset + 114) % 31) + 1
    return date(year, month, day)


def _thanksgiving(year: int) -> date:
    first = date(year, 11, 1)
    first_thursday = first + timedelta(days=(3 - first.weekday()) % 7)
    return first_thursday + timedelta(weeks=3)


def known_cfd_closed_days(start: date, end: date, cal: KrxCalendar) -> set[date]:
    closed: set[date] = set()
    for year in range(start.year, end.year + 1):
        thanksgiving = _thanksgiving(year)
        candidates = (
            _easter(year) - timedelta(days=2),
            thanksgiving,
            thanksgiving + timedelta(days=1),
        )
        for candidate in candidates:
            if start <= candidate <= end and cal.is_trading_day(candidate):
                closed.add(candidate)
    return closed


def _level_check(
    frame: pl.DataFrame, series: ParquetStore
) -> tuple[int, int, list[str]]:
    checked = 0
    violations = 0
    examples: list[str] = []
    for symbol in SYMBOLS:
        reference = series.read(US_DAILY, LEVEL_INDEX[symbol])
        if reference is None or reference.is_empty():
            continue
        ordered = reference.select("day", "close").drop_nulls().sort("day")
        ref_days = ordered["day"].to_list()
        ref_close = ordered["close"].to_list()
        sub = frame.filter(pl.col("symbol") == symbol).select("day", "price").sort("day")
        for row in sub.iter_rows(named=True):
            position = bisect.bisect_left(ref_days, row["day"])
            if position == 0:
                continue
            close = ref_close[position - 1]
            if close == 0:
                continue
            checked += 1
            if abs(row["price"] - close) / abs(close) > LEVEL_BAND:
                violations += 1
                if len(examples) < 5:
                    examples.append(
                        f"{symbol} {row['day']}: cfd {row['price']:.1f} vs "
                        f"{LEVEL_INDEX[symbol]} {close:.1f}"
                    )
    return checked, violations, examples


def verify_usfut(
    *,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    cal: KrxCalendar,
    start: date | None = None,
    end: date | None = None,
) -> UsFutVerifyReport:
    scan = snapshots.scan(US_FUTURES_1510)
    if scan is None:
        return UsFutVerifyReport(status="empty")
    frame = scan.collect()
    if start is not None:
        frame = frame.filter(pl.col("day") >= start)
    if end is not None:
        frame = frame.filter(pl.col("day") <= end)
    if frame.is_empty():
        return UsFutVerifyReport(status="empty")

    first_day = frame.select(pl.col("day").min()).item()
    last_day = frame.select(pl.col("day").max()).item()
    covered = set(frame["day"].to_list())
    sessions = cal.sessions_between(first_day, last_day)
    known = known_cfd_closed_days(first_day, last_day, cal)
    missing: list[str] = []
    known_gaps: list[str] = []
    for session in sessions:
        if session in covered:
            continue
        if session in known:
            known_gaps.append(session.isoformat())
        else:
            missing.append(session.isoformat())

    duplicate_keys = frame.height - frame.select("day", "symbol").n_unique()
    bar_ts_violations = 0
    for symbol in SYMBOLS:
        stamps = frame.filter(pl.col("symbol") == symbol).sort("day")["bar_ts"].to_list()
        bar_ts_violations += sum(
            1 for index in range(1, len(stamps)) if stamps[index] <= stamps[index - 1]
        )

    symbols = {row[0]: int(row[1]) for row in frame.group_by("symbol").len().iter_rows()}
    stale_distribution = {
        str(offset): int(count)
        for offset, count in frame.group_by("stale_minutes")
        .len()
        .sort("stale_minutes")
        .iter_rows()
    }
    level_checked, level_violations, examples = _level_check(frame, series)

    status = (
        "issues"
        if missing or level_violations or duplicate_keys or bar_ts_violations
        else "ok"
    )
    return UsFutVerifyReport(
        status=status,
        symbols=symbols,
        first_day=first_day,
        last_day=last_day,
        coverage_sessions=len(sessions),
        missing_sessions=missing,
        known_holiday_gaps=known_gaps,
        level_checked=level_checked,
        level_violations=level_violations,
        duplicate_keys=duplicate_keys,
        bar_ts_violations=bar_ts_violations,
        stale_distribution=stale_distribution,
        examples=examples,
    )

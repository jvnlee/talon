import logging
import random
import time as time_module
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    DAILY_CANDLES,
    KIS_MINUTES,
    KIS_MINUTES_SCHEMA,
    STOCK_INFO,
    DatePartitionedStore,
)
from talon.errors import SourceError
from talon.ingest.pool import parallel_fetch
from talon.markets.kr import KrxCalendar
from talon.models import (
    KisMinutesBackfillSummary,
    KisMinutesProbeReport,
    KisMinutesVerifyReport,
)
from talon.sources.kis import KisClient, build_kis_client
from talon.sources.kis_market import fetch_minute_chart
from talon.timeutil import KST, now_utc, to_utc

log = logging.getLogger(__name__)

KisMinutesFetcher = Callable[[str, date, str], list[dict[str, Any]]]

PAUSE_START = time(14, 50)
PAUSE_END = time(15, 50)
DAILY_READY = time(16, 0)
DAILY_LOOKBACK_SESSIONS = 5
MAX_CONSECUTIVE_FAILURES = 3
MAX_FAILURE_RATIO = 0.1
PROBE_SYMBOL = "005930"
PROBE_WINDOW_DAYS = 420


def _bind_fetch(client: KisClient) -> KisMinutesFetcher:
    def fetch(symbol: str, day: date, anchor: str) -> list[dict[str, Any]]:
        return fetch_minute_chart(client, symbol, day, anchor=anchor)

    return fetch


def _anchor_for(cal: KrxCalendar, day: date) -> str:
    return cal.session_close(day).astimezone(KST).strftime("%H%M%S")


def _day_symbols(snapshots: DatePartitionedStore, day: date) -> list[str]:
    frame = snapshots.read_date(STOCK_INFO, day)
    if frame is None or frame.is_empty():
        raise SourceError(f"stock_info 없음: {day}")
    return frame["symbol"].to_list()


def _bar_ts(day: date, time_text: str) -> datetime:
    moment = datetime.combine(
        day, time(int(time_text[:2]), int(time_text[2:4]), int(time_text[4:6]))
    )
    return to_utc(moment)


def _frame_for_day(
    day: date, results: list[tuple[str, list[dict[str, Any]], datetime]]
) -> pl.DataFrame:
    records: list[dict[str, Any]] = []
    for symbol, bars, captured_at in results:
        for bar in bars:
            records.append(
                {
                    "day": day,
                    "symbol": symbol,
                    "ts": _bar_ts(day, bar["time"]),
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "cum_value": bar["cum_value"],
                    "fetched_at": captured_at,
                }
            )
    frame = pl.DataFrame(records, schema=KIS_MINUTES_SCHEMA)
    return frame.sort(["symbol", "ts"])


def _pause_if_live_window(
    cal: KrxCalendar,
    now_fn: Callable[[], datetime],
    sleep_fn: Callable[[float], None],
) -> None:
    moment = now_fn().astimezone(KST)
    if not cal.is_trading_day(moment.date()):
        return
    if PAUSE_START <= moment.time() < PAUSE_END:
        target = datetime.combine(moment.date(), PAUSE_END, tzinfo=KST)
        wait = (target - moment).total_seconds()
        if wait > 0:
            log.info("장중 보호창: %s까지 %.0f초 대기", PAUSE_END, wait)
            sleep_fn(wait)


def _collect_day(
    cal: KrxCalendar,
    day: date,
    symbols: list[str],
    fetch: KisMinutesFetcher,
    workers: int,
) -> pl.DataFrame:
    anchor = _anchor_for(cal, day)

    def one(symbol: str) -> list[dict[str, Any]]:
        return fetch(symbol, day, anchor)

    results, _ = parallel_fetch(
        symbols,
        one,
        workers=workers,
        max_failure_ratio=MAX_FAILURE_RATIO,
        log_name="kis-minutes",
    )
    return _frame_for_day(day, results)


def _load_day(
    cfg: TalonSettings,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    day: date,
    fetch: KisMinutesFetcher,
) -> int:
    symbols = _day_symbols(snapshots, day)
    frame = _collect_day(cal, day, symbols, fetch, cfg.kis_workers)
    if frame.is_empty():
        raise SourceError(f"분봉 0행: {day}")
    snapshots.write_date(KIS_MINUTES, day, frame)
    return frame.height


def backfill_kis_minutes(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    start: date | None,
    end: date,
    fetch: KisMinutesFetcher | None = None,
    progress: Callable[[int, int, date], None] | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time_module.sleep,
    force: bool = False,
) -> KisMinutesBackfillSummary:
    run_id = state.start_job("kis-minutes-backfill")
    now_fn: Callable[[], datetime] = now or now_utc
    if fetch is None:
        with build_kis_client(cfg) as client:
            summary = _run_backfill(
                cfg, cal, snapshots, start, end, _bind_fetch(client), progress, now_fn, sleep, force
            )
    else:
        summary = _run_backfill(
            cfg, cal, snapshots, start, end, fetch, progress, now_fn, sleep, force
        )
    state.finish_job(run_id, summary.status == "ok", summary.model_dump(mode="json"))
    return summary


def _run_backfill(
    cfg: TalonSettings,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    start: date | None,
    end: date,
    fetch: KisMinutesFetcher,
    progress: Callable[[int, int, date], None] | None,
    now_fn: Callable[[], datetime],
    sleep: Callable[[float], None],
    force: bool,
) -> KisMinutesBackfillSummary:
    if start is None:
        probe = _probe_cliff(cal, fetch, now_fn())
        if probe.status != "ok" or probe.cliff is None:
            return KisMinutesBackfillSummary(status="no-data")
        start = probe.cliff
    sessions = cal.sessions_between(start, end)
    loaded = 0
    skipped = 0
    rows = 0
    failed: list[str] = []
    streak = 0
    aborted = False
    for index, day in enumerate(sessions, start=1):
        _pause_if_live_window(cal, now_fn, sleep)
        if not force and snapshots.has_date(KIS_MINUTES, day):
            skipped += 1
        else:
            try:
                rows += _load_day(cfg, cal, snapshots, day, fetch)
                loaded += 1
                streak = 0
            except SourceError as exc:
                failed.append(day.isoformat())
                streak += 1
                log.warning("kis-minutes backfill failed for %s: %s", day, exc)
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    log.error("연속 %d일 실패로 분봉 백필을 중단합니다", streak)
        if progress is not None:
            progress(index, len(sessions), day)
        if aborted:
            break
    status = "aborted" if aborted else ("ok" if not failed else "partial")
    return KisMinutesBackfillSummary(
        status=status,
        sessions=len(sessions),
        loaded=loaded,
        skipped=skipped,
        rows=rows,
        failed=failed,
    )


def daily_kis_minutes(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    now: datetime | None = None,
    lookback_sessions: int = DAILY_LOOKBACK_SESSIONS,
    fetch: KisMinutesFetcher | None = None,
) -> str:
    moment = (now or now_utc()).astimezone(KST)
    today = moment.date()
    end = cal.latest_trading_day(today)
    sessions = cal.sessions_between(end - timedelta(days=lookback_sessions * 2 + 7), end)
    recent = sessions[-lookback_sessions:]
    eligible = [day for day in recent if day < today or moment.time() >= DAILY_READY]
    missing = [day for day in eligible if not snapshots.has_date(KIS_MINUTES, day)]
    if not missing:
        return "up-to-date"
    if fetch is None:
        with build_kis_client(cfg) as client:
            return _run_daily(cfg, cal, snapshots, missing, _bind_fetch(client))
    return _run_daily(cfg, cal, snapshots, missing, fetch)


def _run_daily(
    cfg: TalonSettings,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    missing: list[date],
    fetch: KisMinutesFetcher,
) -> str:
    done = 0
    rows = 0
    errors: list[str] = []
    for day in missing:
        try:
            rows += _load_day(cfg, cal, snapshots, day, fetch)
            done += 1
        except SourceError as exc:
            errors.append(f"{day}: {exc}")
            log.warning("daily kis-minutes failed for %s: %s", day, exc)
    result = f"{done}/{len(missing)} days, {rows} rows"
    if errors:
        result += f", errors: {len(errors)}"
    return result


def probe_kis_minutes(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    day: date | None = None,
    anchor: str | None = None,
    fetch: KisMinutesFetcher | None = None,
    now: datetime | None = None,
) -> KisMinutesProbeReport:
    if fetch is None:
        with build_kis_client(cfg) as client:
            return _run_probe(cal, _bind_fetch(client), day, anchor, now or now_utc())
    return _run_probe(cal, fetch, day, anchor, now or now_utc())


def _run_probe(
    cal: KrxCalendar,
    fetch: KisMinutesFetcher,
    day: date | None,
    anchor: str | None,
    now: datetime,
) -> KisMinutesProbeReport:
    if day is not None:
        used_anchor = anchor or _anchor_for(cal, day)
        bars = fetch(PROBE_SYMBOL, day, used_anchor)
        times = sorted(bar["time"] for bar in bars)
        return KisMinutesProbeReport(
            status="ok",
            day=day,
            anchor=used_anchor,
            rows=len(bars),
            first_ts=_bar_ts(day, times[0]) if times else None,
            last_ts=_bar_ts(day, times[-1]) if times else None,
        )
    return _probe_cliff(cal, fetch, now)


def _probe_cliff(
    cal: KrxCalendar,
    fetch: KisMinutesFetcher,
    now: datetime,
) -> KisMinutesProbeReport:
    today = now.astimezone(KST).date()
    end = cal.previous_trading_day(today)
    sessions = cal.sessions_between(end - timedelta(days=PROBE_WINDOW_DAYS), end)
    if not sessions:
        return KisMinutesProbeReport(status="no-data")
    calls = 0

    def has_data(session: date) -> bool:
        nonlocal calls
        calls += 1
        return len(fetch(PROBE_SYMBOL, session, _anchor_for(cal, session))) > 0

    if not has_data(sessions[-1]):
        return KisMinutesProbeReport(status="no-data", calls=calls)
    if has_data(sessions[0]):
        return KisMinutesProbeReport(status="ok", cliff=sessions[0], calls=calls)
    low = 0
    high = len(sessions) - 1
    while high - low > 1:
        mid = (low + high) // 2
        if has_data(sessions[mid]):
            high = mid
        else:
            low = mid
    return KisMinutesProbeReport(status="ok", cliff=sessions[high], calls=calls)


def verify_kis_minutes(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    start: date | None = None,
    end: date | None = None,
    symbols_sample: int = 30,
) -> KisMinutesVerifyReport:
    scan = snapshots.scan(KIS_MINUTES)
    if scan is None:
        return KisMinutesVerifyReport(status="ok")
    if start is not None:
        scan = scan.filter(pl.col("day") >= start)
    if end is not None:
        scan = scan.filter(pl.col("day") <= end)
    frame = scan.collect()
    if frame.is_empty():
        return KisMinutesVerifyReport(status="ok")

    duplicate_keys = frame.height - frame.select("symbol", "ts").unique().height
    ohlc_violations = frame.filter(
        (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("close") <= 0)
    ).height
    out_of_session = _out_of_session(cal, frame)
    crosscheck_symbols, crosscheck_mismatches, examples = _crosscheck(
        cal, snapshots, frame, symbols_sample
    )
    status = (
        "ok"
        if duplicate_keys == 0
        and ohlc_violations == 0
        and out_of_session == 0
        and crosscheck_mismatches == 0
        else "issues"
    )
    return KisMinutesVerifyReport(
        status=status,
        days=frame.select("day").n_unique(),
        rows=frame.height,
        duplicate_keys=duplicate_keys,
        ohlc_violations=ohlc_violations,
        out_of_session=out_of_session,
        crosscheck_symbols=crosscheck_symbols,
        crosscheck_mismatches=crosscheck_mismatches,
        examples=examples,
    )


def _out_of_session(cal: KrxCalendar, frame: pl.DataFrame) -> int:
    days = frame.select("day").unique().to_series().to_list()
    bounds = pl.DataFrame(
        {
            "day": days,
            "session_open": [cal.session_open(day) for day in days],
            "session_close": [cal.session_close(day) for day in days],
        },
        schema={
            "day": pl.Date(),
            "session_open": pl.Datetime("us", "UTC"),
            "session_close": pl.Datetime("us", "UTC"),
        },
    )
    joined = frame.join(bounds, on="day", how="left")
    return joined.filter(
        (pl.col("ts") < pl.col("session_open")) | (pl.col("ts") > pl.col("session_close"))
    ).height


def _crosscheck(
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    frame: pl.DataFrame,
    symbols_sample: int,
) -> tuple[int, int, list[str]]:
    symbols = frame.select("symbol").unique().to_series().to_list()
    if not symbols:
        return 0, 0, []
    sampled = random.Random(0).sample(sorted(symbols), min(symbols_sample, len(symbols)))
    daily_scan = snapshots.scan(DAILY_CANDLES)
    if daily_scan is None:
        return 0, 0, []
    last_bars = (
        frame.filter(pl.col("symbol").is_in(sampled))
        .sort("ts")
        .group_by("symbol", "day")
        .agg(
            pl.col("close").last().alias("minute_close"),
            pl.col("ts").last().alias("last_ts"),
        )
    )
    daily = (
        daily_scan.filter(pl.col("symbol").is_in(sampled))
        .select("symbol", "day", pl.col("close").alias("daily_close"))
        .collect()
    )
    merged = last_bars.join(daily, on=["symbol", "day"], how="inner")
    if merged.is_empty():
        return 0, 0, []
    crosscheck_symbols = merged.select("symbol").n_unique()
    days = merged.select("day").unique().to_series().to_list()
    expected_close = pl.DataFrame(
        {
            "day": days,
            "expected_close_ts": [to_utc(cal.session_close(day)) for day in days],
        },
        schema={"day": pl.Date(), "expected_close_ts": pl.Datetime("us", "UTC")},
    )
    auction = merged.join(expected_close, on="day", how="left").filter(
        pl.col("last_ts") == pl.col("expected_close_ts")
    )
    if auction.is_empty():
        return crosscheck_symbols, 0, []
    mismatches = auction.filter(pl.col("minute_close") != pl.col("daily_close")).sort(
        "symbol", "day"
    )
    examples = [
        f"{row['symbol']} {row['day']}: {row['minute_close']} vs {row['daily_close']}"
        for row in mismatches.head(10).iter_rows(named=True)
    ]
    return crosscheck_symbols, mismatches.height, examples

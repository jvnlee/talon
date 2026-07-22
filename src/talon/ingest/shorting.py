import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import cast

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    DAILY_CANDLES,
    SHORTING,
    SHORTING_BALANCE,
    SHORTING_INVESTOR,
    DatePartitionedStore,
)
from talon.errors import SourceError
from talon.markets.kr import KrxCalendar
from talon.models import BackfillSummary, ShortingVerifyReport
from talon.sources.krx_daily import KrxCredentials
from talon.sources.krx_shorting import (
    fetch_shorting,
    fetch_shorting_balance,
    fetch_shorting_investor,
    market_short_volume,
)
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

SHORTING_READY = time(18, 30)
BALANCE_DELAY_SESSIONS = 3
DAILY_LOOKBACK_SESSIONS = 7
MAX_CONSECUTIVE_FAILURES = 3

TRADE_START = date(2016, 1, 1)
BALANCE_START = date(2016, 6, 30)
INVESTOR_START = date(2017, 5, 22)

RATIO_TOLERANCE_PCT = 0.5

BAN_WINDOWS: tuple[tuple[date, date], ...] = (
    (date(2020, 3, 16), date(2021, 5, 2)),
    (date(2023, 11, 6), date(2025, 3, 30)),
)

DATASET_NAMES: dict[str, str] = {
    "trade": SHORTING,
    "balance": SHORTING_BALANCE,
    "investor": SHORTING_INVESTOR,
}

TradeFetcher = Callable[[date], pl.DataFrame]
BalanceFetcher = Callable[[date], pl.DataFrame]
InvestorFetcher = Callable[[date, date], pl.DataFrame]


@dataclass
class ShortingFetchers:
    trade: TradeFetcher
    balance: BalanceFetcher
    investor: InvestorFetcher


def _default_fetchers(cfg: TalonSettings) -> ShortingFetchers:
    credentials = KrxCredentials(cfg.krx_id, cfg.krx_password)
    pause = cfg.krx_flows_pause_seconds

    def trade(day: date) -> pl.DataFrame:
        return fetch_shorting(day, credentials=credentials, pause=pause)

    def balance(day: date) -> pl.DataFrame:
        return fetch_shorting_balance(day, credentials=credentials, pause=pause)

    def investor(start: date, end: date) -> pl.DataFrame:
        return fetch_shorting_investor(start, end, credentials=credentials, pause=pause)

    return ShortingFetchers(trade=trade, balance=balance, investor=investor)


def _trade_ready(frame: pl.DataFrame) -> bool:
    return not frame.is_empty() and market_short_volume(frame) > 0


def _balance_ready(frame: pl.DataFrame) -> bool:
    return not frame.is_empty()


def _floor_for(dataset: str) -> date:
    if dataset == SHORTING_BALANCE:
        return BALANCE_START
    if dataset == SHORTING_INVESTOR:
        return INVESTOR_START
    return TRADE_START


def backfill_shorting(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    dataset: str,
    start: date,
    end: date,
    fetchers: ShortingFetchers | None = None,
    progress: Callable[[int, int, date], None] | None = None,
) -> BackfillSummary:
    run_id = state.start_job(f"backfill-shorting-{dataset}")
    if fetchers is None:
        fetchers = _default_fetchers(cfg)
    if dataset == SHORTING_INVESTOR:
        summary = _backfill_investor(cal, snapshots, start, end, fetchers.investor, progress)
    elif dataset == SHORTING_BALANCE:
        summary = _backfill_by_day(
            SHORTING_BALANCE, cal, snapshots, start, end, fetchers.balance, _balance_ready, progress
        )
    else:
        summary = _backfill_by_day(
            SHORTING, cal, snapshots, start, end, fetchers.trade, _trade_ready, progress
        )
    state.finish_job(run_id, summary.status == "ok", summary.model_dump(mode="json"))
    return summary


def _backfill_by_day(
    dataset: str,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    fetch: TradeFetcher,
    ready: Callable[[pl.DataFrame], bool],
    progress: Callable[[int, int, date], None] | None,
) -> BackfillSummary:
    sessions = cal.sessions_between(max(start, _floor_for(dataset)), end)
    loaded = 0
    skipped = 0
    failed: list[str] = []
    streak = 0
    aborted = False
    for index, day in enumerate(sessions, start=1):
        if snapshots.has_date(dataset, day):
            skipped += 1
        else:
            try:
                frame = fetch(day)
                if not ready(frame):
                    skipped += 1
                    streak = 0
                    log.info("shorting %s not ready for %s", dataset, day)
                else:
                    snapshots.write_date(dataset, day, frame)
                    loaded += 1
                    streak = 0
            except SourceError as exc:
                failed.append(day.isoformat())
                streak += 1
                log.warning("shorting %s backfill failed for %s: %s", dataset, day, exc)
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    log.error("연속 %d일 실패로 공매도 백필을 중단합니다", streak)
        if progress is not None:
            progress(index, len(sessions), day)
        if aborted:
            break
    status = "aborted" if aborted else ("ok" if not failed else "partial")
    return BackfillSummary(
        status=status, sessions=len(sessions), loaded=loaded, skipped=skipped, failed=failed
    )


def _year_chunks(sessions: list[date]) -> list[tuple[date, date, list[date]]]:
    chunks: list[tuple[date, date, list[date]]] = []
    current_year: int | None = None
    bucket: list[date] = []
    for day in sessions:
        if current_year is None or day.year == current_year:
            bucket.append(day)
            current_year = day.year
        else:
            chunks.append((bucket[0], bucket[-1], bucket))
            bucket = [day]
            current_year = day.year
    if bucket:
        chunks.append((bucket[0], bucket[-1], bucket))
    return chunks


def _write_investor_days(
    snapshots: DatePartitionedStore, frame: pl.DataFrame, days: list[date]
) -> int:
    written = 0
    for day in days:
        if snapshots.has_date(SHORTING_INVESTOR, day):
            continue
        day_frame = frame.filter(pl.col("day") == day)
        if day_frame.is_empty():
            continue
        snapshots.write_date(SHORTING_INVESTOR, day, day_frame)
        written += 1
    return written


def _backfill_investor(
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    fetch: InvestorFetcher,
    progress: Callable[[int, int, date], None] | None,
) -> BackfillSummary:
    sessions = cal.sessions_between(max(start, INVESTOR_START), end)
    loaded = 0
    skipped = 0
    failed: list[str] = []
    streak = 0
    aborted = False
    index = 0
    total = len(sessions)
    for chunk_start, chunk_end, days in _year_chunks(sessions):
        missing = [day for day in days if not snapshots.has_date(SHORTING_INVESTOR, day)]
        if not missing:
            skipped += len(days)
        else:
            try:
                frame = fetch(chunk_start, chunk_end)
                written = _write_investor_days(snapshots, frame, days)
                loaded += written
                skipped += len(days) - written
                streak = 0
            except SourceError as exc:
                failed.append(f"{chunk_start}..{chunk_end}")
                streak += 1
                log.warning(
                    "shorting investor backfill failed for %s..%s: %s", chunk_start, chunk_end, exc
                )
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    log.error("연속 %d청크 실패로 공매도 투자자 백필을 중단합니다", streak)
        index += len(days)
        if progress is not None:
            progress(index, total, chunk_end)
        if aborted:
            break
    status = "aborted" if aborted else ("ok" if not failed else "partial")
    return BackfillSummary(
        status=status, sessions=total, loaded=loaded, skipped=skipped, failed=failed
    )


def daily_shorting(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    now: datetime | None = None,
    lookback_sessions: int = DAILY_LOOKBACK_SESSIONS,
    fetchers: ShortingFetchers | None = None,
) -> str:
    moment = (now or now_utc()).astimezone(KST)
    today = moment.date()
    end = cal.latest_trading_day(today)
    sessions = cal.sessions_between(end - timedelta(days=lookback_sessions * 2 + 14), end)
    recent = sessions[-lookback_sessions:]
    tradeable = [day for day in recent if day < today or moment.time() >= SHORTING_READY]
    if len(sessions) > BALANCE_DELAY_SESSIONS:
        frontier = sessions[-(BALANCE_DELAY_SESSIONS + 1)]
        balanceable = [day for day in recent if day <= frontier]
    else:
        balanceable = []
    if fetchers is None:
        fetchers = _default_fetchers(cfg)
    trade_result = _daily_fill(SHORTING, tradeable, snapshots, fetchers.trade, _trade_ready)
    balance_result = _daily_fill(
        SHORTING_BALANCE, balanceable, snapshots, fetchers.balance, _balance_ready
    )
    investor_result = _daily_fill_investor(tradeable, snapshots, fetchers.investor)
    return f"trade {trade_result}, balance {balance_result}, investor {investor_result}"


def _daily_fill(
    dataset: str,
    days: list[date],
    snapshots: DatePartitionedStore,
    fetch: TradeFetcher,
    ready: Callable[[pl.DataFrame], bool],
) -> str:
    missing = [day for day in days if not snapshots.has_date(dataset, day)]
    if not missing:
        return "up-to-date"
    done = 0
    errors = 0
    for day in missing:
        try:
            frame = fetch(day)
            if not ready(frame):
                continue
            snapshots.write_date(dataset, day, frame)
            done += 1
        except SourceError as exc:
            errors += 1
            log.warning("daily shorting %s failed for %s: %s", dataset, day, exc)
    result = f"{done}/{len(missing)}"
    if errors:
        result += f" (errors {errors})"
    return result


def _daily_fill_investor(
    days: list[date],
    snapshots: DatePartitionedStore,
    fetch: InvestorFetcher,
) -> str:
    missing = [
        day
        for day in days
        if day >= INVESTOR_START and not snapshots.has_date(SHORTING_INVESTOR, day)
    ]
    if not missing:
        return "up-to-date"
    try:
        frame = fetch(missing[0], missing[-1])
    except SourceError as exc:
        log.warning("daily shorting investor failed for %s..%s: %s", missing[0], missing[-1], exc)
        return f"0/{len(missing)} (errors 1)"
    done = _write_investor_days(snapshots, frame, missing)
    return f"{done}/{len(missing)}"


def _collect_range(
    snapshots: DatePartitionedStore,
    dataset: str,
    start: date | None,
    end: date | None,
) -> pl.DataFrame | None:
    scan = snapshots.scan(dataset)
    if scan is None:
        return None
    if start is not None:
        scan = scan.filter(pl.col("day") >= start)
    if end is not None:
        scan = scan.filter(pl.col("day") <= end)
    frame = scan.collect()
    return frame if not frame.is_empty() else None


def _in_ban_window(frame: pl.DataFrame) -> pl.DataFrame:
    predicate = pl.lit(False)
    for window_start, window_end in BAN_WINDOWS:
        predicate = predicate | pl.col("day").is_between(window_start, window_end)
    return frame.filter(predicate)


def _candle_alerts(
    snapshots: DatePartitionedStore, trade: pl.DataFrame
) -> tuple[int, int, list[str]]:
    day_column = trade.get_column("day")
    start = cast("date | None", day_column.min())
    end = cast("date | None", day_column.max())
    candles = _collect_range(snapshots, DAILY_CANDLES, start, end)
    if candles is None:
        return 0, 0, []
    candle_volume = candles.select(
        "day", "symbol", pl.col("volume").alias("candle_volume")
    )
    joined = trade.join(candle_volume, on=["day", "symbol"], how="left").filter(
        pl.col("candle_volume").is_not_null()
    )
    checked = joined.height
    offenders = joined.filter(pl.col("short_volume") > pl.col("candle_volume"))
    examples = [
        f"{row['symbol']} {row['day']}: short {row['short_volume']} > candle "
        f"{row['candle_volume']:g}"
        for row in offenders.head(5).iter_rows(named=True)
    ]
    return checked, offenders.height, examples


def verify_shorting(
    cfg: TalonSettings,
    *,
    snapshots: DatePartitionedStore,
    start: date | None = None,
    end: date | None = None,
) -> ShortingVerifyReport:
    trade = _collect_range(snapshots, SHORTING, start, end)
    balance = _collect_range(snapshots, SHORTING_BALANCE, start, end)
    investor = _collect_range(snapshots, SHORTING_INVESTOR, start, end)

    examples: list[str] = []
    ratio_violations = 0
    candle_checked = 0
    candle_alerts = 0
    trade_days = 0
    trade_rows = 0
    ban_window_days = 0
    ban_zero_days = 0
    if trade is not None:
        trade_rows = trade.height
        trade_days = trade.select("day").n_unique()
        ratio_violations = trade.filter(
            (pl.col("short_volume") > pl.col("total_volume_consolidated"))
            | (pl.col("short_ratio_pct") < 0)
            | (pl.col("short_ratio_pct") > 100 + RATIO_TOLERANCE_PCT)
        ).height
        candle_checked, candle_alerts, candle_examples = _candle_alerts(snapshots, trade)
        examples.extend(candle_examples)
        ban_frame = _in_ban_window(trade)
        if not ban_frame.is_empty():
            by_day = ban_frame.group_by("day").agg(
                pl.col("short_volume").sum().alias("short_sum")
            )
            ban_window_days = by_day.height
            ban_zero_days = by_day.filter(pl.col("short_sum") == 0).height

    balance_days = 0
    balance_rows = 0
    balance_violations = 0
    if balance is not None:
        balance_rows = balance.height
        balance_days = balance.select("day").n_unique()
        balance_violations = balance.filter(
            (pl.col("short_balance_qty") > pl.col("listed_shares"))
            | (pl.col("short_balance_ratio_pct") > 100 + RATIO_TOLERANCE_PCT)
        ).height

    investor_days = 0
    investor_rows = 0
    investor_total_mismatches = 0
    if investor is not None:
        investor_rows = investor.height
        investor_days = investor.select("day").n_unique()
        totals = investor.filter(pl.col("investor") == "total").select(
            "day", "market", pl.col("vol_shares").alias("total_vol")
        )
        parts = (
            investor.filter(pl.col("investor") != "total")
            .group_by("day", "market")
            .agg(pl.col("vol_shares").sum().alias("parts_vol"))
        )
        merged = totals.join(parts, on=["day", "market"], how="inner")
        investor_total_mismatches = merged.filter(
            pl.col("total_vol") != pl.col("parts_vol")
        ).height

    status = (
        "issues"
        if ratio_violations or balance_violations or investor_total_mismatches
        else "ok"
    )
    return ShortingVerifyReport(
        status=status,
        trade_days=trade_days,
        trade_rows=trade_rows,
        ratio_violations=ratio_violations,
        candle_checked=candle_checked,
        candle_alerts=candle_alerts,
        balance_days=balance_days,
        balance_rows=balance_rows,
        balance_violations=balance_violations,
        investor_days=investor_days,
        investor_rows=investor_rows,
        investor_total_mismatches=investor_total_mismatches,
        ban_window_days=ban_window_days,
        ban_zero_days=ban_zero_days,
        examples=examples,
    )

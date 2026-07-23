import logging
import random
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    CREDIT_BALANCE,
    CREDIT_BALANCE_1D_SCHEMA,
    DAILY_CANDLES,
    STOCK_INFO,
    DatePartitionedStore,
)
from talon.errors import SourceError
from talon.ingest.pool import parallel_fetch
from talon.models import CreditBackfillSummary, CreditVerifyReport
from talon.sources.kis import KisClient, build_kis_client
from talon.sources.kis_market import fetch_credit_daily
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

CreditFetcher = Callable[[str, date], list[dict[str, Any]]]

CREDIT_START = date(2016, 1, 4)
WALKBACK_STRIDE_DAYS = 35
JOB = "credit-backfill"
GVRT_TOLERANCE = 0.15
CONTINUITY_SAMPLE = 50
BALANCE_COLUMNS = ("loan_balance_qty", "short_balance_qty")


def _bind_fetch(client: KisClient) -> CreditFetcher:
    def fetch(symbol: str, anchor: date) -> list[dict[str, Any]]:
        return fetch_credit_daily(client, symbol, anchor)

    return fetch


def _universe_symbols(snapshots: DatePartitionedStore) -> list[str]:
    info = snapshots.latest(STOCK_INFO)
    if info is not None and not info[1].is_empty():
        return info[1]["symbol"].to_list()
    daily = snapshots.latest(DAILY_CANDLES)
    if daily is not None and not daily[1].is_empty():
        return daily[1]["symbol"].to_list()
    raise SourceError("유니버스 없음: stock_info/candles_1d 부재")


def _group_by_day(
    results: list[tuple[str, list[dict[str, Any]], datetime]],
    *,
    floor: date | None = None,
    ceiling: date | None = None,
) -> dict[date, list[dict[str, Any]]]:
    groups: dict[date, list[dict[str, Any]]] = {}
    for symbol, rows, stamp in results:
        for row in rows:
            day = row["day"]
            if floor is not None and day < floor:
                continue
            if ceiling is not None and day > ceiling:
                continue
            record = dict(row)
            record["symbol"] = symbol
            record["fetched_at"] = stamp
            groups.setdefault(day, []).append(record)
    return groups


def _day_frame(records: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(records, schema=CREDIT_BALANCE_1D_SCHEMA).sort("symbol")


def daily_credit(
    cfg: TalonSettings,
    *,
    snapshots: DatePartitionedStore,
    now: datetime | None = None,
    fetch: CreditFetcher | None = None,
) -> str:
    anchor = (now or now_utc()).astimezone(KST).date()
    symbols = _universe_symbols(snapshots)
    if fetch is None:
        with build_kis_client(cfg) as client:
            return _run_daily(cfg, snapshots, symbols, anchor, _bind_fetch(client))
    return _run_daily(cfg, snapshots, symbols, anchor, fetch)


def _run_daily(
    cfg: TalonSettings,
    snapshots: DatePartitionedStore,
    symbols: list[str],
    anchor: date,
    fetch: CreditFetcher,
) -> str:
    def one(symbol: str) -> list[dict[str, Any]]:
        return fetch(symbol, anchor)

    results, failed = parallel_fetch(
        symbols,
        one,
        workers=cfg.kis_workers,
        max_failure_ratio=cfg.collect_failure_ratio,
        log_name="credit",
    )
    groups = _group_by_day(results)
    written_days = 0
    rows = 0
    for day in sorted(groups):
        if snapshots.has_date(CREDIT_BALANCE, day):
            continue
        frame = _day_frame(groups[day])
        snapshots.upsert_date(CREDIT_BALANCE, day, frame, key=("symbol",))
        written_days += 1
        rows += frame.height
    result = f"{len(symbols)} symbols, {written_days}/{len(groups)} days, {rows} rows"
    if failed:
        result += f", errors: {failed}"
    return result


def backfill_credit(
    cfg: TalonSettings,
    *,
    state: StateDB,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    symbols: list[str] | None = None,
    fetch: CreditFetcher | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> CreditBackfillSummary:
    run_id = state.start_job(JOB)
    targets = symbols if symbols is not None else _universe_symbols(snapshots)
    if fetch is None:
        with build_kis_client(cfg) as client:
            summary = _run_backfill(
                cfg, snapshots, targets, start, end, _bind_fetch(client), progress
            )
    else:
        summary = _run_backfill(cfg, snapshots, targets, start, end, fetch, progress)
    state.finish_job(run_id, summary.status == "ok", summary.model_dump(mode="json"))
    return summary


def _walk_back(fetch: CreditFetcher, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    anchor = end
    while anchor >= start:
        rows = fetch(symbol, anchor)
        if not rows:
            break
        collected.extend(row for row in rows if start <= row["day"] <= end)
        anchor = anchor - timedelta(days=WALKBACK_STRIDE_DAYS)
    return collected


def _run_backfill(
    cfg: TalonSettings,
    snapshots: DatePartitionedStore,
    symbols: list[str],
    start: date,
    end: date,
    fetch: CreditFetcher,
    progress: Callable[[int, int, str], None] | None,
) -> CreditBackfillSummary:
    floor = max(start, CREDIT_START)

    def one(symbol: str) -> list[dict[str, Any]]:
        return _walk_back(fetch, symbol, floor, end)

    def report(index: int, total: int, symbol: str) -> None:
        if progress is not None:
            progress(index, total, symbol)

    results, failed = parallel_fetch(
        symbols,
        one,
        workers=cfg.kis_workers,
        max_failure_ratio=cfg.collect_failure_ratio,
        log_name="credit-backfill",
        progress=report,
    )
    groups = _group_by_day(results, floor=floor, ceiling=end)
    rows = 0
    for day in sorted(groups):
        frame = _day_frame(groups[day])
        snapshots.upsert_date(CREDIT_BALANCE, day, frame, key=("symbol",))
        rows += frame.height
    status = "ok" if not failed else "partial"
    return CreditBackfillSummary(
        status=status,
        symbols=len(results),
        days=len(groups),
        rows=rows,
        failed=failed,
    )


def _collect_range(
    snapshots: DatePartitionedStore,
    start: date | None,
    end: date | None,
) -> pl.DataFrame | None:
    scan = snapshots.scan(CREDIT_BALANCE)
    if scan is None:
        return None
    if start is not None:
        scan = scan.filter(pl.col("day") >= start)
    if end is not None:
        scan = scan.filter(pl.col("day") <= end)
    frame = scan.collect()
    return frame if not frame.is_empty() else None


def _continuity(frame: pl.DataFrame, symbols_sample: int) -> tuple[int, int]:
    symbols = frame.select("symbol").unique().to_series().to_list()
    if not symbols:
        return 0, 0
    sampled = random.Random(0).sample(sorted(symbols), min(symbols_sample, len(symbols)))
    subset = (
        frame.filter(pl.col("symbol").is_in(sampled))
        .sort(["symbol", "day"])
        .with_columns(
            pl.col("loan_balance_qty").shift(1).over("symbol").alias("prev_balance"),
            pl.col("day").shift(1).over("symbol").alias("prev_day"),
        )
        .filter(pl.col("prev_balance").is_not_null())
    )
    if subset.is_empty():
        return 0, 0
    checked = subset.height
    matched = subset.filter(
        (pl.col("prev_balance") + pl.col("loan_new_qty") - pl.col("loan_repay_qty"))
        == pl.col("loan_balance_qty")
    ).height
    return checked, matched


def _gvrt(frame: pl.DataFrame) -> tuple[int, int]:
    checked = frame.filter(
        (pl.col("volume") > 0) & pl.col("loan_give_rate").is_not_null()
    )
    if checked.is_empty():
        return 0, 0
    mismatches = checked.filter(
        (pl.col("loan_new_qty") / pl.col("volume") * 100 - pl.col("loan_give_rate")).abs()
        > GVRT_TOLERANCE
    ).height
    return checked.height, mismatches


def verify_credit(
    cfg: TalonSettings,
    *,
    snapshots: DatePartitionedStore,
    start: date | None = None,
    end: date | None = None,
    symbols_sample: int = CONTINUITY_SAMPLE,
) -> CreditVerifyReport:
    frame = _collect_range(snapshots, start, end)
    if frame is None:
        return CreditVerifyReport(status="empty")

    duplicate_keys = frame.height - frame.select("day", "symbol").unique().height
    negative_balances = frame.filter(
        (pl.col("loan_balance_qty") < 0) | (pl.col("short_balance_qty") < 0)
    ).height
    settle_violations = frame.filter(
        pl.col("settle_day").is_not_null() & (pl.col("settle_day") < pl.col("day"))
    ).height
    continuity_checked, continuity_ok = _continuity(frame, symbols_sample)
    gvrt_checked, gvrt_mismatches = _gvrt(frame)

    examples: list[str] = []
    if negative_balances:
        for row in frame.filter(
            (pl.col("loan_balance_qty") < 0) | (pl.col("short_balance_qty") < 0)
        ).head(5).iter_rows(named=True):
            examples.append(f"{row['symbol']} {row['day']}: negative balance")
    if settle_violations:
        for row in frame.filter(
            pl.col("settle_day").is_not_null() & (pl.col("settle_day") < pl.col("day"))
        ).head(5).iter_rows(named=True):
            examples.append(f"{row['symbol']} {row['day']}: settle {row['settle_day']} < day")

    status = (
        "issues"
        if duplicate_keys or negative_balances or settle_violations
        else "ok"
    )
    return CreditVerifyReport(
        status=status,
        days=frame.select("day").n_unique(),
        rows=frame.height,
        symbols=frame.select("symbol").n_unique(),
        first_day=frame.select(pl.col("day").min()).item(),
        last_day=frame.select(pl.col("day").max()).item(),
        continuity_checked=continuity_checked,
        continuity_ok=continuity_ok,
        continuity_ratio=(continuity_ok / continuity_checked if continuity_checked else None),
        negative_balances=negative_balances,
        gvrt_checked=gvrt_checked,
        gvrt_mismatches=gvrt_mismatches,
        settle_violations=settle_violations,
        duplicate_keys=duplicate_keys,
        examples=examples,
    )

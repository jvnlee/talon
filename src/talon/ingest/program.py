import logging
import time as time_module
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    DAILY_CANDLES,
    PROGRAM_MARKET_1D,
    PROGRAM_MARKET_1D_SCHEMA,
    PROGRAM_STOCK_1D,
    PROGRAM_STOCK_1D_SCHEMA,
    STOCK_INFO,
    DatePartitionedStore,
)
from talon.errors import SourceError
from talon.ingest.pool import parallel_fetch
from talon.markets.kr import KrxCalendar
from talon.models import BackfillSummary, ProgramVerifyReport
from talon.sources.kis import KisClient, build_kis_client
from talon.sources.kis_market import fetch_program_daily
from talon.sources.krx_daily import KrxCredentials
from talon.sources.krx_program import (
    COMPONENTS,
    PROGRAM_MARKETS,
    fetch_program_market,
)
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

MarketFetcher = Callable[[date, str], pl.DataFrame]
StockFetcher = Callable[[str, date], list[dict[str, Any]]]

MARKET_START = date(2002, 1, 2)
PROGRAM_READY = time(18, 0)
DAILY_LOOKBACK_SESSIONS = 7
MAX_CONSECUTIVE_FAILURES = 3
PAGE_SIZE = 30
IDENTITY_TOL = 1.0

PROGRAM_STOCK_KEY = ("symbol",)
MARKET_ROWS = len(PROGRAM_MARKETS) * len(COMPONENTS)


def _default_market_fetcher(cfg: TalonSettings) -> MarketFetcher:
    credentials = KrxCredentials(cfg.krx_id, cfg.krx_password)

    def fetch(day: date, market: str) -> pl.DataFrame:
        return fetch_program_market(day, market, credentials=credentials)

    return fetch


def _bind_stock(client: KisClient) -> StockFetcher:
    def fetch(symbol: str, anchor: date) -> list[dict[str, Any]]:
        return fetch_program_daily(client, symbol, anchor)

    return fetch


def _market_complete(snapshots: DatePartitionedStore, day: date) -> bool:
    frame = snapshots.read_date(PROGRAM_MARKET_1D, day)
    return frame is not None and frame.height >= MARKET_ROWS


def _fetch_market_day(
    fetch: MarketFetcher, day: date, sleep: Callable[[float], None], pause: float
) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for market in PROGRAM_MARKETS:
        frames.append(fetch(day, market))
        sleep(pause)
    combined = pl.concat(frames, how="vertical") if frames else pl.DataFrame(
        schema=PROGRAM_MARKET_1D_SCHEMA
    )
    return combined.sort(["market", "component"])


def backfill_program_market(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    fetch: MarketFetcher | None = None,
    sleep: Callable[[float], None] = time_module.sleep,
    progress: Callable[[int, int, date], None] | None = None,
) -> BackfillSummary:
    sessions = cal.sessions_between(start, end)
    run_id = state.start_job("backfill-program-market")
    if fetch is None:
        fetch = _default_market_fetcher(cfg)
    pause = cfg.krx_flows_pause_seconds
    loaded = 0
    skipped = 0
    failed: list[str] = []
    streak = 0
    aborted = False
    for index, day in enumerate(sessions, start=1):
        if _market_complete(snapshots, day):
            skipped += 1
        else:
            try:
                frame = _fetch_market_day(fetch, day, sleep, pause)
                snapshots.write_date(PROGRAM_MARKET_1D, day, frame)
                if frame.height:
                    loaded += 1
                streak = 0
            except SourceError as exc:
                failed.append(day.isoformat())
                streak += 1
                log.warning("program-market backfill failed for %s: %s", day, exc)
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    log.error("연속 %d세션 실패로 프로그램매매 백필을 중단합니다", streak)
        if progress is not None:
            progress(index, len(sessions), day)
        if aborted:
            break
    status = "aborted" if aborted else ("ok" if not failed else "partial")
    summary = BackfillSummary(
        status=status, sessions=len(sessions), loaded=loaded, skipped=skipped, failed=failed
    )
    state.finish_job(run_id, status == "ok", summary.model_dump(mode="json"))
    return summary


def daily_program_market(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    now: datetime | None = None,
    lookback_sessions: int = DAILY_LOOKBACK_SESSIONS,
    fetch: MarketFetcher | None = None,
    sleep: Callable[[float], None] = time_module.sleep,
) -> str:
    moment = (now or now_utc()).astimezone(KST)
    today = moment.date()
    end = cal.latest_trading_day(today)
    sessions = cal.sessions_between(end - timedelta(days=lookback_sessions * 2 + 7), end)
    recent = sessions[-lookback_sessions:]
    eligible = [day for day in recent if day < today or moment.time() >= PROGRAM_READY]
    missing = [day for day in eligible if not _market_complete(snapshots, day)]
    if not missing:
        return "up-to-date"
    if fetch is None:
        fetch = _default_market_fetcher(cfg)
    pause = cfg.krx_flows_pause_seconds
    done = 0
    rows = 0
    errors: list[str] = []
    for day in missing:
        try:
            frame = _fetch_market_day(fetch, day, sleep, pause)
            snapshots.write_date(PROGRAM_MARKET_1D, day, frame)
            done += 1
            rows += frame.height
        except SourceError as exc:
            errors.append(f"{day}: {exc}")
            log.warning("daily program-market failed for %s: %s", day, exc)
    result = f"{done}/{len(missing)} days, {rows} rows"
    if errors:
        result += f", errors: {len(errors)}"
    return result


def _universe_symbols(snapshots: DatePartitionedStore, day: date) -> list[str]:
    frame = snapshots.read_date(STOCK_INFO, day)
    if frame is not None and not frame.is_empty():
        return frame.get_column("symbol").to_list()
    daily = snapshots.read_date(DAILY_CANDLES, day)
    if daily is not None and not daily.is_empty():
        return daily.get_column("symbol").to_list()
    return []


def _write_by_day(
    snapshots: DatePartitionedStore,
    results: list[tuple[str, list[dict[str, Any]], datetime]],
) -> tuple[int, int]:
    by_day: dict[date, list[dict[str, Any]]] = {}
    for _symbol, records, stamp in results:
        for record in records:
            by_day.setdefault(record["day"], []).append({**record, "fetched_at": stamp})
    days = 0
    rows = 0
    for day, buffered in by_day.items():
        frame = pl.DataFrame(buffered, schema=PROGRAM_STOCK_1D_SCHEMA).sort("symbol")
        snapshots.upsert_date(PROGRAM_STOCK_1D, day, frame, PROGRAM_STOCK_KEY)
        days += 1
        rows += frame.height
    return days, rows


def _drop_unpublished_today(
    results: list[tuple[str, list[dict[str, Any]], datetime]], anchor: date
) -> list[tuple[str, list[dict[str, Any]], datetime]]:
    return [
        (
            symbol,
            [
                record
                for record in records
                if record["day"] != anchor or record["net_value"] is not None
            ],
            stamp,
        )
        for symbol, records, stamp in results
    ]


def _run_daily_stock(
    cfg: TalonSettings,
    snapshots: DatePartitionedStore,
    symbols: list[str],
    anchor: date,
    fetch: StockFetcher,
) -> str:
    results, failed = parallel_fetch(
        symbols,
        lambda symbol: fetch(symbol, anchor),
        workers=cfg.kis_workers,
        max_failure_ratio=cfg.collect_failure_ratio,
        log_name="program-stock",
    )
    days, rows = _write_by_day(snapshots, _drop_unpublished_today(results, anchor))
    result = f"{days} days, {rows} rows"
    if failed:
        result += f", {failed} failed"
    return result


def daily_program_stock(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    now: datetime | None = None,
    fetch: StockFetcher | None = None,
) -> str:
    moment = (now or now_utc()).astimezone(KST)
    today = moment.date()
    end = cal.latest_trading_day(today)
    symbols = _universe_symbols(snapshots, end)
    if not symbols:
        return "no-universe"
    if fetch is None:
        with build_kis_client(cfg) as client:
            return _run_daily_stock(cfg, snapshots, symbols, today, _bind_stock(client))
    return _run_daily_stock(cfg, snapshots, symbols, today, fetch)


def _walkback_symbol(
    fetch: StockFetcher, cal: KrxCalendar, symbol: str, start: date, end: date, anchor: date
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_min: date | None = None
    while anchor >= start:
        page = fetch(symbol, anchor)
        if not page:
            break
        page_min = min(row["day"] for row in page)
        records.extend(row for row in page if start <= row["day"] <= end)
        if len(page) < PAGE_SIZE:
            break
        if seen_min is not None and page_min >= seen_min:
            break
        seen_min = page_min
        if page_min <= start:
            break
        anchor = cal.previous_trading_day(page_min)
    return records


def _resume_anchors(
    snapshots: DatePartitionedStore,
    cal: KrxCalendar,
    symbols: list[str],
    start: date,
    end: date,
) -> tuple[list[tuple[str, date]], int]:
    scan = snapshots.scan(PROGRAM_STOCK_1D)
    mins: dict[str, date] = {}
    if scan is not None:
        covered = scan.group_by("symbol").agg(pl.col("day").min().alias("min_day")).collect()
        mins = dict(
            zip(
                covered.get_column("symbol").to_list(),
                covered.get_column("min_day").to_list(),
                strict=True,
            )
        )
    pending: list[tuple[str, date]] = []
    skipped = 0
    for symbol in symbols:
        stored_min = mins.get(symbol)
        if stored_min is not None and stored_min <= start:
            skipped += 1
            continue
        if stored_min is None:
            pending.append((symbol, end))
        else:
            pending.append((symbol, min(cal.previous_trading_day(stored_min), end)))
    return pending, skipped


def backfill_program_stock(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    start: date,
    end: date,
    symbols: list[str] | None = None,
    fetch: StockFetcher | None = None,
) -> BackfillSummary:
    run_id = state.start_job("backfill-program-stock")
    targets = symbols if symbols is not None else _universe_symbols(snapshots, end)
    pending, resume_skipped = _resume_anchors(snapshots, cal, targets, start, end)
    if fetch is None:
        with build_kis_client(cfg) as client:
            summary = _run_stock_backfill(
                cfg, cal, snapshots, pending, start, end, _bind_stock(client), resume_skipped
            )
    else:
        summary = _run_stock_backfill(
            cfg, cal, snapshots, pending, start, end, fetch, resume_skipped
        )
    state.finish_job(run_id, summary.status == "ok", summary.model_dump(mode="json"))
    return summary


def _run_stock_backfill(
    cfg: TalonSettings,
    cal: KrxCalendar,
    snapshots: DatePartitionedStore,
    pending: list[tuple[str, date]],
    start: date,
    end: date,
    fetch: StockFetcher,
    resume_skipped: int,
) -> BackfillSummary:
    anchors = dict(pending)
    aborted = False
    try:
        results, _failed = parallel_fetch(
            [symbol for symbol, _ in pending],
            lambda symbol: _walkback_symbol(fetch, cal, symbol, start, end, anchors[symbol]),
            workers=cfg.kis_workers,
            max_failure_ratio=cfg.collect_failure_ratio,
            log_name="program-stock-backfill",
        )
    except SourceError as exc:
        log.error("프로그램매매 종목별 백필 중단: %s", exc)
        aborted = True
        results = []
    days, _rows = _write_by_day(snapshots, results)
    status = "aborted" if aborted else "ok"
    return BackfillSummary(
        status=status, sessions=days, loaded=len(results), skipped=resume_skipped, failed=[]
    )


def _coverage(frame: pl.DataFrame) -> str:
    days = frame.select("day").n_unique()
    low = frame.select(pl.col("day").min()).item()
    high = frame.select(pl.col("day").max()).item()
    return f"{days} days, {frame.height} rows, {low}..{high}"


def _total_identity_violations(frame: pl.DataFrame) -> int:
    total = frame.filter(pl.col("component") == "total")
    parts = (
        frame.filter(pl.col("component") != "total")
        .group_by("day", "market")
        .agg(
            pl.col("sell_qty").sum().alias("p_sell_qty"),
            pl.col("buy_qty").sum().alias("p_buy_qty"),
            pl.col("sell_value").sum().alias("p_sell_value"),
            pl.col("buy_value").sum().alias("p_buy_value"),
        )
    )
    merged = total.join(parts, on=["day", "market"], how="inner")
    return merged.filter(
        ((pl.col("sell_qty") - pl.col("p_sell_qty")).abs() > IDENTITY_TOL)
        | ((pl.col("buy_qty") - pl.col("p_buy_qty")).abs() > IDENTITY_TOL)
        | ((pl.col("sell_value") - pl.col("p_sell_value")).abs() > IDENTITY_TOL)
        | ((pl.col("buy_value") - pl.col("p_buy_value")).abs() > IDENTITY_TOL)
    ).height


def _verify_market(snapshots: DatePartitionedStore) -> str:
    scan = snapshots.scan(PROGRAM_MARKET_1D)
    if scan is None:
        return "empty"
    frame = scan.collect()
    if frame.is_empty():
        return "empty"
    issues: list[str] = []
    bad_component = frame.filter(~pl.col("component").is_in(list(COMPONENTS))).height
    if bad_component:
        issues.append(f"bad-component {bad_component}")
    duplicate = frame.height - frame.select("day", "market", "component").unique().height
    if duplicate:
        issues.append(f"dup-key {duplicate}")
    qty_off = (pl.col("net_qty") - (pl.col("buy_qty") - pl.col("sell_qty"))).abs()
    value_off = (pl.col("net_value") - (pl.col("buy_value") - pl.col("sell_value"))).abs()
    net_bad = frame.filter((qty_off > IDENTITY_TOL) | (value_off > IDENTITY_TOL)).height
    if net_bad:
        issues.append(f"net-identity {net_bad}")
    total_bad = _total_identity_violations(frame)
    if total_bad:
        issues.append(f"total-identity {total_bad}")
    return "issues: " + "; ".join(issues) if issues else f"ok — {_coverage(frame)}"


def _verify_stock(snapshots: DatePartitionedStore) -> str:
    scan = snapshots.scan(PROGRAM_STOCK_1D)
    if scan is None:
        return "empty"
    frame = scan.collect()
    if frame.is_empty():
        return "empty"
    issues: list[str] = []
    duplicate = frame.height - frame.select("day", "symbol").unique().height
    if duplicate:
        issues.append(f"dup-key {duplicate}")
    complete = (
        pl.col("net_qty").is_not_null()
        & pl.col("buy_qty").is_not_null()
        & pl.col("sell_qty").is_not_null()
    )
    complete_value = (
        pl.col("net_value").is_not_null()
        & pl.col("buy_value").is_not_null()
        & pl.col("sell_value").is_not_null()
    )
    net_bad = frame.filter(
        (
            complete
            & ((pl.col("net_qty") - (pl.col("buy_qty") - pl.col("sell_qty"))).abs() > IDENTITY_TOL)
        )
        | (
            complete_value
            & (
                (pl.col("net_value") - (pl.col("buy_value") - pl.col("sell_value"))).abs()
                > IDENTITY_TOL
            )
        )
    ).height
    if net_bad:
        issues.append(f"net-identity {net_bad}")
    return "issues: " + "; ".join(issues) if issues else f"ok — {_coverage(frame)}"


def verify_program(
    cfg: TalonSettings,
    *,
    snapshots: DatePartitionedStore,
    parts: tuple[str, ...] = ("market", "stock"),
) -> ProgramVerifyReport:
    market = _verify_market(snapshots) if "market" in parts else ""
    stock = _verify_stock(snapshots) if "stock" in parts else ""
    selected = [status for status in (market, stock) if status]
    ok = all(status == "empty" or status.startswith("ok") for status in selected)
    return ProgramVerifyReport(status="ok" if ok else "issues", market=market, stock=stock)

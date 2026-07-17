import logging
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    OVERTIME_MARKET,
    OVERTIME_MARKET_SCHEMA,
    OVERTIME_PRICE,
    OVERTIME_PRICE_SCHEMA,
    OVERTIME_RANKING,
    OVERTIME_RANKING_SCHEMA,
    DatePartitionedStore,
)
from talon.ingest.close_auction import auction_symbols
from talon.ingest.kis_sweep import MAX_FAILURE_RATIO
from talon.ingest.pool import parallel_fetch
from talon.markets.kr import KrxCalendar
from talon.models import OvertimeSummary
from talon.notify.telegram import Alerter
from talon.sources.kis import KisClient, build_kis_client
from talon.sources.kis_market import fetch_overtime_price, fetch_overtime_ranking
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

SESSION_CLOSE = time(18, 0)
OVERTIME_SIDES = ("up", "down")
OVERTIME_SCOPE = "all"
PRICE_KEY = ("symbol",)
RANKING_KEY = ("side", "rank")
MARKET_KEY = ("scope",)


def run_overtime(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    alerter: Alerter,
    today: date | None = None,
    force: bool = False,
    now: Callable[[], datetime] = now_utc,
) -> OvertimeSummary:
    day = today or now().astimezone(KST).date()
    if not force and not cal.is_trading_day(day):
        return OvertimeSummary(status="skipped-holiday", day=day)
    if not cfg.kis_configured:
        alerter.error("overtime-no-kis", "KIS 앱키가 없어 시간외 시세를 못 받습니다")
        return OvertimeSummary(status="no-kis", day=day)
    if not force and now().astimezone(KST).time() < SESSION_CLOSE:
        return OvertimeSummary(status="too-early", day=day)
    symbols = auction_symbols(cfg, snapshots, day)
    if not symbols:
        alerter.error(
            "overtime-no-universe",
            f"{day} 15:10 스냅샷도 pinned 종목도 없어 시간외 시세를 못 받습니다",
        )
        return OvertimeSummary(status="no-universe", day=day)

    run_id = state.start_job("overtime")
    summary = OvertimeSummary(status="ok", day=day, symbols=len(symbols))
    with build_kis_client(cfg) as client:
        price_status, price_rows = _collect_price(
            client, snapshots, day, symbols, cfg.kis_workers, now
        )
        summary.parts["overtime_price"] = price_status
        summary.rows["overtime_price"] = price_rows
        rank_status, rank_rows = _collect_rank(client, snapshots, day, now)
        summary.parts["overtime_rank"] = rank_status
        summary.rows["overtime_rank"] = rank_rows

    errors = [label for label, status in summary.parts.items() if status.startswith("error")]
    stored = sum(summary.rows.values())
    if stored == 0:
        summary.status = "error"
        alerter.error(
            "overtime-error",
            f"{day} 시간외 수집이 한 행도 안 남았습니다: {summary.parts}",
        )
    elif errors:
        alerter.warning(
            "overtime-partial",
            f"{day} 시간외 수집 일부 실패: {errors}",
        )

    ok = summary.status == "ok"
    detail: dict[str, object] = {
        "day": day.isoformat(),
        "symbols": len(symbols),
        "parts": summary.parts,
        "rows": summary.rows,
    }
    state.heartbeat("overtime", ok, detail)
    state.finish_job(run_id, ok, detail)
    return summary


def _collect_price(
    client: KisClient,
    snapshots: DatePartitionedStore,
    day: date,
    symbols: list[str],
    workers: int,
    now: Callable[[], datetime],
) -> tuple[str, int]:
    try:
        fetched, failed = parallel_fetch(
            symbols,
            lambda symbol: fetch_overtime_price(client, symbol),
            workers=workers,
            max_failure_ratio=MAX_FAILURE_RATIO,
            log_name="overtime price",
            now=now,
        )
    except Exception as exc:
        log.exception("overtime price aborted")
        return f"error: {exc}", 0
    records = [
        {"day": day, "captured_at": captured_at, **row}
        for _, row, captured_at in fetched
        if row is not None
    ]
    if not records:
        return "empty", 0
    frame = pl.DataFrame(records, schema=OVERTIME_PRICE_SCHEMA)
    rows = snapshots.upsert_date(OVERTIME_PRICE, day, frame, PRICE_KEY)
    status = "ok" if failed == 0 else f"partial: {failed}종목 실패"
    return status, rows


def _collect_rank(
    client: KisClient,
    snapshots: DatePartitionedStore,
    day: date,
    now: Callable[[], datetime],
) -> tuple[str, int]:
    rank_records: list[dict[str, Any]] = []
    market_record: dict[str, Any] | None = None
    try:
        for side in OVERTIME_SIDES:
            captured_at = now()
            result = fetch_overtime_ranking(client, side)
            for row in result["rows"]:
                rank_records.append({"day": day, "captured_at": captured_at, **row})
            if side == "up" and result["market"] is not None:
                market_record = {
                    "day": day,
                    "scope": OVERTIME_SCOPE,
                    "captured_at": captured_at,
                    **result["market"],
                }
    except Exception as exc:
        log.exception("overtime rank aborted")
        return f"error: {exc}", 0
    if not rank_records and market_record is None:
        return "empty", 0
    rows = 0
    if rank_records:
        frame = pl.DataFrame(rank_records, schema=OVERTIME_RANKING_SCHEMA)
        rows += snapshots.upsert_date(OVERTIME_RANKING, day, frame, RANKING_KEY)
    if market_record is not None:
        market_frame = pl.DataFrame([market_record], schema=OVERTIME_MARKET_SCHEMA)
        rows += snapshots.upsert_date(OVERTIME_MARKET, day, market_frame, MARKET_KEY)
    return "ok", rows

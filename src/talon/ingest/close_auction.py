import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    CLOSE_AUCTION_INTRADAY,
    INTRADAY_SNAPSHOT,
    ORDERBOOK_INTRADAY_SCHEMA,
    DatePartitionedStore,
)
from talon.ingest.intraday import DECISION_SLOT
from talon.ingest.kis_sweep import MAX_FAILURE_RATIO, SWEEP_KEY, sweep_symbols
from talon.ingest.pool import parallel_fetch
from talon.markets.kr import KrxCalendar
from talon.models import CloseAuctionSummary
from talon.notify.telegram import Alerter
from talon.sources.kis import KisClient, build_kis_client
from talon.sources.kis_market import fetch_orderbook
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

PASSES = ("15:21", "15:23", "15:25", "15:27", "15:29")
LATE_TOLERANCE = timedelta(seconds=40)


def run_close_auction(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    alerter: Alerter,
    today: date | None = None,
    force: bool = False,
    now: Callable[[], datetime] = now_utc,
    sleep: Callable[[float], None] = time.sleep,
) -> CloseAuctionSummary:
    day = today or now().astimezone(KST).date()
    if not force and not cal.is_trading_day(day):
        return CloseAuctionSummary(status="skipped-holiday", day=day)
    if not cfg.kis_configured:
        alerter.error("close-auction-no-kis", "KIS 앱키가 없어 종가 예상체결을 못 받습니다")
        return CloseAuctionSummary(status="no-kis", day=day)
    symbols = auction_symbols(cfg, snapshots, day)
    if not symbols:
        alerter.error(
            "close-auction-no-universe",
            f"{day} 15:10 스냅샷도 pinned 종목도 없어 종가 예상체결을 못 받습니다",
        )
        return CloseAuctionSummary(status="no-universe", day=day)

    run_id = state.start_job("close-auction")
    summary = CloseAuctionSummary(status="ok", day=day, symbols=len(symbols))
    with build_kis_client(cfg) as client:
        for label in PASSES:
            target = _pass_target(day, label)
            current = now().astimezone(KST)
            if current < target:
                sleep((target - current).total_seconds())
            elif current - target > LATE_TOLERANCE:
                summary.passes[label] = "missed"
                summary.rows[label] = 0
                continue
            status, rows = _collect_pass(
                client, snapshots, day, label, symbols, cfg.kis_workers, now
            )
            summary.passes[label] = status
            summary.rows[label] = rows

    missed = [label for label, status in summary.passes.items() if status == "missed"]
    errors = [label for label, status in summary.passes.items() if status.startswith("error")]
    stored = sum(summary.rows.values())
    if len(missed) == len(PASSES):
        summary.status = "missed"
        alerter.error(
            "close-auction-missed",
            f"{day} 종가 예상체결 잡이 너무 늦게 떠서 동시호가 패스를 전부 놓쳤습니다",
        )
    elif stored == 0:
        summary.status = "error"
        alerter.error(
            "close-auction-error",
            f"{day} 종가 예상체결이 한 행도 안 남았습니다: {summary.passes}",
        )
    elif errors or missed:
        alerter.warning(
            "close-auction-partial",
            f"{day} 종가 예상체결 일부 패스 누락: "
            f"실패 {errors or '없음'} / 놓침 {missed or '없음'}",
        )

    ok = summary.status == "ok"
    detail: dict[str, object] = {
        "day": day.isoformat(),
        "symbols": len(symbols),
        "passes": summary.passes,
        "rows": summary.rows,
    }
    state.heartbeat("close-auction", ok, detail)
    state.finish_job(run_id, ok, detail)
    return summary


def auction_symbols(cfg: TalonSettings, snapshots: DatePartitionedStore, day: date) -> list[str]:
    frame = snapshots.read_date(INTRADAY_SNAPSHOT, day)
    if frame is None:
        return sweep_symbols(cfg, None)
    decision = frame.filter(pl.col("slot") == DECISION_SLOT)
    return sweep_symbols(cfg, decision if not decision.is_empty() else None)


def _pass_target(day: date, label: str) -> datetime:
    hour, minute = label.split(":")
    return datetime(day.year, day.month, day.day, int(hour), int(minute), tzinfo=KST)


def _collect_pass(
    client: KisClient,
    snapshots: DatePartitionedStore,
    day: date,
    label: str,
    symbols: list[str],
    workers: int,
    now: Callable[[], datetime],
) -> tuple[str, int]:
    try:
        fetched, failed = parallel_fetch(
            symbols,
            lambda symbol: fetch_orderbook(client, symbol),
            workers=workers,
            max_failure_ratio=MAX_FAILURE_RATIO,
            log_name="close auction",
            now=now,
        )
    except Exception as exc:
        log.exception("close auction pass %s aborted", label)
        return f"error: {exc}", 0
    records = [
        {"day": day, "slot": label, "captured_at": captured_at, **row}
        for _, row, captured_at in fetched
        if row is not None
    ]
    if not records:
        return "empty", 0
    frame = pl.DataFrame(records, schema=ORDERBOOK_INTRADAY_SCHEMA)
    rows = snapshots.upsert_date(CLOSE_AUCTION_INTRADAY, day, frame, SWEEP_KEY)
    status = "ok" if failed == 0 else f"partial: {failed}종목 실패"
    return status, rows

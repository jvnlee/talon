import logging
from datetime import date

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import INTRADAY_SNAPSHOT, DatePartitionedStore
from talon.markets.kr import KrxCalendar
from talon.models import IntradaySummary
from talon.notify.telegram import Alerter
from talon.sources.krx_daily import KrxCredentials, fetch_daily_ohlcv
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

DECISION_SLOT = "15:10"
AUCTION_SLOT = "15:35"
SLOTS = (DECISION_SLOT, AUCTION_SLOT)
SNAPSHOT_KEY = ("slot", "symbol")
MIN_ROWS = 1_000


def run_intraday(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    alerter: Alerter,
    slot: str,
    today: date | None = None,
    force: bool = False,
) -> IntradaySummary:
    if slot not in SLOTS:
        raise ValueError(f"알 수 없는 스냅샷 슬롯: {slot!r} (지원: {list(SLOTS)})")
    day = today or now_utc().astimezone(KST).date()
    if not force and not cal.is_trading_day(day):
        return IntradaySummary(status="skipped-holiday", day=day, slot=slot)
    if not cfg.krx_login_configured:
        alerter.alert("intraday-no-credentials", "KRX 로그인 정보가 없어 장중 스냅샷을 못 받습니다")
        return IntradaySummary(status="no-credentials", day=day, slot=slot)

    run_id = state.start_job("intraday")
    try:
        frame = fetch_daily_ohlcv(day, credentials=KrxCredentials(cfg.krx_id, cfg.krx_password))
    except Exception as exc:
        log.exception("intraday snapshot failed")
        detail: dict[str, object] = {"day": day.isoformat(), "slot": slot, "error": str(exc)}
        state.heartbeat("intraday", False, detail)
        state.finish_job(run_id, False, detail)
        alerter.alert("intraday-error", f"{day} {slot} 장중 스냅샷 실패: {exc}")
        return IntradaySummary(status="error", day=day, slot=slot)

    if frame.height < MIN_ROWS:
        thin: dict[str, object] = {"day": day.isoformat(), "slot": slot, "rows": frame.height}
        state.heartbeat("intraday", False, thin)
        state.finish_job(run_id, False, thin)
        alerter.alert(
            "intraday-empty",
            f"{day} {slot} 장중 스냅샷이 {frame.height}종목뿐입니다 "
            "(KRX가 아직 안 채웠거나 막혔습니다)",
        )
        return IntradaySummary(status="data-not-ready", day=day, slot=slot, rows=frame.height)

    prepared = frame.with_columns(
        pl.lit(slot).alias("slot"),
        pl.lit(now_utc()).alias("captured_at"),
    )
    rows = snapshots.upsert_date(INTRADAY_SNAPSHOT, day, prepared, SNAPSHOT_KEY)
    done: dict[str, object] = {"day": day.isoformat(), "slot": slot, "rows": rows}
    state.heartbeat("intraday", True, done)
    state.finish_job(run_id, True, done)
    return IntradaySummary(status="ok", day=day, slot=slot, rows=rows)

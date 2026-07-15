import logging
from datetime import date

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import INTRADAY_SNAPSHOT, DatePartitionedStore
from talon.ingest.kis_sweep import collect_kis_sweep
from talon.ingest.pulse import collect_pulse
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
    status = "ok"
    rows = 0
    stock_frame: pl.DataFrame | None = None
    detail: dict[str, object] = {"day": day.isoformat(), "slot": slot}
    try:
        frame = fetch_daily_ohlcv(day, credentials=KrxCredentials(cfg.krx_id, cfg.krx_password))
    except Exception as exc:
        log.exception("intraday snapshot failed")
        status = "error"
        detail["error"] = str(exc)
        alerter.alert("intraday-error", f"{day} {slot} 장중 스냅샷 실패: {exc}")
    else:
        if frame.height < MIN_ROWS:
            status = "data-not-ready"
            rows = frame.height
            detail["rows"] = frame.height
            alerter.alert(
                "intraday-empty",
                f"{day} {slot} 장중 스냅샷이 {frame.height}종목뿐입니다 "
                "(KRX가 아직 안 채웠거나 막혔습니다)",
            )
        else:
            stock_frame = frame
            prepared = frame.with_columns(
                pl.lit(slot).alias("slot"),
                pl.lit(now_utc()).alias("captured_at"),
            )
            rows = snapshots.upsert_date(INTRADAY_SNAPSHOT, day, prepared, SNAPSHOT_KEY)
            detail["rows"] = rows

    pulse = collect_pulse(cfg, snapshots=snapshots, slot=slot, day=day, stock_frame=stock_frame)
    if slot == DECISION_SLOT:
        kis = collect_kis_sweep(
            cfg, snapshots=snapshots, slot=slot, day=day, stock_frame=stock_frame
        )
        pulse.parts.update(kis.parts)
        pulse.rows.update(kis.rows)
    failed_parts = sorted(
        name for name, part_status in pulse.parts.items() if part_status.startswith("error")
    )
    if failed_parts:
        alerter.alert(
            "intraday-extras",
            f"{day} {slot} 부가 수집 실패: {', '.join(failed_parts)}",
        )
    detail["extras"] = pulse.parts
    detail["extra_rows"] = pulse.rows

    ok = status == "ok"
    state.heartbeat("intraday", ok, detail)
    state.finish_job(run_id, ok, detail)
    return IntradaySummary(status=status, day=day, slot=slot, rows=rows, extras=pulse.parts)

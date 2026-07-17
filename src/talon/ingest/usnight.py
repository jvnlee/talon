import logging

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import US_DAILY, US_MINUTE, ParquetStore
from talon.models import UsNightSummary
from talon.notify.telegram import Alerter
from talon.sources.yahoo import fetch_daily_bars, fetch_minute_bars

log = logging.getLogger(__name__)

JOB = "us-night"


def run_us_night(
    cfg: TalonSettings,
    *,
    state: StateDB,
    series: ParquetStore,
    alerter: Alerter,
    symbols: list[str] | None = None,
) -> UsNightSummary:
    targets = list(symbols) if symbols is not None else list(cfg.us_night_symbols)
    run_id = state.start_job(JOB)
    daily_rows = 0
    minute_rows = 0
    failed: list[str] = []
    for symbol in targets:
        try:
            daily = fetch_daily_bars(symbol)
            minutes = fetch_minute_bars(symbol)
        except Exception as exc:
            log.warning("us night fetch failed: %s (%s)", symbol, exc)
            failed.append(symbol)
            continue
        if daily.is_empty() and minutes.is_empty():
            failed.append(symbol)
            continue
        daily_rows += series.upsert(US_DAILY, symbol, daily, key="day")
        minute_rows += series.upsert(US_MINUTE, symbol, minutes, key="ts")
    if not failed:
        status = "ok"
    elif len(failed) < len(targets):
        status = "partial"
    else:
        status = "error"
    detail: dict[str, object] = {
        "symbols": len(targets),
        "daily_rows": daily_rows,
        "minute_rows": minute_rows,
        "failed": failed,
    }
    ok = status != "error"
    state.heartbeat(JOB, ok, detail)
    state.finish_job(run_id, ok, detail)
    if status == "error":
        alerter.error("us-night-error", f"미국 밤장 적재가 전부 실패했습니다 ({len(targets)}종목)")
    elif failed:
        alerter.warning(
            "us-night-partial",
            f"미국 밤장 적재 일부 실패: {', '.join(failed[:5])}",
        )
    return UsNightSummary(
        status=status,
        symbols=len(targets),
        daily_rows=daily_rows,
        minute_rows=minute_rows,
        failed=failed,
    )

import logging
from datetime import time as dtime
from datetime import timedelta

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import DAILY_CANDLES, DatePartitionedStore
from talon.markets.kr import KrxCalendar, within_session
from talon.models import WatchdogSummary
from talon.notify.telegram import Alerter
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

EOD_DEADLINE = dtime(17, 30)


def run_watchdog(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    alerter: Alerter,
    now=None,
) -> WatchdogSummary:
    now = now or now_utc()
    kst_now = now.astimezone(KST)
    day = kst_now.date()
    if not cal.is_trading_day(day):
        state.heartbeat("watchdog", True, {"status": "holiday"})
        return WatchdogSummary(status="holiday")

    issues: list[str] = []
    pre = timedelta(minutes=cfg.collect_pre_open_minutes)
    post = timedelta(minutes=cfg.collect_post_close_minutes)
    stale_after = timedelta(minutes=cfg.heartbeat_stale_minutes)

    if within_session(cal, now, pre=pre, post=post):
        collector_active_since = cal.session_open(day) - pre
        heartbeat = state.get_heartbeat("collect")
        last_beat = heartbeat.ts if heartbeat is not None else collector_active_since
        if now - max(last_beat, collector_active_since) > stale_after:
            issues.append("collect-stale")
            alerter.alert(
                "collect-stale",
                f"분봉 수집기가 {cfg.heartbeat_stale_minutes}분 이상 응답이 없습니다",
            )
        failures = state.consecutive_failures("collect")
        if failures >= 2:
            issues.append("collect-failing")
            alerter.alert("collect-failing", f"분봉 수집이 연속 {failures}회 실패했습니다")

    if kst_now.time() >= EOD_DEADLINE and not snapshots.has_date(DAILY_CANDLES, day):
        issues.append("eod-missing")
        alerter.alert("eod-missing", f"{day} 일봉 EOD 스냅샷이 아직 없습니다")

    state.heartbeat("watchdog", True, {"issues": issues})
    return WatchdogSummary(status="ok" if not issues else "issues", issues=issues)

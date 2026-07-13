import logging
from datetime import date, timedelta
from datetime import time as dtime

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import (
    ADJUST_MANIFEST,
    ADJUST_MANIFEST_NAME,
    DAILY_CANDLES,
    STOCK_INFO,
    DatePartitionedStore,
    ParquetStore,
)
from talon.markets.kr import KrxCalendar, within_session
from talon.models import WatchdogSummary
from talon.notify.telegram import Alerter
from talon.timeutil import KST, now_utc

log = logging.getLogger(__name__)

EOD_DEADLINE = dtime(17, 30)
ADJUST_DEADLINE = dtime(21, 0)


def _job_in_flight(state: StateDB, job: str) -> bool:
    runs = state.recent_runs(job, limit=1)
    return bool(runs) and runs[0].finished_at is None


def _factors_behind(
    snapshots: DatePartitionedStore,
    series: ParquetStore,
) -> tuple[date, date | None] | None:
    """load_panel은 수정계수가 없는 날을 경고만 남기고 조용히 버린다. 계수가 일봉을
    못 따라가면 백테스트가 말없이 최신 일자를 잃으므로 여기서 잡아야 한다."""
    daily_days = snapshots.dates(DAILY_CANDLES)
    if not daily_days:
        return None
    manifest = series.read(ADJUST_MANIFEST, ADJUST_MANIFEST_NAME)
    newest = (
        manifest["last_factor_day"].max()
        if manifest is not None and not manifest.is_empty()
        else None
    )
    latest_factor = newest if isinstance(newest, date) else None
    if latest_factor is not None and latest_factor >= daily_days[-1]:
        return None
    return daily_days[-1], latest_factor


def _stock_info_stale(
    snapshots: DatePartitionedStore,
    day: date,
    max_stale_days: int,
) -> tuple[bool, date | None]:
    known = [known_day for known_day in snapshots.dates(STOCK_INFO) if known_day <= day]
    latest = known[-1] if known else None
    if latest is not None and (day - latest).days <= max_stale_days:
        return False, latest
    return True, latest


def run_watchdog(
    cfg: TalonSettings,
    *,
    cal: KrxCalendar,
    state: StateDB,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
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

    stale, as_of = _stock_info_stale(snapshots, day, cfg.universe_info_max_stale_days)
    if stale:
        issues.append("stock-info-stale")
        alerter.alert(
            "stock-info-stale",
            f"종목기본정보가 {as_of or '없음'} 기준입니다 — 유니버스 갱신이 멈춥니다 "
            "(reconcile 잡과 talon stock-info backfill 확인)",
        )

    if kst_now.time() >= ADJUST_DEADLINE and not _job_in_flight(state, "adjust-build"):
        behind = _factors_behind(snapshots, series)
        if behind is not None:
            latest_daily, latest_factor = behind
            issues.append("factors-stale")
            alerter.alert(
                "factors-stale",
                f"수정계수가 일봉을 못 따라갑니다 (일봉 {latest_daily}, "
                f"계수 {latest_factor or '없음'}) — 백테스트 패널에서 해당 일자가 조용히 빠집니다",
            )

    state.heartbeat("watchdog", True, {"issues": issues})
    return WatchdogSummary(status="ok" if not issues else "issues", issues=issues)

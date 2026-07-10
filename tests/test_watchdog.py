from datetime import date, timedelta

import polars as pl

from conftest import FakeNotifier, utc
from talon.data.store import DAILY_CANDLES
from talon.ingest.watchdog import run_watchdog
from talon.notify.telegram import Alerter

IN_SESSION = utc(2026, 7, 10, 0, 20)
EVENING = utc(2026, 7, 10, 9, 0)
SATURDAY = utc(2026, 7, 11, 3, 0)


def run(cfg, cal, state, snapshots, alerter, now):
    return run_watchdog(cfg, cal=cal, state=state, snapshots=snapshots, alerter=alerter, now=now)


def test_holiday_quiet(cfg, cal, state, snapshots, alerter, notifier):
    summary = run(cfg, cal, state, snapshots, alerter, SATURDAY)
    assert summary.status == "holiday"
    assert notifier.sent == []


def test_collect_stale_alert(cfg, cal, state, snapshots, alerter, notifier):
    summary = run(cfg, cal, state, snapshots, alerter, IN_SESSION)
    assert "collect-stale" in summary.issues
    assert any("분봉 수집기" in text for text in notifier.sent)


def test_fresh_heartbeat_no_alert(cfg, cal, state, snapshots, alerter, notifier):
    state.heartbeat("collect", True, {})
    summary = run(cfg, cal, state, snapshots, alerter, IN_SESSION)
    assert summary.issues == []
    assert notifier.sent == []


def test_grace_right_after_open(cfg, cal, state, snapshots, alerter, notifier):
    summary = run(cfg, cal, state, snapshots, alerter, utc(2026, 7, 10, 0, 5))
    assert summary.issues == []


def test_consecutive_failures_alert(cfg, cal, state, snapshots, alerter, notifier):
    state.heartbeat("collect", True, {})
    for _ in range(2):
        run_id = state.start_job("collect")
        state.finish_job(run_id, False)
    summary = run(cfg, cal, state, snapshots, alerter, IN_SESSION)
    assert "collect-failing" in summary.issues
    assert any("연속 2회 실패" in text for text in notifier.sent)


def test_eod_missing_after_deadline(cfg, cal, state, snapshots, alerter, notifier):
    summary = run(cfg, cal, state, snapshots, alerter, EVENING)
    assert summary.issues == ["eod-missing"]
    assert any("EOD 스냅샷" in text for text in notifier.sent)


def test_eod_present_after_deadline(cfg, cal, state, snapshots, alerter, notifier):
    snapshots.write_date(DAILY_CANDLES, date(2026, 7, 10), pl.DataFrame({"symbol": ["005930"]}))
    summary = run(cfg, cal, state, snapshots, alerter, EVENING)
    assert summary.issues == []
    assert notifier.sent == []


def test_alert_cooldown_suppresses_repeat(cfg, cal, state, snapshots):
    notifier = FakeNotifier()
    alerter = Alerter(notifier, state, timedelta(hours=1))
    run(cfg, cal, state, snapshots, alerter, IN_SESSION)
    run(cfg, cal, state, snapshots, alerter, IN_SESSION)
    assert len(notifier.sent) == 1

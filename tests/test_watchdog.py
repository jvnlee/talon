import shutil
from datetime import date, timedelta

import polars as pl
import pytest

from conftest import FakeNotifier, utc, write_stock_info
from talon.data.store import ADJUST_MANIFEST, ADJUST_MANIFEST_NAME, DAILY_CANDLES, STOCK_INFO
from talon.ingest.factors import MANIFEST_SCHEMA
from talon.ingest.watchdog import run_watchdog
from talon.notify.telegram import Alerter

IN_SESSION = utc(2026, 7, 10, 0, 20)
EVENING = utc(2026, 7, 10, 9, 0)
NIGHT = utc(2026, 7, 10, 12, 30)
SATURDAY = utc(2026, 7, 11, 3, 0)

DAY = date(2026, 7, 10)
YESTERDAY = date(2026, 7, 9)


@pytest.fixture(autouse=True)
def fresh_stock_info(snapshots):
    write_stock_info(snapshots, [YESTERDAY], ["005930"])


def drop_stock_info(snapshots):
    shutil.rmtree(snapshots.root / STOCK_INFO)


def run(cfg, cal, state, snapshots, series, alerter, now):
    return run_watchdog(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        series=series,
        alerter=alerter,
        now=now,
    )


def seed_daily(snapshots, day=DAY):
    snapshots.write_date(DAILY_CANDLES, day, pl.DataFrame({"symbol": ["005930"]}))


def seed_manifest(series, last_factor_day):
    series.upsert(
        ADJUST_MANIFEST,
        ADJUST_MANIFEST_NAME,
        pl.DataFrame(
            {
                "symbol": ["005930"],
                "status": ["ok"],
                "raw_days": [10],
                "factor_days": [10],
                "last_raw_day": [last_factor_day],
                "last_factor_day": [last_factor_day],
            },
            schema=MANIFEST_SCHEMA,
        ),
        key="symbol",
    )


def test_holiday_quiet(cfg, cal, state, snapshots, series, alerter, notifier):
    summary = run(cfg, cal, state, snapshots, series, alerter, SATURDAY)
    assert summary.status == "holiday"
    assert notifier.sent == []


def test_collect_stale_alert(cfg, cal, state, snapshots, series, alerter, notifier):
    summary = run(cfg, cal, state, snapshots, series, alerter, IN_SESSION)
    assert "collect-stale" in summary.issues
    assert any("분봉 수집기" in text for text in notifier.sent)


def test_fresh_heartbeat_no_alert(cfg, cal, state, snapshots, series, alerter, notifier):
    state.heartbeat("collect", True, {})
    summary = run(cfg, cal, state, snapshots, series, alerter, IN_SESSION)
    assert summary.issues == []
    assert notifier.sent == []


def test_grace_right_after_open(cfg, cal, state, snapshots, series, alerter, notifier):
    summary = run(cfg, cal, state, snapshots, series, alerter, utc(2026, 7, 10, 0, 5))
    assert summary.issues == []


def test_consecutive_failures_alert(cfg, cal, state, snapshots, series, alerter, notifier):
    state.heartbeat("collect", True, {})
    for _ in range(2):
        run_id = state.start_job("collect")
        state.finish_job(run_id, False)
    summary = run(cfg, cal, state, snapshots, series, alerter, IN_SESSION)
    assert "collect-failing" in summary.issues
    assert any("연속 2회 실패" in text for text in notifier.sent)


def test_eod_missing_after_deadline(cfg, cal, state, snapshots, series, alerter, notifier):
    summary = run(cfg, cal, state, snapshots, series, alerter, EVENING)
    assert summary.issues == ["eod-missing"]
    assert any("EOD 스냅샷" in text for text in notifier.sent)


def test_eod_present_after_deadline(cfg, cal, state, snapshots, series, alerter, notifier):
    seed_daily(snapshots)
    summary = run(cfg, cal, state, snapshots, series, alerter, EVENING)
    assert summary.issues == []
    assert notifier.sent == []


def test_stale_factors_alert_at_night(cfg, cal, state, snapshots, series, alerter, notifier):
    """계수가 일봉을 못 따라가면 load_panel이 그 날짜를 조용히 버린다."""
    seed_daily(snapshots)
    seed_manifest(series, DAY - timedelta(days=3))

    summary = run(cfg, cal, state, snapshots, series, alerter, NIGHT)

    assert "factors-stale" in summary.issues
    assert any("수정계수가 일봉을 못 따라갑니다" in text for text in notifier.sent)


def test_missing_factors_alert_at_night(cfg, cal, state, snapshots, series, alerter, notifier):
    seed_daily(snapshots)
    summary = run(cfg, cal, state, snapshots, series, alerter, NIGHT)
    assert "factors-stale" in summary.issues
    assert any("계수 없음" in text for text in notifier.sent)


def test_current_factors_quiet_at_night(cfg, cal, state, snapshots, series, alerter, notifier):
    seed_daily(snapshots)
    seed_manifest(series, DAY)
    summary = run(cfg, cal, state, snapshots, series, alerter, NIGHT)
    assert summary.issues == []
    assert notifier.sent == []


def test_factors_not_checked_before_deadline(cfg, cal, state, snapshots, series, alerter, notifier):
    """eod가 일봉을 쓴 직후에는 계수가 뒤처진 게 정상이다. 20:00 잡이 따라잡는다."""
    seed_daily(snapshots)
    summary = run(cfg, cal, state, snapshots, series, alerter, EVENING)
    assert summary.issues == []


def test_no_alert_while_adjust_build_is_running(
    cfg, cal, state, snapshots, series, alerter, notifier
):
    """20:00 잡이 아직 도는 중이면 계수가 뒤처진 게 당연하다. 오경보를 내지 않는다."""
    seed_daily(snapshots)
    state.start_job("adjust-build")

    summary = run(cfg, cal, state, snapshots, series, alerter, NIGHT)

    assert summary.issues == []
    assert notifier.sent == []


def test_alert_cooldown_suppresses_repeat(cfg, cal, state, snapshots, series):
    notifier = FakeNotifier()
    alerter = Alerter(notifier, state, timedelta(hours=1))
    run(cfg, cal, state, snapshots, series, alerter, IN_SESSION)
    run(cfg, cal, state, snapshots, series, alerter, IN_SESSION)
    assert len(notifier.sent) == 1


def test_stock_info_missing_alerts(cfg, cal, state, snapshots, series, alerter, notifier):
    drop_stock_info(snapshots)
    seed_daily(snapshots)
    summary = run(cfg, cal, state, snapshots, series, alerter, EVENING)

    assert "stock-info-stale" in summary.issues
    assert any("종목기본정보가 없음 기준입니다" in text for text in notifier.sent)


def test_stock_info_stale_alerts(cfg, cal, state, snapshots, series, alerter, notifier):
    drop_stock_info(snapshots)
    write_stock_info(snapshots, [date(2026, 6, 1)], ["005930"])
    seed_daily(snapshots)
    summary = run(cfg, cal, state, snapshots, series, alerter, EVENING)

    assert "stock-info-stale" in summary.issues
    assert any("2026-06-01 기준입니다" in text for text in notifier.sent)


def test_yesterdays_stock_info_is_fresh_enough(
    cfg, cal, state, snapshots, series, alerter, notifier
):
    seed_daily(snapshots)
    summary = run(cfg, cal, state, snapshots, series, alerter, EVENING)

    assert "stock-info-stale" not in summary.issues
    assert not any("종목기본정보" in text for text in notifier.sent)

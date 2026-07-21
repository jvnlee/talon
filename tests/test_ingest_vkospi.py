from datetime import UTC, date, datetime

import polars as pl

from talon.data.store import VKOSPI_1D, VKOSPI_1D_SCHEMA
from talon.errors import SourceError
from talon.ingest.vkospi import VKOSPI_NAME, backfill_vkospi, daily_vkospi, vkospi_status
from talon.sources.krx_index import VkospiDailyBar

START = date(2026, 7, 6)
END = date(2026, 7, 10)


def bar(day: date, close: float = 20.0, change: float | None = 0.0) -> VkospiDailyBar:
    return VkospiDailyBar(day, close, close, close, close, change, 0.0)


def seed(series, day: date, close: float = 20.0, change: float | None = 0.0) -> None:
    frame = pl.DataFrame(
        [
            {
                "day": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "change": change,
                "change_pct": 0.0,
                "source": "krx",
                "fetched_at": datetime(2026, 7, 16, tzinfo=UTC),
            }
        ],
        schema=VKOSPI_1D_SCHEMA,
    )
    series.upsert(VKOSPI_1D, VKOSPI_NAME, frame, key="day")


def no_sleep(_seconds: float) -> None:
    return None


def test_backfill_loads_sessions(cfg, cal, series):
    calls: list[date] = []

    def fetch(day: date) -> VkospiDailyBar:
        calls.append(day)
        return bar(day)

    summary = backfill_vkospi(
        cfg, series, cal, start=START, end=END, fetch=fetch, sleep=no_sleep
    )
    assert summary.status == "ok"
    assert summary.sessions == 5
    assert summary.loaded == 5
    assert len(calls) == 5
    assert series.read(VKOSPI_1D, VKOSPI_NAME).height == 5


def test_backfill_skips_stored(cfg, cal, series):
    seed(series, date(2026, 7, 7))
    calls: list[date] = []

    def fetch(day: date) -> VkospiDailyBar:
        calls.append(day)
        return bar(day)

    summary = backfill_vkospi(
        cfg, series, cal, start=START, end=END, fetch=fetch, sleep=no_sleep
    )
    assert summary.skipped == 1
    assert summary.loaded == 4
    assert date(2026, 7, 7) not in calls


def test_backfill_force_refetches(cfg, cal, series):
    seed(series, date(2026, 7, 7))
    calls: list[date] = []

    def fetch(day: date) -> VkospiDailyBar:
        calls.append(day)
        return bar(day)

    summary = backfill_vkospi(
        cfg, series, cal, start=START, end=END, fetch=fetch, force=True, sleep=no_sleep
    )
    assert summary.skipped == 0
    assert summary.loaded == 5
    assert date(2026, 7, 7) in calls


def test_backfill_aborts_after_consecutive_failures(cfg, cal, series):
    calls: list[date] = []

    def fetch(day: date) -> VkospiDailyBar:
        calls.append(day)
        raise SourceError("boom")

    summary = backfill_vkospi(
        cfg, series, cal, start=START, end=END, fetch=fetch, sleep=no_sleep
    )
    assert summary.status == "aborted"
    assert len(calls) == 3
    assert len(summary.failed) == 3


def test_backfill_partial_on_isolated_failure(cfg, cal, series):
    def fetch(day: date) -> VkospiDailyBar:
        if day == date(2026, 7, 8):
            raise SourceError("boom")
        return bar(day)

    summary = backfill_vkospi(
        cfg, series, cal, start=START, end=END, fetch=fetch, sleep=no_sleep
    )
    assert summary.status == "partial"
    assert summary.loaded == 4
    assert summary.failed == ["2026-07-08"]


def test_backfill_detects_chain_violation(cfg, cal, series):
    closes = {
        date(2026, 7, 6): 20.0,
        date(2026, 7, 7): 20.0,
        date(2026, 7, 8): 25.0,
        date(2026, 7, 9): 25.0,
        date(2026, 7, 10): 25.0,
    }

    def fetch(day: date) -> VkospiDailyBar:
        return VkospiDailyBar(day, None, None, None, closes[day], 0.0, None)

    summary = backfill_vkospi(
        cfg, series, cal, start=START, end=END, fetch=fetch, sleep=no_sleep
    )
    assert summary.status == "ok"
    assert summary.chain_violations == ["2026-07-08"]


def kst(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 16, hour - 9, minute, tzinfo=UTC)


def test_daily_waits_for_ready_time(cfg, cal, series):
    calls: list[date] = []

    def fetch(day: date) -> VkospiDailyBar:
        calls.append(day)
        return bar(day)

    daily_vkospi(cfg, series=series, cal=cal, now=kst(15, 50), fetch=fetch)
    assert date(2026, 7, 16) not in calls
    assert calls

    calls.clear()
    result = daily_vkospi(cfg, series=series, cal=cal, now=kst(16, 40), fetch=fetch)
    assert calls == [date(2026, 7, 16)]
    assert result == "1/1 days"


def test_daily_up_to_date(cfg, cal, series):
    for day in cal.sessions_between(date(2026, 7, 1), date(2026, 7, 16)):
        seed(series, day)
    result = daily_vkospi(cfg, series=series, cal=cal, now=kst(18, 30), fetch=None)
    assert result == "up-to-date"


def test_daily_counts_errors(cfg, cal, series):
    for day in cal.sessions_between(date(2026, 7, 1), date(2026, 7, 15)):
        seed(series, day)

    def fetch(day: date) -> VkospiDailyBar:
        raise SourceError("boom")

    result = daily_vkospi(cfg, series=series, cal=cal, now=kst(18, 30), fetch=fetch)
    assert result == "0/1 days, errors: 1"


def test_daily_heals_recent_missing_only(cfg, cal, series):
    sessions = cal.sessions_between(date(2026, 7, 1), date(2026, 7, 15))
    for day in sessions[:-1]:
        seed(series, day)

    calls: list[date] = []

    def fetch(day: date) -> VkospiDailyBar:
        calls.append(day)
        return bar(day)

    daily_vkospi(cfg, series=series, cal=cal, now=kst(18, 30), fetch=fetch)
    assert date(2026, 7, 15) in calls
    assert date(2026, 7, 2) not in calls


def test_status_reports_clean(cfg, cal, series):
    for day in cal.sessions_between(START, END):
        seed(series, day)
    report = vkospi_status(series, cal)
    assert report.status == "ok"
    assert report.rows == 5
    assert report.missing_sessions == []
    assert report.first_day == START
    assert report.last_day == END


def test_status_flags_missing_session(cfg, cal, series):
    seed(series, date(2026, 7, 6))
    seed(series, date(2026, 7, 8))
    report = vkospi_status(series, cal)
    assert report.status == "issues"
    assert "2026-07-07" in report.missing_sessions


def test_status_flags_out_of_range(cfg, cal, series):
    seed(series, date(2026, 7, 6), close=200.0)
    report = vkospi_status(series, cal)
    assert report.status == "issues"
    assert "2026-07-06" in report.range_violations


def test_status_empty_store(cfg, cal, series):
    report = vkospi_status(series, cal)
    assert report.status == "empty"
    assert report.rows == 0

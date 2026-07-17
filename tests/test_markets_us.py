from datetime import UTC, date, datetime

import pytest

from talon.markets.us import UsCalendar, third_friday


@pytest.fixture(scope="module")
def uscal() -> UsCalendar:
    return UsCalendar()


def test_third_friday():
    assert third_friday(2026, 3) == date(2026, 3, 20)
    assert third_friday(2026, 6) == date(2026, 6, 19)


def test_latest_completed_session_after_us_close(uscal):
    at = datetime(2026, 7, 17, 21, 30, tzinfo=UTC)
    assert uscal.latest_completed_session(at) == date(2026, 7, 17)


def test_latest_completed_session_during_us_session(uscal):
    at = datetime(2026, 7, 17, 19, 0, tzinfo=UTC)
    assert uscal.latest_completed_session(at) == date(2026, 7, 16)


def test_latest_completed_session_respects_winter_close(uscal):
    after_close = datetime(2026, 1, 5, 21, 30, tzinfo=UTC)
    before_close = datetime(2026, 1, 5, 20, 30, tzinfo=UTC)
    assert uscal.latest_completed_session(after_close) == date(2026, 1, 5)
    assert uscal.latest_completed_session(before_close) == date(2026, 1, 2)


def test_mapped_session_for_kr_monday_is_us_friday(uscal):
    assert uscal.mapped_session(date(2026, 7, 13)) == date(2026, 7, 10)


def test_mapped_session_skips_us_holiday(uscal):
    assert not uscal.is_session(date(2026, 7, 3))
    assert uscal.mapped_session(date(2026, 7, 6)) == date(2026, 7, 2)


def test_early_close_detection(uscal):
    assert uscal.is_early_close(date(2025, 11, 28))
    assert not uscal.is_early_close(date(2025, 11, 26))


def test_witching_days_snap_to_previous_session_on_holiday(uscal):
    days = uscal.witching_days(date(2026, 1, 1), date(2026, 12, 31))
    assert not uscal.is_session(date(2026, 6, 19))
    assert days == [
        date(2026, 3, 20),
        date(2026, 6, 18),
        date(2026, 9, 18),
        date(2026, 12, 18),
    ]


def test_sessions_behind(uscal):
    assert uscal.sessions_behind(date(2026, 7, 17), date(2026, 7, 17)) == 0
    assert uscal.sessions_behind(date(2026, 7, 16), date(2026, 7, 17)) == 1
    assert uscal.sessions_behind(date(2026, 7, 10), date(2026, 7, 17)) == 5


def test_holidays_between(uscal):
    assert uscal.holidays_between(date(2026, 7, 1), date(2026, 7, 7)) == [date(2026, 7, 3)]

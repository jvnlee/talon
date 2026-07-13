from datetime import date, timedelta

from conftest import utc
from talon.markets.kr import within_session


def test_known_trading_days(cal):
    assert cal.is_trading_day(date(2026, 7, 10))
    assert not cal.is_trading_day(date(2026, 7, 11))
    assert not cal.is_trading_day(date(2026, 7, 12))
    assert not cal.is_trading_day(date(2026, 1, 1))


def test_session_times_utc(cal):
    day = date(2026, 7, 10)
    assert cal.session_open(day) == utc(2026, 7, 10, 0, 0)
    assert cal.session_close(day) == utc(2026, 7, 10, 6, 30)


def test_latest_and_previous_trading_day(cal):
    assert cal.latest_trading_day(date(2026, 7, 11)) == date(2026, 7, 10)
    assert cal.latest_trading_day(date(2026, 7, 10)) == date(2026, 7, 10)
    assert cal.previous_trading_day(date(2026, 7, 10)) == date(2026, 7, 9)
    assert cal.previous_trading_day(date(2026, 7, 13)) == date(2026, 7, 10)


def test_sessions_between(cal):
    sessions = cal.sessions_between(date(2026, 7, 6), date(2026, 7, 12))
    assert sessions == [
        date(2026, 7, 6),
        date(2026, 7, 7),
        date(2026, 7, 8),
        date(2026, 7, 9),
        date(2026, 7, 10),
    ]


def test_ad_hoc_closure_is_not_a_trading_day(cal):
    assert cal.is_trading_day(date(2026, 6, 2))
    assert not cal.is_trading_day(date(2026, 6, 3))
    assert cal.is_trading_day(date(2026, 6, 4))
    assert not within_session(cal, utc(2026, 6, 3, 3, 0))


def test_ad_hoc_closure_skipped_when_walking_sessions(cal):
    assert cal.latest_trading_day(date(2026, 6, 3)) == date(2026, 6, 2)
    assert cal.previous_trading_day(date(2026, 6, 4)) == date(2026, 6, 2)
    assert cal.sessions_between(date(2026, 6, 1), date(2026, 6, 5)) == [
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 4),
        date(2026, 6, 5),
    ]


def test_within_session_bounds(cal):
    pre = timedelta(minutes=5)
    post = timedelta(minutes=20)
    assert within_session(cal, utc(2026, 7, 10, 3, 0), pre=pre, post=post)
    assert within_session(cal, utc(2026, 7, 9, 23, 55), pre=pre, post=post)
    assert not within_session(cal, utc(2026, 7, 9, 23, 54), pre=pre, post=post)
    assert within_session(cal, utc(2026, 7, 10, 6, 50), pre=pre, post=post)
    assert not within_session(cal, utc(2026, 7, 10, 6, 51), pre=pre, post=post)
    assert not within_session(cal, utc(2026, 7, 11, 3, 0), pre=pre, post=post)

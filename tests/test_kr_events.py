from collections import Counter
from datetime import date

from talon.markets.kr import KrxCalendar, second_thursday
from talon.markets.kr_events import TIERS, kr_events_between


def days(events, category):
    return sorted(event.event_day for event in events if event.category == category)


def keys(events, category):
    return {event.event_key for event in events if event.category == category}


def test_monthly_option_expiries(cal):
    events = kr_events_between(cal, date(2021, 1, 1), date(2026, 12, 31))
    expiries = set(days(events, "expiry_option"))
    for expected in (
        date(2021, 12, 9),
        date(2024, 1, 11),
        date(2024, 3, 14),
        date(2025, 9, 11),
        date(2025, 12, 11),
        date(2026, 6, 11),
    ):
        assert expected in expiries


def test_holiday_adjusted_expiry_walks_back_to_prior_session(cal):
    events = kr_events_between(cal, date(2025, 10, 1), date(2025, 10, 31))
    assert second_thursday(2025, 10) == date(2025, 10, 9)
    assert date(2025, 10, 2) in days(events, "expiry_option")


def test_annual_witching_is_quarterly(cal):
    events = kr_events_between(cal, date(2024, 1, 1), date(2024, 12, 31))
    assert days(events, "expiry_witching") == [
        date(2024, 3, 14),
        date(2024, 6, 13),
        date(2024, 9, 12),
        date(2024, 12, 12),
    ]


def test_witching_and_option_share_the_day_as_separate_rows(cal):
    events = kr_events_between(cal, date(2024, 6, 1), date(2024, 6, 30))
    same_day = [e.category for e in events if e.event_day == date(2024, 6, 13)]
    assert "expiry_option" in same_day
    assert "expiry_witching" in same_day
    assert "rebalance_k200" in same_day


def test_k200_regime_transition_year(cal):
    events = kr_events_between(cal, date(2019, 1, 1), date(2020, 12, 31))
    k200 = set(days(events, "rebalance_k200"))
    assert date(2019, 6, 13) in k200
    assert date(2019, 12, 12) not in k200
    assert date(2020, 6, 11) in k200
    assert date(2020, 12, 10) in k200
    december = {
        e.event_key
        for e in events
        if e.category == "rebalance_k200" and e.event_day.month == 12
    }
    assert december == {"rebalance_k200:2020-12"}


def test_msci_overrides_shifted_reviews(cal):
    events = kr_events_between(cal, date(2019, 1, 1), date(2025, 12, 31))
    msci = set(days(events, "rebalance_msci"))
    assert date(2019, 11, 26) in msci
    assert date(2019, 11, 29) not in msci
    assert date(2021, 5, 27) in msci
    assert date(2021, 5, 31) not in msci
    assert date(2024, 11, 25) in msci
    assert date(2025, 11, 24) in msci
    assert date(2022, 5, 31) in msci
    assert "rebalance_msci:2019-11" in keys(events, "rebalance_msci")


def test_msci_uses_last_krx_session_and_self_heals_on_closure():
    clean = KrxCalendar(closures={})
    base = kr_events_between(clean, date(2026, 11, 1), date(2026, 11, 30))
    assert days(base, "rebalance_msci") == [date(2026, 11, 30)]

    closed = KrxCalendar(closures={date(2026, 11, 30): "임시휴장"})
    moved = kr_events_between(closed, date(2026, 11, 1), date(2026, 11, 30))
    assert days(moved, "rebalance_msci") == [date(2026, 11, 27)]
    assert keys(moved, "rebalance_msci") == {"rebalance_msci:2026-11"}


def test_ftse_third_friday(cal):
    events = kr_events_between(cal, date(2026, 6, 1), date(2026, 6, 30))
    assert days(events, "rebalance_ftse") == [date(2026, 6, 19)]


def test_yearend_ex_dividend_and_last_session(cal):
    events = kr_events_between(cal, date(2022, 1, 1), date(2025, 12, 31))
    ex = set(days(events, "ex_dividend_yearend"))
    assert {date(2022, 12, 28), date(2024, 12, 27), date(2025, 12, 29)} <= ex
    last = set(days(events, "year_last_session"))
    assert {date(2022, 12, 29), date(2024, 12, 30), date(2025, 12, 30)} <= last


def test_quarter_ex_dividend_rolls_back_weekend_record(cal):
    events = kr_events_between(cal, date(2024, 1, 1), date(2025, 12, 31))
    ex = set(days(events, "ex_dividend_quarter"))
    assert date(2024, 6, 27) in ex
    assert date(2024, 9, 27) in ex
    assert date(2025, 3, 28) in ex


def test_year_first_session_present(cal):
    events = kr_events_between(cal, date(2024, 1, 1), date(2024, 12, 31))
    assert days(events, "year_first_session") == [date(2024, 1, 2)]


def test_full_year_category_counts(cal):
    events = kr_events_between(cal, date(2024, 1, 1), date(2024, 12, 31))
    assert Counter(event.category for event in events) == {
        "expiry_option": 12,
        "expiry_witching": 4,
        "rebalance_k200": 2,
        "rebalance_msci": 4,
        "rebalance_ftse": 4,
        "ex_dividend_quarter": 3,
        "ex_dividend_yearend": 1,
        "year_last_session": 1,
        "year_first_session": 1,
    }


def test_weekly_categories_are_not_emitted(cal):
    events = kr_events_between(cal, date(2024, 1, 1), date(2026, 12, 31))
    emitted = {event.category for event in events}
    assert "expiry_weekly_thu" not in emitted
    assert "expiry_weekly_mon" not in emitted
    assert emitted <= set(TIERS)


def test_events_sorted_by_day_then_category(cal):
    events = kr_events_between(cal, date(2024, 1, 1), date(2024, 12, 31))
    ordered = [(event.event_day, event.category) for event in events]
    assert ordered == sorted(ordered)

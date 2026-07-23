from collections.abc import Iterator
from datetime import date, timedelta
from typing import NamedTuple

from talon.markets.kr import KrxCalendar

WITCHING_MONTHS = (3, 6, 9, 12)
MSCI_MONTHS = (2, 5, 8, 11)
MSCI_TRADE_OVERRIDES: dict[tuple[int, int], date] = {
    (2019, 11): date(2019, 11, 26),
    (2021, 5): date(2021, 5, 27),
    (2024, 11): date(2024, 11, 25),
    (2025, 11): date(2025, 11, 24),
}
FTSE_MONTHS = (3, 6, 9, 12)
K200_REBALANCE_MONTHS = (6, 12)
K200_SEMIANNUAL_FROM_YEAR = 2020
QUARTER_RECORD_DAYS = ((3, 31), (6, 30), (9, 30))

TIERS: dict[str, str] = {
    "expiry_option": "note",
    "expiry_witching": "shrink",
    "rebalance_k200": "shrink",
    "rebalance_msci": "shrink",
    "rebalance_ftse": "shrink",
    "ex_dividend_quarter": "note",
    "ex_dividend_yearend": "note",
    "year_last_session": "note",
    "year_first_session": "note",
}

DETAILS: dict[str, str] = {
    "expiry_option": "지수옵션·개별주식 선물·옵션 만기(매월 둘째 목요일, 휴장 시 직전 거래일)",
    "expiry_witching": "지수선물·지수옵션·개별주식선물·개별주식옵션 동시만기",
    "rebalance_k200": "KOSPI200·KOSDAQ150 정기변경 트레이드일(효력 익거래일)",
    "rebalance_msci": "MSCI 분기 리뷰 리밸런싱 트레이드일(월말 세션, 이동 리뷰는 공표 확정 반영)",
    "rebalance_ftse": "FTSE GEIS 분기 리뷰 리밸런싱 트레이드일(셋째 금요일, 효력 익영업일)",
    "ex_dividend_quarter": "분기배당 배당락일(3·6·9월 말 기준일)",
    "ex_dividend_yearend": "12월 결산 배당락일(연말 폐장일 기준)",
    "year_last_session": "연말 폐장일(정규 마지막 거래일)",
    "year_first_session": "연초 개장일(정규장 10:00 지연개장)",
}


class KrEvent(NamedTuple):
    event_day: date
    category: str
    event_key: str


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _month_key(category: str, year: int, month: int) -> str:
    return f"{category}:{year:04d}-{month:02d}"


def _year_key(category: str, year: int) -> str:
    return f"{category}:{year:04d}"


def _iter_months(start: date, end: date) -> Iterator[tuple[int, int]]:
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _month_last_day(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _last_session_of_month(cal: KrxCalendar, year: int, month: int) -> date | None:
    sessions = cal.sessions_between(date(year, month, 1), _month_last_day(year, month))
    return sessions[-1] if sessions else None


def _option_expiries(cal: KrxCalendar, start: date, end: date) -> list[KrEvent]:
    events: list[KrEvent] = []
    for year, month in _iter_months(start, end):
        day = cal.option_expiry_day(year, month)
        events.append(KrEvent(day, "expiry_option", _month_key("expiry_option", year, month)))
        if month in WITCHING_MONTHS:
            events.append(
                KrEvent(day, "expiry_witching", _month_key("expiry_witching", year, month))
            )
    return events


def _k200_rebalances(cal: KrxCalendar, start: date, end: date) -> list[KrEvent]:
    events: list[KrEvent] = []
    for year in range(start.year, end.year + 1):
        for month in K200_REBALANCE_MONTHS:
            if month == 12 and year < K200_SEMIANNUAL_FROM_YEAR:
                continue
            day = cal.option_expiry_day(year, month)
            events.append(KrEvent(day, "rebalance_k200", _month_key("rebalance_k200", year, month)))
    return events


def _msci_rebalances(cal: KrxCalendar, start: date, end: date) -> list[KrEvent]:
    events: list[KrEvent] = []
    for year in range(start.year, end.year + 1):
        for month in MSCI_MONTHS:
            day = MSCI_TRADE_OVERRIDES.get((year, month))
            if day is None:
                day = _last_session_of_month(cal, year, month)
            if day is not None:
                events.append(
                    KrEvent(day, "rebalance_msci", _month_key("rebalance_msci", year, month))
                )
    return events


def _ftse_rebalances(cal: KrxCalendar, start: date, end: date) -> list[KrEvent]:
    events: list[KrEvent] = []
    for year in range(start.year, end.year + 1):
        for month in FTSE_MONTHS:
            day = cal.latest_trading_day(third_friday(year, month))
            events.append(KrEvent(day, "rebalance_ftse", _month_key("rebalance_ftse", year, month)))
    return events


def _quarter_dividends(cal: KrxCalendar, start: date, end: date) -> list[KrEvent]:
    events: list[KrEvent] = []
    for year in range(start.year, end.year + 1):
        for month, day_num in QUARTER_RECORD_DAYS:
            ex = cal.previous_trading_day(cal.latest_trading_day(date(year, month, day_num)))
            events.append(
                KrEvent(ex, "ex_dividend_quarter", _month_key("ex_dividend_quarter", year, month))
            )
    return events


def _year_events(cal: KrxCalendar, start: date, end: date) -> list[KrEvent]:
    events: list[KrEvent] = []
    for year in range(start.year, end.year + 1):
        sessions = cal.sessions_between(date(year, 1, 1), date(year, 12, 31))
        if not sessions:
            continue
        first, last = sessions[0], sessions[-1]
        events.append(KrEvent(first, "year_first_session", _year_key("year_first_session", year)))
        events.append(KrEvent(last, "year_last_session", _year_key("year_last_session", year)))
        events.append(
            KrEvent(
                cal.previous_trading_day(last),
                "ex_dividend_yearend",
                _year_key("ex_dividend_yearend", year),
            )
        )
    return events


def kr_events_between(cal: KrxCalendar, start: date, end: date) -> list[KrEvent]:
    events: list[KrEvent] = []
    events += _option_expiries(cal, start, end)
    events += _k200_rebalances(cal, start, end)
    events += _msci_rebalances(cal, start, end)
    events += _ftse_rebalances(cal, start, end)
    events += _quarter_dividends(cal, start, end)
    events += _year_events(cal, start, end)
    selected = [event for event in events if start <= event.event_day <= end]
    return sorted(selected, key=lambda event: (event.event_day, event.category))

from datetime import date, datetime, timedelta
from functools import lru_cache

from talon.timeutil import KST

CLOSURES_MISSING_FROM_XKRX: dict[date, str] = {
    date(2026, 6, 3): "제9회 전국동시지방선거",
}


class KrxCalendar:
    def __init__(
        self,
        start: date = date(2004, 1, 2),
        closures: dict[date, str] | None = None,
    ) -> None:
        import exchange_calendars as xcals

        self._cal = xcals.get_calendar("XKRX", start=str(start))
        self._closures = CLOSURES_MISSING_FROM_XKRX if closures is None else closures

    def is_trading_day(self, day: date) -> bool:
        if day in self._closures:
            return False
        return bool(self._cal.is_session(str(day)))

    def session_open(self, day: date) -> datetime:
        return self._cal.session_open(str(day)).to_pydatetime()

    def session_close(self, day: date) -> datetime:
        return self._cal.session_close(str(day)).to_pydatetime()

    def latest_trading_day(self, day: date) -> date:
        session = self._cal.date_to_session(str(day), direction="previous").date()
        while session in self._closures:
            session = self._cal.date_to_session(
                str(session - timedelta(days=1)), direction="previous"
            ).date()
        return session

    def previous_trading_day(self, day: date) -> date:
        return self.latest_trading_day(day - timedelta(days=1))

    def sessions_between(self, start: date, end: date) -> list[date]:
        return [
            ts.date()
            for ts in self._cal.sessions_in_range(str(start), str(end))
            if ts.date() not in self._closures
        ]


@lru_cache(maxsize=1)
def krx_calendar() -> KrxCalendar:
    return KrxCalendar()


def within_session(
    cal: KrxCalendar,
    now: datetime,
    *,
    pre: timedelta = timedelta(0),
    post: timedelta = timedelta(0),
) -> bool:
    day = now.astimezone(KST).date()
    if not cal.is_trading_day(day):
        return False
    return cal.session_open(day) - pre <= now <= cal.session_close(day) + post

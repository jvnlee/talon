from datetime import date, datetime, timedelta
from functools import lru_cache

from talon.timeutil import KST


class KrxCalendar:
    def __init__(self, start: date = date(2004, 1, 2)) -> None:
        import exchange_calendars as xcals

        self._cal = xcals.get_calendar("XKRX", start=str(start))

    def is_trading_day(self, day: date) -> bool:
        return bool(self._cal.is_session(str(day)))

    def session_open(self, day: date) -> datetime:
        return self._cal.session_open(str(day)).to_pydatetime()

    def session_close(self, day: date) -> datetime:
        return self._cal.session_close(str(day)).to_pydatetime()

    def latest_trading_day(self, day: date) -> date:
        return self._cal.date_to_session(str(day), direction="previous").date()

    def previous_trading_day(self, day: date) -> date:
        return self.latest_trading_day(day - timedelta(days=1))

    def sessions_between(self, start: date, end: date) -> list[date]:
        return [ts.date() for ts in self._cal.sessions_in_range(str(start), str(end))]


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

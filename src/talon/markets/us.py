from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from talon.timeutil import KST

ET = ZoneInfo("America/New_York")
REGULAR_CLOSE_ET = time(16, 0)
DECISION_TIME_KST = time(15, 10)
WITCHING_MONTHS = (3, 6, 9, 12)


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


class UsCalendar:
    def __init__(self, start: date = date(2004, 1, 2)) -> None:
        import exchange_calendars as xcals

        self._cal = xcals.get_calendar("XNYS", start=str(start))

    def is_session(self, day: date) -> bool:
        return bool(self._cal.is_session(str(day)))

    def session_close(self, day: date) -> datetime:
        return self._cal.session_close(str(day)).to_pydatetime()

    def is_early_close(self, day: date) -> bool:
        return self.session_close(day).astimezone(ET).time() < REGULAR_CLOSE_ET

    def previous_session(self, day: date) -> date:
        return self._cal.date_to_session(str(day - timedelta(days=1)), direction="previous").date()

    def sessions_between(self, start: date, end: date) -> list[date]:
        return [ts.date() for ts in self._cal.sessions_in_range(str(start), str(end))]

    def latest_completed_session(self, at: datetime) -> date:
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        probe = at.astimezone(ET).date()
        session = self._cal.date_to_session(str(probe), direction="previous").date()
        while self.session_close(session) > at:
            session = self.previous_session(session)
        return session

    def mapped_session(self, kr_day: date, at: time = DECISION_TIME_KST) -> date:
        instant = datetime.combine(kr_day, at, tzinfo=KST)
        return self.latest_completed_session(instant.astimezone(UTC))

    def sessions_behind(self, last: date, expected: date) -> int:
        if last >= expected:
            return 0
        return max(len(self.sessions_between(last, expected)) - 1, 0)

    def witching_days(self, start: date, end: date) -> list[date]:
        days: list[date] = []
        year, quarter_months = start.year, WITCHING_MONTHS
        while year <= end.year:
            for month in quarter_months:
                candidate = third_friday(year, month)
                if not self.is_session(candidate):
                    candidate = self.previous_session(candidate)
                if start <= candidate <= end:
                    days.append(candidate)
            year += 1
        return sorted(days)

    def holidays_between(self, start: date, end: date) -> list[date]:
        days = []
        current = start
        while current <= end:
            if current.weekday() < 5 and not self.is_session(current):
                days.append(current)
            current += timedelta(days=1)
        return days

    def early_closes_between(self, start: date, end: date) -> list[date]:
        return [day for day in self.sessions_between(start, end) if self.is_early_close(day)]


@lru_cache(maxsize=1)
def us_calendar() -> UsCalendar:
    return UsCalendar()

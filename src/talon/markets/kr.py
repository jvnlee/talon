import json
import logging
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from talon.timeutil import KST

log = logging.getLogger(__name__)

CLOSURES_MISSING_FROM_XKRX: dict[date, str] = {
    date(2026, 6, 3): "제9회 전국동시지방선거",
    date(2026, 7, 17): "제헌절",
}

CLOSURES_FILE_NAME = "market_closures.json"


def closures_path(data_dir: Path) -> Path:
    return data_dir / CLOSURES_FILE_NAME


def load_stored_closures(path: Path) -> dict[date, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("휴장일 파일을 읽을 수 없습니다 (%s): %s", path, exc)
        return {}
    closures: dict[date, str] = {}
    for key, name in payload.items():
        try:
            closures[date.fromisoformat(key)] = str(name)
        except ValueError:
            log.warning("휴장일 파일의 날짜를 해석할 수 없습니다: %r", key)
    return closures


def save_stored_closures(path: Path, closures: dict[date, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {day.isoformat(): name for day, name in sorted(closures.items())}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def second_thursday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(3 - first.weekday()) % 7 + 7)


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

    def option_expiry_day(self, year: int, month: int) -> date:
        return self.latest_trading_day(second_thursday(year, month))

    def option_expiry_days(self, start: date, end: date) -> set[date]:
        days: set[date] = set()
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            expiry = self.option_expiry_day(year, month)
            if start <= expiry <= end:
                days.add(expiry)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return days


@lru_cache(maxsize=1)
def krx_calendar() -> KrxCalendar:
    from talon.config import TalonSettings

    closures = dict(CLOSURES_MISSING_FROM_XKRX)
    closures |= load_stored_closures(closures_path(TalonSettings().data_dir))
    return KrxCalendar(closures=closures)


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

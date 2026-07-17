import logging
import re
from datetime import date

import httpx

from talon.errors import SourceError

log = logging.getLogger(__name__)

FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FOMC_HISTORICAL_URL = "https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm"

_YEAR_RE = re.compile(r"(\d{4}) FOMC Meetings")
_MEETING_RE = re.compile(
    r"fomc-meeting__month[^>]*>\s*<strong>([A-Za-z/]+)</strong>"
    r".*?fomc-meeting__date[^>]*>([^<]+)<",
    re.S,
)
_HISTORICAL_RE = re.compile(
    r">\s*([A-Za-z]+)\s+(\d+)(?:\s*-\s*(?:([A-Za-z]+)\s+)?(\d+))?"
    r"\s*(?:\((?:unscheduled|notation vote)\)\s*)?Meeting\s*-\s*(\d{4})",
    re.I,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_number(token: str) -> int:
    key = token.strip().lower()[:3]
    if key not in _MONTHS:
        raise SourceError(f"FOMC 월 표기를 해석할 수 없습니다: {token!r}")
    return _MONTHS[key]


def parse_fomc_calendar(html: str) -> set[date]:
    matches = list(_YEAR_RE.finditer(html))
    if not matches:
        raise SourceError("FOMC 캘린더에서 연도 패널을 찾지 못했습니다 (마크업 변경 의심)")
    decisions: set[date] = set()
    for index, match in enumerate(matches):
        year = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        for month_token, date_token in _MEETING_RE.findall(html[match.end() : end]):
            days = re.findall(r"\d+", date_token)
            if not days:
                continue
            months = [part for part in month_token.split("/") if part]
            month = _month_number(months[-1])
            decisions.add(date(year, month, int(days[-1])))
    if not decisions:
        raise SourceError("FOMC 캘린더에서 회의 일정을 한 건도 얻지 못했습니다")
    return decisions


def parse_fomc_historical(html: str) -> set[date]:
    decisions: set[date] = set()
    for month1, _day1, month2, day2, year in _HISTORICAL_RE.findall(html):
        month = _month_number(month2 if month2 else month1)
        day = int(day2) if day2 else int(_day1)
        decisions.add(date(int(year), month, day))
    return decisions


def _get(url: str, timeout: float, transport: httpx.BaseTransport | None) -> str:
    try:
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceError(f"연준 페이지 요청 실패 ({url}): {exc}") from exc
    return response.text


def fetch_fomc_calendar(
    *, timeout: float = 30.0, transport: httpx.BaseTransport | None = None
) -> set[date]:
    return parse_fomc_calendar(_get(FOMC_CALENDAR_URL, timeout, transport))


def fetch_fomc_history(
    year: int, *, timeout: float = 30.0, transport: httpx.BaseTransport | None = None
) -> set[date]:
    html = _get(FOMC_HISTORICAL_URL.format(year=year), timeout, transport)
    decisions = parse_fomc_historical(html)
    if not decisions:
        raise SourceError(f"{year} FOMC 과거 페이지에서 회의 일정을 얻지 못했습니다")
    return decisions

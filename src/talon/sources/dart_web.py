import logging
import re
import time
from collections.abc import Callable
from datetime import date
from html.parser import HTMLParser

import httpx

from talon.errors import SchemaDriftError, SourceError

log = logging.getLogger(__name__)

DART_WEB_URL = "https://dart.fss.or.kr/dsac001/{tab}.do"
DART_WEB_TABS = ("mainAll", "mainO")
ROWS_PER_PAGE = 100
DART_WEB_HORIZON = date(2005, 1, 3)
DART_WEB_HEADERS = {"User-Agent": "Mozilla/5.0"}

_RCEPT_NO_RE = re.compile(r"rcpNo=(\d{14})")
_TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")
_TIME_HEADER = "시간"


class DisclosureRow:
    __slots__ = ("corp_name", "rcept_no", "received_time", "title")

    def __init__(
        self, rcept_no: str, received_time: str, corp_name: str | None, title: str | None
    ) -> None:
        self.rcept_no = rcept_no
        self.received_time = received_time
        self.corp_name = corp_name
        self.title = title


class _DisclosureListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.total_count: int | None = None
        self.time_header_seen = False
        self.rows: list[DisclosureRow] = []
        self.row_count = 0
        self._in_list = False
        self._in_thead = False
        self._in_tbody = False
        self._in_row = False
        self._cell_index = -1
        self._cap_header = False
        self._cap_time = False
        self._cap_corp = False
        self._cap_report = False
        self._header_parts: list[str] = []
        self._time_parts: list[str] = []
        self._corp_parts: list[str] = []
        self._report_parts: list[str] = []
        self._rcept_no: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "input" and values.get("id") == "totalCnt":
            raw = values.get("value")
            if raw is not None and raw.strip().isdigit():
                self.total_count = int(raw.strip())
            return
        if tag == "table" and "tbList" in (values.get("class") or ""):
            self._in_list = True
            return
        if not self._in_list:
            return
        if tag == "thead":
            self._in_thead = True
        elif tag == "tbody":
            self._in_tbody = True
        elif tag == "label" and self._in_thead:
            self._cap_header = True
            self._header_parts = []
        elif tag == "tr" and self._in_tbody:
            self._begin_row()
        elif tag == "td" and self._in_row:
            self._cell_index += 1
            self._cap_time = self._cell_index == 0
        elif tag == "a" and self._in_row:
            href = values.get("href") or ""
            match = _RCEPT_NO_RE.search(href)
            if match is not None:
                self._rcept_no = match.group(1)
                self._cap_report = True
                self._report_parts = []
            elif "openCorpInfoNew(" in href:
                self._cap_corp = True
                self._corp_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "input":
            return
        if not self._in_list:
            return
        if tag == "label" and self._cap_header:
            if "".join(self._header_parts).strip() == _TIME_HEADER:
                self.time_header_seen = True
            self._cap_header = False
        elif tag == "td":
            self._cap_time = False
        elif tag == "a":
            self._cap_report = False
            self._cap_corp = False
        elif tag == "tr" and self._in_row:
            self._finish_row()
        elif tag == "thead":
            self._in_thead = False
        elif tag == "tbody":
            self._in_tbody = False
        elif tag == "table":
            self._in_list = False

    def handle_data(self, data: str) -> None:
        if self._cap_header:
            self._header_parts.append(data)
        if self._cap_time:
            self._time_parts.append(data)
        if self._cap_corp:
            self._corp_parts.append(data)
        if self._cap_report:
            self._report_parts.append(data)

    def _begin_row(self) -> None:
        self._in_row = True
        self.row_count += 1
        self._cell_index = -1
        self._cap_time = False
        self._cap_corp = False
        self._cap_report = False
        self._time_parts = []
        self._corp_parts = []
        self._report_parts = []
        self._rcept_no = None

    def _finish_row(self) -> None:
        self._in_row = False
        received_time = "".join(self._time_parts).strip()
        if self._rcept_no is None or not _TIME_RE.match(received_time):
            return
        corp_name = " ".join("".join(self._corp_parts).split()) or None
        title = " ".join("".join(self._report_parts).split()) or None
        self.rows.append(DisclosureRow(self._rcept_no, received_time, corp_name, title))


def parse_disclosure_page(html: str) -> tuple[list[DisclosureRow], int, int]:
    parser = _DisclosureListParser()
    parser.feed(html)
    if parser.total_count is None or not parser.time_header_seen:
        raise SchemaDriftError(
            "DART 공시목록 구조가 바뀌었습니다 (totalCnt 또는 시간 헤더 없음)"
        )
    if parser.total_count > 0 and not parser.rows:
        raise SchemaDriftError(
            f"DART 공시목록 파싱 0행인데 totalCnt={parser.total_count} (구조 변경 의심)"
        )
    return parser.rows, parser.total_count, parser.row_count


def _fetch_page(
    client: httpx.Client, url: str, day: date, page: int
) -> str:
    params = {
        "selectDate": day.strftime("%Y%m%d"),
        "currentPage": str(page),
        "maxResults": str(ROWS_PER_PAGE),
        "mdayCnt": "0",
    }
    try:
        response = client.get(url, params=params, headers=DART_WEB_HEADERS)
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as exc:
        raise SourceError(f"DART 공시목록 요청 실패 ({day} {page}p): {exc}") from exc


def fetch_disclosure_day(
    day: date,
    *,
    tabs: tuple[str, ...] = DART_WEB_TABS,
    url_template: str = DART_WEB_URL,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    pause: float = 1.0,
) -> list[DisclosureRow]:
    seen: dict[str, DisclosureRow] = {}
    with httpx.Client(timeout=timeout, transport=transport) as client:
        for tab in tabs:
            url = url_template.format(tab=tab)
            page = 1
            while True:
                sleep(pause)
                rows, total, raw_count = parse_disclosure_page(
                    _fetch_page(client, url, day, page)
                )
                for row in rows:
                    seen.setdefault(row.rcept_no, row)
                pages = (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE
                if page >= pages or raw_count < ROWS_PER_PAGE:
                    break
                page += 1
    return list(seen.values())

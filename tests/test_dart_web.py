from datetime import date

import httpx
import pytest

from talon.errors import SchemaDriftError, SourceError
from talon.sources.dart_web import (
    ROWS_PER_PAGE,
    fetch_disclosure_day,
    parse_disclosure_page,
)

DAY = date(2023, 6, 1)

_ROW = """
<tr>
    <td>
        {time}
    </td>
    <td class="tL">
        <span class="innerWrap">
            <span class="tagCom_kosdaq" title="코스닥시장">코</span>
            <a href="javascript:openCorpInfoNew('{corp_code}', 'winCorpInfo');"
               title="{corp} 기업개황 새창" >
                {corp}
            </a>
        </span>
    </td>
    <td class="tL">
        <a href="/dsaf001/main.do?rcpNo={rcept}"
           id="r_{rcept}" title="{title} 공시뷰어 새창" >{title}
        </a>
    </td>
    <td class="tL ellipsis" title="제출인">제출인</td>
    <td>2023.06.01</td>
    <td></td>
</tr>
"""

_NO_DATA_ROW = '<tr><td colspan="6" class="tC">조회된 데이타가 없습니다.</td></tr>'

_ROW_NO_REPORT = """
<tr>
    <td>13:05</td>
    <td class="tL"><span>철회공시</span></td>
    <td class="tL"><span>보고서 링크 없음</span></td>
    <td class="tL ellipsis" title="제출인">제출인</td>
    <td>2023.06.01</td>
    <td></td>
</tr>
"""


def _page(rows_html: str, total: int, *, header: bool = True, total_input: bool = True) -> str:
    header_row = (
        "<thead><tr>"
        '<th scope="row"><label for="inpSample00">시간</label></th>'
        '<th scope="row"><label for="inpSample00">공시대상회사</label></th>'
        '<th scope="row"><label for="inpSample00">보고서명</label></th>'
        "</tr></thead>"
        if header
        else "<thead><tr><th>없음</th></tr></thead>"
    )
    total_html = (
        f'<input type="hidden" name="totalCnt" id="totalCnt" value="{total}">'
        if total_input
        else ""
    )
    return (
        "<html><body>"
        f"{total_html}"
        '<div class="tbListInner"><table class="tbList"><caption>목록</caption>'
        f"{header_row}<tbody>{rows_html}</tbody></table></div>"
        "</body></html>"
    )


def _row(rcept: str, time: str = "13:05", corp: str = "종목", title: str = "보고서") -> str:
    return _ROW.format(rcept=rcept, time=time, corp=corp, corp_code="00516246", title=title)


def _rows(prefixes: list[str]) -> str:
    return "".join(_row(p) for p in prefixes)


def test_parse_extracts_fields():
    html = _page(
        _row("20230601900642", time="19:34", corp="알에프세미", title="기타시장안내")
        + _row("20230601000431", time="17:21", corp="메디앙스", title="대량보유상황보고서"),
        total=2,
    )
    rows, total, raw_count = parse_disclosure_page(html)
    assert total == 2
    assert raw_count == 2
    assert [r.rcept_no for r in rows] == ["20230601900642", "20230601000431"]
    assert [r.received_time for r in rows] == ["19:34", "17:21"]
    assert [r.corp_name for r in rows] == ["알에프세미", "메디앙스"]
    assert rows[1].title == "대량보유상황보고서"


def test_parse_empty_day_is_not_drift():
    rows, total, _raw = parse_disclosure_page(_page(_NO_DATA_ROW, total=0))
    assert rows == []
    assert total == 0


def test_parse_drift_when_rows_missing_but_total_positive():
    with pytest.raises(SchemaDriftError):
        parse_disclosure_page(_page(_NO_DATA_ROW, total=5))


def test_parse_drift_when_total_input_gone():
    with pytest.raises(SchemaDriftError):
        parse_disclosure_page(_page(_row("20230601000001"), total=1, total_input=False))


def test_parse_drift_when_time_header_gone():
    with pytest.raises(SchemaDriftError):
        parse_disclosure_page(_page(_row("20230601000001"), total=1, header=False))


def _transport(pages: dict[tuple[str, int], str]):
    def handler(request: httpx.Request) -> httpx.Response:
        tab = request.url.path.rsplit("/", 1)[-1].removesuffix(".do")
        page = int(dict(request.url.params)["currentPage"])
        body = pages.get((tab, page))
        if body is None:
            return httpx.Response(500, text="unexpected")
        return httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


def test_fetch_paginates_until_total_reached():
    page1_ids = [f"202306010000{i:02d}" for i in range(ROWS_PER_PAGE)]
    page2_ids = [f"202306010001{i:02d}" for i in range(50)]
    pages = {
        ("mainAll", 1): _page(_rows(page1_ids), total=150),
        ("mainAll", 2): _page(_rows(page2_ids), total=150),
        ("mainO", 1): _page(_NO_DATA_ROW, total=0),
    }
    calls: list[float] = []
    rows = fetch_disclosure_day(
        DAY, transport=_transport(pages), sleep=calls.append
    )
    assert len(rows) == 150
    assert len(calls) == 3


def test_fetch_unions_tabs_and_dedupes_by_rcept_no():
    pages = {
        ("mainAll", 1): _page(_row("20230601000001") + _row("20230601000002"), total=2),
        ("mainO", 1): _page(_row("20230601000002") + _row("20230601000003"), total=2),
    }
    rows = fetch_disclosure_day(DAY, transport=_transport(pages), sleep=lambda _s: None)
    assert sorted(r.rcept_no for r in rows) == [
        "20230601000001",
        "20230601000002",
        "20230601000003",
    ]


def test_fetch_stops_on_short_first_page():
    pages = {
        ("mainAll", 1): _page(_row("20230601000001"), total=1),
        ("mainO", 1): _page(_NO_DATA_ROW, total=0),
    }
    seen_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_pages.append(int(dict(request.url.params)["currentPage"]))
        tab = request.url.path.rsplit("/", 1)[-1].removesuffix(".do")
        page = int(dict(request.url.params)["currentPage"])
        return httpx.Response(200, text=pages[(tab, page)])

    rows = fetch_disclosure_day(
        DAY, transport=httpx.MockTransport(handler), sleep=lambda _s: None
    )
    assert len(rows) == 1
    assert max(seen_pages) == 1


def test_fetch_continues_when_full_page_drops_a_row():
    page1_ids = [f"202306010000{i:02d}" for i in range(ROWS_PER_PAGE - 1)]
    page2_ids = [f"202306010001{i:02d}" for i in range(50)]
    pages = {
        ("mainAll", 1): _page(_rows(page1_ids) + _ROW_NO_REPORT, total=150),
        ("mainAll", 2): _page(_rows(page2_ids), total=150),
        ("mainO", 1): _page(_NO_DATA_ROW, total=0),
    }
    rows = fetch_disclosure_day(DAY, transport=_transport(pages), sleep=lambda _s: None)
    assert len(rows) == 149


def test_fetch_propagates_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with pytest.raises(SourceError):
        fetch_disclosure_day(
            DAY, transport=httpx.MockTransport(handler), sleep=lambda _s: None
        )

import logging
from datetime import date

import httpx
import polars as pl

from talon.errors import SourceError

log = logging.getLogger(__name__)

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
PAGE_COUNT = 100
STATUS_OK = "000"
STATUS_NO_DATA = "013"
DEFAULT_TYPES = ("A", "B", "D")

DART_FILINGS_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "corp_code": pl.Utf8(),
    "corp_name": pl.Utf8(),
    "corp_cls": pl.Utf8(),
    "filing_type": pl.Utf8(),
    "report_nm": pl.Utf8(),
    "rcept_no": pl.Utf8(),
}


def fetch_filings(
    api_key: str,
    day: date,
    *,
    types: tuple[str, ...] = DEFAULT_TYPES,
    url: str = DART_LIST_URL,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> pl.DataFrame:
    if not api_key:
        raise SourceError("DART API 키가 없습니다 (TALON_DART_API_KEY 설정 필요)")
    records: list[dict[str, object]] = []
    with httpx.Client(timeout=timeout, transport=transport) as client:
        for filing_type in types:
            records.extend(_fetch_type(client, url, api_key, day, filing_type))
    if not records:
        return pl.DataFrame(schema=DART_FILINGS_SCHEMA)
    return (
        pl.DataFrame(records, schema=DART_FILINGS_SCHEMA)
        .filter(pl.col("symbol").is_not_null())
        .unique(subset=["rcept_no"], keep="first")
        .sort("rcept_no")
    )


def _fetch_type(
    client: httpx.Client,
    url: str,
    api_key: str,
    day: date,
    filing_type: str,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    page = 1
    while True:
        payload = _fetch_page(client, url, api_key, day, filing_type, page)
        status = payload.get("status")
        if status == STATUS_NO_DATA:
            break
        if status != STATUS_OK:
            raise SourceError(f"DART list 오류 status={status}: {payload.get('message', '')}")
        raw_rows = payload.get("list")
        rows = raw_rows if isinstance(raw_rows, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("stock_code") or "").strip()
            records.append(
                {
                    "day": day,
                    "symbol": symbol or None,
                    "corp_code": row.get("corp_code"),
                    "corp_name": row.get("corp_name"),
                    "corp_cls": row.get("corp_cls"),
                    "filing_type": filing_type,
                    "report_nm": row.get("report_nm"),
                    "rcept_no": row.get("rcept_no"),
                }
            )
        raw_total = payload.get("total_page")
        total_page = int(raw_total) if isinstance(raw_total, int | str) else 1
        if page >= total_page:
            break
        page += 1
    return records


def _fetch_page(
    client: httpx.Client,
    url: str,
    api_key: str,
    day: date,
    filing_type: str,
    page: int,
) -> dict[str, object]:
    day_text = day.strftime("%Y%m%d")
    params = {
        "crtfc_key": api_key,
        "bgn_de": day_text,
        "end_de": day_text,
        "pblntf_ty": filing_type,
        "page_no": str(page),
        "page_count": str(PAGE_COUNT),
    }
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        return dict(response.json())
    except httpx.HTTPError as exc:
        raise SourceError(f"DART list 요청 실패: {exc}") from exc

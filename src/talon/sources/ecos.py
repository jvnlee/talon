import logging
from datetime import date, datetime

import httpx
import polars as pl

from talon.data.store import US_MACRO_DAILY_SCHEMA
from talon.errors import SourceError

log = logging.getLogger(__name__)

ECOS_BASE_URL = "https://ecos.bok.or.kr/api"
USDKRW_STAT_CODE = "731Y001"
USDKRW_ITEM_CODE = "0000001"
SANE_RANGE = (700.0, 3000.0)
MAX_ROWS = 100000


def parse_usdkrw(payload: dict, captured_at: datetime) -> pl.DataFrame:
    result = payload.get("RESULT")
    if isinstance(result, dict):
        raise SourceError(
            f"ECOS 오류 응답: {result.get('CODE')} {result.get('MESSAGE')}"
        )
    body = payload.get("StatisticSearch")
    if not isinstance(body, dict):
        raise SourceError(f"ECOS 응답에 StatisticSearch가 없습니다: {list(payload)}")
    rows = body.get("row")
    if not isinstance(rows, list) or not rows:
        raise SourceError("ECOS 환율 관측치가 한 건도 없습니다")
    records: list[dict[str, object]] = []
    low, high = SANE_RANGE
    for row in rows:
        time_text = str(row.get("TIME", "")).strip()
        value_text = str(row.get("DATA_VALUE", "")).strip()
        if len(time_text) != 8 or not time_text.isdigit() or not value_text:
            continue
        value = float(value_text)
        if not low <= value <= high:
            raise SourceError(f"ECOS 환율 {value}가 정상 범위({low}~{high}) 밖입니다")
        records.append(
            {
                "day": date(int(time_text[:4]), int(time_text[4:6]), int(time_text[6:8])),
                "value": value,
                "source": "ecos",
                "captured_at": captured_at,
            }
        )
    if not records:
        raise SourceError("ECOS 환율 응답을 한 행도 해석하지 못했습니다")
    return pl.DataFrame(records, schema=US_MACRO_DAILY_SCHEMA).sort("day")


def fetch_usdkrw_daily(
    api_key: str,
    captured_at: datetime,
    *,
    start: date,
    end: date,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> pl.DataFrame:
    if not api_key:
        raise SourceError("ECOS API 키가 없습니다 (TALON_ECOS_API_KEY)")
    path = "/".join(
        [
            ECOS_BASE_URL,
            "StatisticSearch",
            api_key,
            "json",
            "kr",
            "1",
            str(MAX_ROWS),
            USDKRW_STAT_CODE,
            "D",
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
            USDKRW_ITEM_CODE,
        ]
    )
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.get(path)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise SourceError(f"ECOS 요청 실패: {exc}") from exc
    except ValueError as exc:
        raise SourceError("ECOS 응답이 JSON이 아닙니다") from exc
    return parse_usdkrw(payload, captured_at)

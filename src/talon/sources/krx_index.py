import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any, NamedTuple

import polars as pl

from talon.errors import SchemaDriftError, SourceError
from talon.sources.krx_daily import KrxCredentials, _load_pykrx, _retry

log = logging.getLogger(__name__)

KRX_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
VKOSPI_BLD = "dbms/MDC/STAT/standard/MDCSTAT01001"
VKOSPI_CLASS_CD = "0202"
VKOSPI_INDEX_NAME = "코스피 200 변동성지수"
VKOSPI_SANE_RANGE = (5.0, 120.0)

_VKOSPI_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd",
    "X-Requested-With": "XMLHttpRequest",
}


class VkospiQuote(NamedTuple):
    price: float
    prev_close: float | None

INDEX_SNAPSHOT_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "market": pl.Utf8(),
    "name": pl.Utf8(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
    "value": pl.Float64(),
    "cap": pl.Float64(),
}

_INDEX_COLUMNS = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
    "거래대금": "value",
    "상장시가총액": "cap",
}
_INDEX_REQUIRED = {"시가", "고가", "저가", "종가", "거래량", "거래대금"}


def fetch_index_snapshot(
    day: date,
    market: str,
    *,
    credentials: KrxCredentials | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    stock = _load_pykrx(credentials)
    pdf: Any = _retry(
        lambda: stock.get_index_ohlcv_by_ticker(day.strftime("%Y%m%d"), market),
        sleep=sleep,
    )
    if pdf is None or len(pdf) == 0:
        return pl.DataFrame(schema=INDEX_SNAPSHOT_SCHEMA)
    missing = sorted(col for col in _INDEX_REQUIRED if col not in pdf.columns)
    if missing:
        raise SchemaDriftError(f"pykrx index columns missing: {missing}")
    reset = pdf.reset_index()
    names = reset[reset.columns[0]].astype(str).tolist()
    data: dict[str, Any] = {
        "day": [day] * len(names),
        "market": [market] * len(names),
        "name": names,
    }
    for source_col, target_col in _INDEX_COLUMNS.items():
        if source_col in pdf.columns:
            data[target_col] = [float(v) for v in pdf[source_col].tolist()]
        else:
            data[target_col] = [None] * len(names)
    frame = pl.DataFrame(data, schema=INDEX_SNAPSHOT_SCHEMA)
    return frame.filter(pl.col("close") > 0)


def fetch_vkospi(
    day: date,
    *,
    credentials: KrxCredentials | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> VkospiQuote:
    _load_pykrx(credentials)
    from pykrx.website.comm.webio import get_session

    def request() -> Any:
        krx_session = get_session()
        if krx_session is None:
            raise SourceError("KRX 로그인 세션을 얻지 못했습니다")
        headers = dict(krx_session.get_headers())
        headers.update(_VKOSPI_HEADERS)
        response = krx_session.session.post(
            KRX_JSON_URL,
            headers=headers,
            data={
                "bld": VKOSPI_BLD,
                "locale": "ko_KR",
                "clssCd": VKOSPI_CLASS_CD,
                "trdDd": day.strftime("%Y%m%d"),
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            },
        )
        response.raise_for_status()
        return response.json()

    body = _retry(request, sleep=sleep)
    rows = body.get("output", []) if isinstance(body, dict) else []
    return parse_vkospi_rows(rows)


def parse_vkospi_rows(rows: list[dict[str, Any]]) -> VkospiQuote:
    row = next((r for r in rows if r.get("IDX_NM") == VKOSPI_INDEX_NAME), None)
    if row is None:
        raise SourceError(
            f"KRX 파생지수 응답에 {VKOSPI_INDEX_NAME}가 없습니다 (분류 변경 의심)"
        )
    price = _parse_index_number(row.get("CLSPRC_IDX"))
    if price is None:
        raise SourceError("KRX 변동성지수 값이 비어 있습니다 (휴장이거나 아직 산출 전)")
    low, high = VKOSPI_SANE_RANGE
    if not low <= price <= high:
        raise SourceError(f"VKOSPI 값 {price}가 정상 범위({low}~{high}) 밖입니다")
    change = _parse_index_number(row.get("CMPPREVDD_IDX"))
    prev_close = round(price - change, 4) if change is not None else None
    return VkospiQuote(price, prev_close)


def _parse_index_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.replace(",", "").strip()
    if not text or text == "-":
        return None
    return float(text)

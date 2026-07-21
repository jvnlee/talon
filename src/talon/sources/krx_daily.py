import logging
import os
import time
from collections.abc import Callable
from datetime import date
from typing import Any, NamedTuple

import polars as pl

from talon.data.store import DAILY_SNAPSHOT_SCHEMA, MARKET_CAP_SCHEMA, normalize_daily_snapshot
from talon.errors import SchemaDriftError, SourceError

log = logging.getLogger(__name__)

KRX_ID_ENV = "KRX_ID"
KRX_PW_ENV = "KRX_PW"


class KrxCredentials(NamedTuple):
    krx_id: str
    password: str


def _load_pykrx(credentials: "KrxCredentials | None") -> Any:
    if credentials is not None:
        os.environ[KRX_ID_ENV] = credentials.krx_id
        os.environ[KRX_PW_ENV] = credentials.password
    elif not (os.environ.get(KRX_ID_ENV) and os.environ.get(KRX_PW_ENV)):
        raise SourceError(
            "KRX 로그인 정보가 없습니다 (TALON_KRX_ID / TALON_KRX_PASSWORD). "
            "KRX는 2025-12-27부터 회원제라 로그인 없이는 전종목 스냅샷을 받을 수 없습니다"
        )
    from pykrx import stock

    return stock


_OHLCV_COLUMNS = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
    "거래대금": "value",
    "등락률": "change_pct",
}
_OHLCV_REQUIRED = {"시가", "고가", "저가", "종가", "거래량", "거래대금"}

_CAP_COLUMNS = {
    "종가": "close",
    "시가총액": "cap",
    "거래량": "volume",
    "거래대금": "value",
    "상장주식수": "shares",
}
_CAP_REQUIRED = {"시가총액", "거래대금", "상장주식수"}


def _retry(
    func: Callable[[], Any],
    *,
    attempts: int = 3,
    base_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return func()
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                sleep(base_delay * (attempt + 1))
    raise SourceError(f"pykrx request failed: {last_error}") from last_error


def _snapshot_frame(
    pdf: Any,
    day: date,
    mapping: dict[str, str],
    required: set[str],
    schema: dict[str, pl.DataType],
) -> pl.DataFrame:
    if pdf is None or len(pdf) == 0:
        return pl.DataFrame(schema=schema)
    missing = sorted(col for col in required if col not in pdf.columns)
    if missing:
        raise SchemaDriftError(f"pykrx columns missing: {missing}")
    reset = pdf.reset_index()
    symbols = reset[reset.columns[0]].astype(str).tolist()
    data: dict[str, Any] = {
        "day": [day] * len(symbols),
        "symbol": symbols,
    }
    for source_col, target_col in mapping.items():
        if source_col in pdf.columns:
            data[target_col] = [float(v) for v in pdf[source_col].tolist()]
        else:
            data[target_col] = [None] * len(symbols)
    return pl.DataFrame(data, schema=schema)


def fetch_daily_ohlcv(
    day: date,
    *,
    credentials: KrxCredentials | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    stock = _load_pykrx(credentials)
    pdf = _retry(
        lambda: stock.get_market_ohlcv(day.strftime("%Y%m%d"), market="ALL"),
        sleep=sleep,
    )
    frame = _snapshot_frame(pdf, day, _OHLCV_COLUMNS, _OHLCV_REQUIRED, DAILY_SNAPSHOT_SCHEMA)
    if frame.is_empty():
        return frame
    return normalize_daily_snapshot(frame)


def fetch_market_cap(
    day: date,
    *,
    credentials: KrxCredentials | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    stock = _load_pykrx(credentials)
    pdf = _retry(
        lambda: stock.get_market_cap(day.strftime("%Y%m%d"), market="ALL"),
        sleep=sleep,
    )
    frame = _snapshot_frame(pdf, day, _CAP_COLUMNS, _CAP_REQUIRED, MARKET_CAP_SCHEMA)
    if frame.is_empty():
        return frame
    return frame.filter(pl.col("cap") > 0)

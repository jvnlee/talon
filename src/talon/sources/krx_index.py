import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import polars as pl

from talon.errors import SchemaDriftError
from talon.sources.krx_daily import KrxCredentials, _load_pykrx, _retry

log = logging.getLogger(__name__)

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

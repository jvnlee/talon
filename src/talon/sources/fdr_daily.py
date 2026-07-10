import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import polars as pl

from talon.errors import SchemaDriftError, SourceError

log = logging.getLogger(__name__)

_HISTORY_COLUMNS = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
}

HISTORY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Float64(),
}


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
    raise SourceError(f"FinanceDataReader request failed: {last_error}") from last_error


def fetch_symbol_history(
    symbol: str,
    start: date,
    end: date,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> pl.DataFrame:
    import FinanceDataReader as fdr

    pdf = _retry(lambda: fdr.DataReader(symbol, start.isoformat(), end.isoformat()), sleep=sleep)
    if pdf is None or len(pdf) == 0:
        return pl.DataFrame(schema=HISTORY_SCHEMA)
    missing = sorted(col for col in _HISTORY_COLUMNS if col not in pdf.columns)
    if missing:
        raise SchemaDriftError(f"FinanceDataReader columns missing: {missing}")
    reset = pdf.reset_index()
    date_col = reset.columns[0]
    data: dict[str, Any] = {
        "day": [ts.date() for ts in reset[date_col].tolist()],
    }
    for source_col, target_col in _HISTORY_COLUMNS.items():
        data[target_col] = [float(v) for v in reset[source_col].tolist()]
    return pl.DataFrame(data, schema=HISTORY_SCHEMA).sort("day")


def fetch_admin_issues() -> set[str] | None:
    import FinanceDataReader as fdr

    try:
        listing = fdr.StockListing("KRX-ADMINISTRATIVE")
    except Exception as exc:
        log.warning("admin issue listing unavailable: %s", exc)
        return None
    for column in ("Symbol", "Code"):
        if column in listing.columns:
            return {str(v) for v in listing[column].tolist()}
    log.warning("admin issue listing has unexpected columns: %s", list(listing.columns))
    return None

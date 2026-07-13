import logging
from collections.abc import Callable
from datetime import date

import polars as pl
from pydantic import BaseModel

from talon.errors import SourceError
from talon.sources.fdr_daily import fetch_symbol_history

log = logging.getLogger(__name__)


class Discrepancy(BaseModel):
    symbol: str
    field: str
    ours: float
    theirs: float


class CrosscheckResult(BaseModel):
    checked: int = 0
    discrepancies: list[Discrepancy] = []
    errors: list[str] = []


def _relative_diff(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1.0)


DEFAULT_FIELDS = ("close", "volume")


def crosscheck_daily(
    snapshot: pl.DataFrame,
    day: date,
    symbols: list[str],
    *,
    tolerance_pct: float,
    fields: tuple[str, ...] = DEFAULT_FIELDS,
    fetch_history: Callable[[str, date, date], pl.DataFrame] = fetch_symbol_history,
) -> CrosscheckResult:
    result = CrosscheckResult()
    tolerance = tolerance_pct / 100.0
    for symbol in symbols:
        ours_rows = snapshot.filter(pl.col("symbol") == symbol)
        if ours_rows.is_empty():
            result.errors.append(f"{symbol}: missing in snapshot")
            continue
        try:
            history = fetch_history(symbol, day, day)
        except SourceError as exc:
            result.errors.append(f"{symbol}: {exc}")
            continue
        theirs_rows = history.filter(pl.col("day") == day)
        if theirs_rows.is_empty():
            result.errors.append(f"{symbol}: missing in FinanceDataReader")
            continue
        result.checked += 1
        ours = ours_rows.row(0, named=True)
        theirs = theirs_rows.row(0, named=True)
        for field in fields:
            ours_value = float(ours[field])
            theirs_value = float(theirs[field])
            if _relative_diff(ours_value, theirs_value) > tolerance:
                result.discrepancies.append(
                    Discrepancy(
                        symbol=symbol,
                        field=field,
                        ours=ours_value,
                        theirs=theirs_value,
                    )
                )
    return result

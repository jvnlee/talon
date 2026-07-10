from datetime import date

import polars as pl

from talon.errors import SourceError
from talon.sources.crosscheck import crosscheck_daily

DAY = date(2026, 7, 10)


def snapshot():
    return pl.DataFrame(
        {
            "symbol": ["005930", "000660", "035720"],
            "close": [70000.0, 250000.0, 45000.0],
            "volume": [1000.0, 2000.0, 3000.0],
        }
    )


def history(close: float, volume: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "day": [DAY],
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "volume": [volume],
        }
    )


def test_crosscheck_all_matching():
    def fetch(symbol, start, end):
        return {"005930": history(70000.0, 1000.0), "000660": history(250000.0, 2000.0)}[symbol]

    result = crosscheck_daily(
        snapshot(), DAY, ["005930", "000660"], tolerance_pct=0.1, fetch_history=fetch
    )
    assert result.checked == 2
    assert result.discrepancies == []
    assert result.errors == []


def test_crosscheck_detects_mismatch():
    def fetch(symbol, start, end):
        return history(71000.0, 1000.0)

    result = crosscheck_daily(snapshot(), DAY, ["005930"], tolerance_pct=0.1, fetch_history=fetch)
    assert result.checked == 1
    assert len(result.discrepancies) == 1
    assert result.discrepancies[0].field == "close"


def test_crosscheck_collects_errors():
    def fetch(symbol, start, end):
        if symbol == "005930":
            raise SourceError("fdr down")
        return pl.DataFrame(
            schema={
                "day": pl.Date(),
                "open": pl.Float64(),
                "high": pl.Float64(),
                "low": pl.Float64(),
                "close": pl.Float64(),
                "volume": pl.Float64(),
            }
        )

    result = crosscheck_daily(
        snapshot(), DAY, ["005930", "000660", "404040"], tolerance_pct=0.1, fetch_history=fetch
    )
    assert result.checked == 0
    assert len(result.errors) == 3

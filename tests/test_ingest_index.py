from datetime import date

import polars as pl
import pytest

from talon.data.store import INDEX_DAILY
from talon.errors import SourceError
from talon.ingest.index import backfill_index
from talon.sources.fdr_daily import HISTORY_SCHEMA

START = date(2016, 1, 4)
END = date(2026, 7, 10)


def history(days):
    return pl.DataFrame(
        {
            "day": days,
            "open": [1.0] * len(days),
            "high": [1.0] * len(days),
            "low": [1.0] * len(days),
            "close": [float(i + 1) for i in range(len(days))],
            "volume": [1.0] * len(days),
        },
        schema=HISTORY_SCHEMA,
    )


def test_backfill_index_stores_and_upserts_idempotently(state, series):
    days = [date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10)]
    calls = []

    def fetch(code, start, end):
        calls.append((code, start, end))
        return history(days)

    summary = backfill_index(state=state, series=series, start=START, end=END, fetch=fetch)
    assert summary.status == "ok"
    assert summary.rows == {"KOSDAQ": 3, "KOSPI": 3}
    assert [call[0] for call in calls] == ["KQ11", "KS11"]
    assert calls[0][1:] == (START, END)

    again = backfill_index(state=state, series=series, start=START, end=END, fetch=fetch)
    assert again.rows == {"KOSDAQ": 3, "KOSPI": 3}
    stored = series.read(INDEX_DAILY, "KOSPI")
    assert stored is not None
    assert stored.height == 3


def test_backfill_index_partial_on_source_error(state, series):
    def fetch(code, start, end):
        if code == "KQ11":
            raise SourceError("down")
        return history([date(2026, 7, 10)])

    summary = backfill_index(state=state, series=series, start=START, end=END, fetch=fetch)
    assert summary.status == "partial"
    assert summary.failed == ["KOSDAQ"]
    assert summary.rows == {"KOSPI": 1}


def test_backfill_index_marks_empty_result_as_failed(state, series):
    def fetch(code, start, end):
        return history([])

    summary = backfill_index(
        state=state, series=series, start=START, end=END, symbols=["KOSPI"], fetch=fetch
    )
    assert summary.status == "error"
    assert summary.failed == ["KOSPI"]


def test_backfill_index_rejects_unknown_symbol(state, series):
    with pytest.raises(ValueError):
        backfill_index(state=state, series=series, start=START, end=END, symbols=["NASDAQ"])

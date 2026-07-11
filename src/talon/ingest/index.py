import logging
from collections.abc import Callable
from datetime import date

import polars as pl

from talon.data.state import StateDB
from talon.data.store import INDEX_DAILY, ParquetStore
from talon.errors import SourceError
from talon.models import IndexBackfillSummary
from talon.sources.fdr_daily import fetch_symbol_history

log = logging.getLogger(__name__)

INDEX_FDR_CODES = {
    "KOSPI": "KS11",
    "KOSDAQ": "KQ11",
}

IndexFetcher = Callable[[str, date, date], pl.DataFrame]


def backfill_index(
    *,
    state: StateDB,
    series: ParquetStore,
    start: date,
    end: date,
    symbols: list[str] | None = None,
    fetch: IndexFetcher | None = None,
) -> IndexBackfillSummary:
    targets = symbols if symbols is not None else sorted(INDEX_FDR_CODES)
    unknown = sorted(set(targets) - set(INDEX_FDR_CODES))
    if unknown:
        raise ValueError(f"지원하지 않는 지수: {unknown} (지원: {sorted(INDEX_FDR_CODES)})")
    if fetch is None:
        fetch = fetch_symbol_history
    run_id = state.start_job("index-backfill")
    rows: dict[str, int] = {}
    failed: list[str] = []
    for name in targets:
        try:
            frame = fetch(INDEX_FDR_CODES[name], start, end)
        except SourceError as exc:
            failed.append(name)
            log.warning("index backfill failed for %s: %s", name, exc)
            continue
        if frame.is_empty():
            failed.append(name)
            log.warning("index backfill returned no rows for %s", name)
            continue
        series.upsert(INDEX_DAILY, name, frame, key="day")
        stored = series.read(INDEX_DAILY, name)
        rows[name] = stored.height if stored is not None else frame.height
    status = "ok" if not failed else "partial" if rows else "error"
    summary = IndexBackfillSummary(status=status, rows=rows, failed=failed)
    state.finish_job(run_id, status == "ok", summary.model_dump(mode="json"))
    return summary

import logging
from collections.abc import Sequence
from datetime import datetime

from talon.data.store import MINUTE_CANDLES, ParquetStore, candles_to_frame
from talon.models import MinuteBackfillSummary
from talon.sources.toss import TossClient

log = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 220


def backfill_minutes(
    store: ParquetStore,
    client: TossClient,
    symbols: Sequence[str],
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> MinuteBackfillSummary:
    rows = 0
    failures: list[str] = []
    for index, symbol in enumerate(symbols, start=1):
        try:
            candles = client.candles_since(symbol, "1m", None, max_pages=max_pages)
        except Exception as exc:
            log.warning("minute backfill failed for %s: %s", symbol, exc)
            failures.append(symbol)
            continue
        if not candles:
            continue
        added = store.upsert(MINUTE_CANDLES, symbol, candles_to_frame(candles), key="ts")
        rows += added
        log.info(
            "minute backfill %s (%d/%d): +%d rows, oldest %s",
            symbol,
            index,
            len(symbols),
            added,
            candles[0].ts,
        )

    oldest: datetime | None = None
    for symbol in symbols:
        first = store.first_value(MINUTE_CANDLES, symbol)
        if first is None:
            continue
        if oldest is None or first < oldest:
            oldest = first

    status = "ok" if not failures else "partial"
    return MinuteBackfillSummary(
        status=status,
        symbols=len(symbols) - len(failures),
        rows=rows,
        oldest=oldest,
        failures=failures[:20],
    )

import logging
from collections.abc import Callable
from datetime import date

import polars as pl

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import DAILY_CANDLES, DatePartitionedStore, ParquetStore
from talon.errors import SourceError
from talon.ingest.factors import FactorFetcher, build_factors
from talon.models import RepairSummary
from talon.notify.telegram import Alerter
from talon.sources.marcap_daily import MarcapSource

log = logging.getLogger(__name__)

DailyFetcher = Callable[[date], tuple[pl.DataFrame, pl.DataFrame]]


def repair_daily_gaps(
    cfg: TalonSettings,
    *,
    state: StateDB,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    alerter: Alerter | None = None,
    start: date | None = None,
    end: date | None = None,
    fetch: DailyFetcher | None = None,
    factor_fetch: FactorFetcher | None = None,
    rebuild_factors: bool = True,
    throttle: float = 0.2,
    progress: Callable[[int, int, date], None] | None = None,
    factor_progress: Callable[[int, int, str], None] | None = None,
) -> RepairSummary:
    stored_days = [
        day
        for day in snapshots.dates(DAILY_CANDLES)
        if (start is None or day >= start) and (end is None or day <= end)
    ]
    if not stored_days:
        return RepairSummary(status="no-data")
    run_id = state.start_job("repair-daily")
    source: MarcapSource | None = None
    if fetch is None:
        source = MarcapSource(cfg.marcap_cache_dir)
        try:
            source.latest_available(stored_days[-1].year)
        except SourceError as exc:
            log.warning("marcap refresh failed, using cached files: %s", exc)
        fetch = source.snapshot
    inserted = 0
    repaired_days = 0
    affected: set[str] = set()
    uncovered: list[str] = []
    try:
        for index, day in enumerate(stored_days, start=1):
            try:
                official, _ = fetch(day)
            except SourceError as exc:
                uncovered.append(day.isoformat())
                log.warning("repair skipped for %s: %s", day, exc)
                official = None
            if official is not None:
                stored = snapshots.read_date(DAILY_CANDLES, day)
                if stored is not None:
                    missing = official.join(stored.select("symbol"), on="symbol", how="anti")
                    if missing.height:
                        snapshots.upsert_date(DAILY_CANDLES, day, missing, key=("day", "symbol"))
                        inserted += missing.height
                        repaired_days += 1
                        affected.update(missing.get_column("symbol").to_list())
            if progress is not None:
                progress(index, len(stored_days), day)
    finally:
        if source is not None:
            source.close()
    adjust = None
    if rebuild_factors and affected:
        adjust = build_factors(
            cfg,
            state=state,
            snapshots=snapshots,
            series=series,
            alerter=alerter,
            fetch=factor_fetch,
            symbols=sorted(affected),
            force=True,
            throttle=throttle,
            progress=factor_progress,
        )
    status = "ok" if not uncovered and (adjust is None or adjust.status == "ok") else "partial"
    summary = RepairSummary(
        status=status,
        sessions=len(stored_days),
        repaired_days=repaired_days,
        inserted_rows=inserted,
        affected_symbols=sorted(affected),
        uncovered=uncovered,
        adjust=adjust,
    )
    state.finish_job(run_id, status == "ok", summary.model_dump(mode="json"))
    return summary

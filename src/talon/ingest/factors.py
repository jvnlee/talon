import logging
import time
from collections.abc import Callable
from datetime import date

import polars as pl

from talon.config import TalonSettings
from talon.data.adjust import rebase_missed_events, stepwise_factors
from talon.data.state import StateDB
from talon.data.store import (
    ADJUST_FACTORS,
    ADJUST_MANIFEST,
    ADJUST_MANIFEST_NAME,
    DAILY_CANDLES,
    DatePartitionedStore,
    ParquetStore,
)
from talon.errors import SchemaDriftError, SourceError
from talon.models import AdjustSummary
from talon.notify.telegram import Alerter
from talon.sources.fdr_daily import fetch_symbol_history

log = logging.getLogger(__name__)

FactorFetcher = Callable[[str, date, date], pl.DataFrame]

MANIFEST_NAME = ADJUST_MANIFEST_NAME

MANIFEST_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8(),
    "status": pl.Utf8(),
    "raw_days": pl.Int64(),
    "factor_days": pl.Int64(),
    "last_raw_day": pl.Date(),
    "last_factor_day": pl.Date(),
}


def _load_raw_closes(snapshots: DatePartitionedStore) -> dict[str, pl.DataFrame]:
    scan = snapshots.scan(DAILY_CANDLES)
    if scan is None:
        return {}
    raw = scan.select("day", "symbol", "close", "change_pct").collect()
    return {
        str(key[0]): frame.select("day", "close", "change_pct").sort("day")
        for key, frame in raw.partition_by("symbol", as_dict=True).items()
    }


def _is_fresh(series: ParquetStore, symbol: str, last_raw_day: date) -> bool:
    last_factor_day = series.last_value(ADJUST_FACTORS, symbol, "day")
    return last_factor_day is not None and last_factor_day >= last_raw_day


def _manifest_row(
    symbol: str,
    status: str,
    raw_days: int,
    last_raw_day: date,
    factors: pl.DataFrame | None,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "status": status,
        "raw_days": raw_days,
        "factor_days": factors.height if factors is not None else 0,
        "last_raw_day": last_raw_day,
        "last_factor_day": factors["day"].max() if factors is not None else None,
    }


def build_factors(
    cfg: TalonSettings,
    *,
    state: StateDB,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    alerter: Alerter | None = None,
    fetch: FactorFetcher | None = None,
    symbols: list[str] | None = None,
    force: bool = False,
    throttle: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[int, int, str], None] | None = None,
) -> AdjustSummary:
    if fetch is None:
        fetch = fetch_symbol_history
    raw_by_symbol = _load_raw_closes(snapshots)
    if not raw_by_symbol:
        return AdjustSummary(status="no-data")
    targets = sorted(set(symbols)) if symbols else sorted(raw_by_symbol)
    run_id = state.start_job("adjust-build")
    computed = 0
    skipped = 0
    empty: list[str] = []
    failed: list[str] = []
    rebased: list[str] = []
    manifest_rows: list[dict[str, object]] = []
    for index, symbol in enumerate(targets, start=1):
        raw = raw_by_symbol.get(symbol)
        if raw is None:
            failed.append(symbol)
            log.warning("no daily snapshot rows for %s", symbol)
            continue
        first_raw_day: date = raw.item(0, "day")
        last_raw_day: date = raw.item(raw.height - 1, "day")
        if not force and _is_fresh(series, symbol, last_raw_day):
            skipped += 1
            continue
        try:
            adjusted = fetch(symbol, first_raw_day, last_raw_day)
        except SchemaDriftError:
            raise
        except SourceError as exc:
            adjusted = None
            log.warning("adjusted history fetch failed for %s: %s", symbol, exc)
        if adjusted is None:
            failed.append(symbol)
            manifest_rows.append(_manifest_row(symbol, "failed", raw.height, last_raw_day, None))
        else:
            factors = stepwise_factors(raw, adjusted)
            if factors.is_empty():
                empty.append(symbol)
                manifest_rows.append(_manifest_row(symbol, "empty", raw.height, last_raw_day, None))
            else:
                bridged = rebase_missed_events(factors, raw)
                if not bridged.equals(factors):
                    rebased.append(symbol)
                    log.info("missed corporate action bridged for %s", symbol)
                series.replace(ADJUST_FACTORS, symbol, bridged)
                computed += 1
                manifest_rows.append(_manifest_row(symbol, "ok", raw.height, last_raw_day, bridged))
        if progress is not None:
            progress(index, len(targets), symbol)
        if throttle > 0:
            sleep(throttle)
    if manifest_rows:
        manifest = pl.DataFrame(manifest_rows, schema=MANIFEST_SCHEMA)
        series.upsert(ADJUST_MANIFEST, MANIFEST_NAME, manifest, key="symbol")
    status = "ok" if not failed else "partial"
    summary = AdjustSummary(
        status=status,
        symbols=len(targets),
        computed=computed,
        skipped=skipped,
        empty=empty,
        failed=failed,
        rebased=rebased,
    )
    detail = summary.model_dump(mode="json")
    state.heartbeat("adjust-build", status == "ok", detail)
    state.finish_job(run_id, status == "ok", detail)
    if failed and alerter is not None:
        alerter.warning(
            "adjust-failed",
            f"수정계수 산출 실패 {len(failed)}종목: {', '.join(failed[:5])}",
        )
    return summary


def rebase_factors(
    cfg: TalonSettings,
    *,
    state: StateDB,
    snapshots: DatePartitionedStore,
    series: ParquetStore,
    symbols: list[str] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> AdjustSummary:
    raw_by_symbol = _load_raw_closes(snapshots)
    if not raw_by_symbol:
        return AdjustSummary(status="no-data")
    stored = series.names(ADJUST_FACTORS)
    targets = sorted(set(symbols)) if symbols else stored
    run_id = state.start_job("adjust-rebase")
    computed = 0
    skipped = 0
    rebased: list[str] = []
    failed: list[str] = []
    for index, symbol in enumerate(targets, start=1):
        raw = raw_by_symbol.get(symbol)
        factors = series.read(ADJUST_FACTORS, symbol)
        if raw is None or factors is None:
            failed.append(symbol)
            log.warning("no factors or daily snapshot rows for %s", symbol)
            continue
        bridged = rebase_missed_events(factors, raw)
        if bridged.equals(factors):
            skipped += 1
        else:
            series.replace(ADJUST_FACTORS, symbol, bridged)
            computed += 1
            rebased.append(symbol)
        if progress is not None:
            progress(index, len(targets), symbol)
    status = "ok" if not failed else "partial"
    summary = AdjustSummary(
        status=status,
        symbols=len(targets),
        computed=computed,
        skipped=skipped,
        failed=failed,
        rebased=rebased,
    )
    state.finish_job(run_id, status == "ok", summary.model_dump(mode="json"))
    return summary

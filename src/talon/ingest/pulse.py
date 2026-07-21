import logging
from collections.abc import Callable
from datetime import date, datetime

import polars as pl

from talon.config import TalonSettings
from talon.data.store import (
    BREADTH_INTRADAY,
    BREADTH_INTRADAY_SCHEMA,
    DART_POLL,
    DART_POLL_SCHEMA,
    INDEX_INTRADAY,
    INDEX_INTRADAY_SCHEMA,
    MACRO_INTRADAY,
    MACRO_INTRADAY_SCHEMA,
    STOCK_INFO,
    DatePartitionedStore,
)
from talon.models import PulseSummary
from talon.sources.dart import fetch_filings
from talon.sources.krx_daily import KrxCredentials
from talon.sources.krx_index import fetch_index_snapshot, fetch_vkospi
from talon.sources.yahoo import fetch_quote
from talon.timeutil import now_utc

log = logging.getLogger(__name__)

INDEX_MARKETS = ("KOSPI", "KOSDAQ")
MACRO_SERIES = (("USDKRW", "KRW=X"), ("ES_F", "ES=F"), ("NQ_F", "NQ=F"))
MACRO_SOURCE = "yahoo"
VKOSPI_SERIES = "VKOSPI"
VKOSPI_SOURCE = "krx"
ALL_MARKETS = "ALL"
INDEX_KEY = ("slot", "market", "name")
MACRO_KEY = ("slot", "series")
BREADTH_KEY = ("slot", "market")
DART_POLL_KEY = ("slot", "rcept_no")


def collect_pulse(
    cfg: TalonSettings,
    *,
    snapshots: DatePartitionedStore,
    slot: str,
    day: date,
    stock_frame: pl.DataFrame | None,
) -> PulseSummary:
    summary = PulseSummary()
    captured_at = now_utc()
    _run_part(summary, "index", lambda: _collect_index(cfg, snapshots, slot, day, captured_at))
    _run_part(summary, "macro", lambda: collect_macro(snapshots, slot, day, captured_at))
    _run_part(summary, "vkospi", lambda: _collect_vkospi(cfg, snapshots, slot, day, captured_at))
    _run_part(
        summary,
        "breadth",
        lambda: _collect_breadth(snapshots, slot, day, captured_at, stock_frame),
    )
    _run_part(summary, "dart", lambda: _collect_dart(cfg, snapshots, slot, day))
    return summary


def _run_part(summary: PulseSummary, name: str, part: Callable[[], tuple[str, int]]) -> None:
    try:
        status, rows = part()
    except Exception as exc:
        log.exception("pulse part failed: %s", name)
        summary.parts[name] = f"error: {exc}"
        summary.rows[name] = 0
        return
    summary.parts[name] = status
    summary.rows[name] = rows


def _collect_index(
    cfg: TalonSettings,
    snapshots: DatePartitionedStore,
    slot: str,
    day: date,
    captured_at: datetime,
) -> tuple[str, int]:
    if not cfg.krx_login_configured:
        return "skipped-no-credentials", 0
    credentials = KrxCredentials(cfg.krx_id, cfg.krx_password)
    frames = [
        fetch_index_snapshot(day, market, credentials=credentials) for market in INDEX_MARKETS
    ]
    non_empty = [frame for frame in frames if not frame.is_empty()]
    if not non_empty:
        return "empty", 0
    merged = pl.concat(non_empty, how="vertical")
    prepared = merged.with_columns(
        pl.lit(slot).alias("slot"),
        pl.lit(captured_at).alias("captured_at"),
    ).select(list(INDEX_INTRADAY_SCHEMA))
    rows = snapshots.upsert_date(INDEX_INTRADAY, day, prepared, INDEX_KEY)
    return "ok", rows


def collect_macro(
    snapshots: DatePartitionedStore,
    slot: str,
    day: date,
    captured_at: datetime,
) -> tuple[str, int]:
    records = []
    failed = []
    for series_name, symbol in MACRO_SERIES:
        try:
            quote = fetch_quote(symbol)
        except Exception as exc:
            log.warning("macro quote failed: %s (%s)", series_name, exc)
            failed.append(series_name)
            continue
        records.append(
            {
                "day": day,
                "slot": slot,
                "series": series_name,
                "captured_at": captured_at,
                "price": quote.price,
                "prev_close": quote.prev_close,
                "source": MACRO_SOURCE,
            }
        )
    if not records:
        return f"error: 전 계열 실패 ({', '.join(failed)})", 0
    frame = pl.DataFrame(records, schema=MACRO_INTRADAY_SCHEMA)
    rows = snapshots.upsert_date(MACRO_INTRADAY, day, frame, MACRO_KEY)
    if failed:
        return f"partial: {', '.join(failed)} 실패", rows
    return "ok", rows


def _collect_vkospi(
    cfg: TalonSettings,
    snapshots: DatePartitionedStore,
    slot: str,
    day: date,
    captured_at: datetime,
) -> tuple[str, int]:
    if not cfg.krx_login_configured:
        return "skipped-no-credentials", 0
    quote = fetch_vkospi(
        day=day, credentials=KrxCredentials(cfg.krx_id, cfg.krx_password)
    )
    frame = pl.DataFrame(
        [
            {
                "day": day,
                "slot": slot,
                "series": VKOSPI_SERIES,
                "captured_at": captured_at,
                "price": quote.price,
                "prev_close": quote.prev_close,
                "source": VKOSPI_SOURCE,
            }
        ],
        schema=MACRO_INTRADAY_SCHEMA,
    )
    rows = snapshots.upsert_date(MACRO_INTRADAY, day, frame, MACRO_KEY)
    return "ok", rows


def _collect_breadth(
    snapshots: DatePartitionedStore,
    slot: str,
    day: date,
    captured_at: datetime,
    stock_frame: pl.DataFrame | None,
) -> tuple[str, int]:
    if stock_frame is None or stock_frame.is_empty():
        return "skipped-no-snapshot", 0
    records = [_breadth_record(stock_frame, ALL_MARKETS, slot, day, captured_at)]
    info = _latest_stock_info(snapshots, day)
    if info is not None:
        labeled = stock_frame.join(info, on="symbol", how="inner")
        for market in sorted(labeled["market"].unique().to_list()):
            subset = labeled.filter(pl.col("market") == market)
            records.append(_breadth_record(subset, market, slot, day, captured_at))
    frame = pl.DataFrame(records, schema=BREADTH_INTRADAY_SCHEMA)
    rows = snapshots.upsert_date(BREADTH_INTRADAY, day, frame, BREADTH_KEY)
    return "ok", rows


def _breadth_record(
    frame: pl.DataFrame,
    market: str,
    slot: str,
    day: date,
    captured_at: datetime,
) -> dict[str, object]:
    change = frame["change_pct"]
    advancing = int((change > 0).sum())
    declining = int((change < 0).sum())
    total = frame.height
    return {
        "day": day,
        "slot": slot,
        "market": market,
        "captured_at": captured_at,
        "advancing": advancing,
        "declining": declining,
        "unchanged": total - advancing - declining,
        "total": total,
    }


def _latest_stock_info(snapshots: DatePartitionedStore, day: date) -> pl.DataFrame | None:
    known = [info_day for info_day in snapshots.dates(STOCK_INFO) if info_day <= day]
    if not known:
        return None
    info = snapshots.read_date(STOCK_INFO, known[-1])
    if info is None or info.is_empty():
        return None
    return info.select("symbol", "market").unique(subset=["symbol"], keep="first")


def _collect_dart(
    cfg: TalonSettings,
    snapshots: DatePartitionedStore,
    slot: str,
    day: date,
) -> tuple[str, int]:
    if not cfg.dart_api_key:
        return "skipped-no-key", 0
    filings = fetch_filings(cfg.dart_api_key, day)
    polled_at = now_utc()
    if filings.is_empty():
        return "ok-empty", 0
    prepared = filings.with_columns(
        pl.lit(slot).alias("slot"),
        pl.lit(polled_at).alias("polled_at"),
    ).select(list(DART_POLL_SCHEMA))
    rows = snapshots.upsert_date(DART_POLL, day, prepared, DART_POLL_KEY)
    return "ok", rows

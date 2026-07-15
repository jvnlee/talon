import logging
from datetime import date, datetime
from typing import Any

import polars as pl

from talon.config import TalonSettings
from talon.data.store import (
    FLOW_RANKING_INTRADAY,
    FLOW_RANKING_SCHEMA,
    FRGNMEM_RANKING_INTRADAY,
    FRGNMEM_RANKING_SCHEMA,
    FRGNMEM_TREND_INTRADAY,
    FRGNMEM_TREND_INTRADAY_SCHEMA,
    INVESTOR_ESTIMATE_INTRADAY,
    INVESTOR_ESTIMATE_SCHEMA,
    MEMBER_INTRADAY,
    MEMBER_INTRADAY_SCHEMA,
    ORDERBOOK_INTRADAY,
    ORDERBOOK_INTRADAY_SCHEMA,
    PROGRAM_MARKET_INTRADAY,
    PROGRAM_MARKET_INTRADAY_SCHEMA,
    PROGRAM_TRADE_INTRADAY,
    PROGRAM_TRADE_INTRADAY_SCHEMA,
    VOLUME_POWER_INTRADAY,
    VOLUME_POWER_INTRADAY_SCHEMA,
    DatePartitionedStore,
)
from talon.models import PulseSummary
from talon.sources.kis import KisClient
from talon.sources.kis_market import (
    fetch_flow_ranking,
    fetch_frgnmem_ranking,
    fetch_frgnmem_trend,
    fetch_investor_estimate,
    fetch_member,
    fetch_orderbook,
    fetch_program_market,
    fetch_program_trade,
    fetch_volume_power,
)
from talon.timeutil import now_utc

log = logging.getLogger(__name__)

SWEEP_KEY = ("slot", "symbol")
RANKING_KEY = ("slot", "side", "symbol")
FRGNMEM_TREND_KEY = ("slot", "symbol", "seq")
PROGRAM_MARKET_KEY = ("slot", "market", "hour")
SIDES = ("buy", "sell")
PROGRAM_MARKETS = ("K", "Q")
MAX_FAILURE_RATIO = 0.2


def collect_kis_sweep(
    cfg: TalonSettings,
    *,
    snapshots: DatePartitionedStore,
    slot: str,
    day: date,
    stock_frame: pl.DataFrame | None,
) -> PulseSummary:
    summary = PulseSummary()
    parts = (
        "kis_orderbook",
        "kis_investor",
        "kis_flow_rank",
        "kis_frgnmem",
        "kis_volume_power",
        "kis_member",
        "kis_program",
        "kis_frgnmem_trend",
        "kis_program_market",
    )
    if not cfg.kis_configured:
        for name in parts:
            summary.parts[name] = "skipped-no-kis"
            summary.rows[name] = 0
        return summary

    captured_at = now_utc()
    symbols = sweep_symbols(cfg, stock_frame)
    try:
        client = KisClient(
            cfg.kis_app_key,
            cfg.kis_app_secret,
            base_url=cfg.kis_base_url,
            token_path=cfg.kis_token_path,
            rps=cfg.kis_rps,
            timeout=cfg.request_timeout,
        )
    except Exception as exc:
        log.exception("kis client init failed")
        for name in parts:
            summary.parts[name] = f"error: {exc}"
            summary.rows[name] = 0
        return summary

    with client:
        _sweep_part(
            summary,
            "kis_orderbook",
            snapshots,
            day,
            symbols,
            lambda symbol: fetch_orderbook(client, symbol),
            ORDERBOOK_INTRADAY,
            ORDERBOOK_INTRADAY_SCHEMA,
            slot,
            captured_at,
        )
        _sweep_part(
            summary,
            "kis_investor",
            snapshots,
            day,
            symbols,
            lambda symbol: fetch_investor_estimate(client, symbol),
            INVESTOR_ESTIMATE_INTRADAY,
            INVESTOR_ESTIMATE_SCHEMA,
            slot,
            captured_at,
        )
        _fanout_part(
            summary,
            "kis_flow_rank",
            snapshots,
            day,
            SIDES,
            lambda side: fetch_flow_ranking(client, side),
            FLOW_RANKING_INTRADAY,
            FLOW_RANKING_SCHEMA,
            RANKING_KEY,
            slot,
            captured_at,
        )
        _fanout_part(
            summary,
            "kis_frgnmem",
            snapshots,
            day,
            SIDES,
            lambda side: fetch_frgnmem_ranking(client, side),
            FRGNMEM_RANKING_INTRADAY,
            FRGNMEM_RANKING_SCHEMA,
            RANKING_KEY,
            slot,
            captured_at,
        )
        _sweep_part(
            summary,
            "kis_volume_power",
            snapshots,
            day,
            symbols,
            lambda symbol: fetch_volume_power(client, symbol),
            VOLUME_POWER_INTRADAY,
            VOLUME_POWER_INTRADAY_SCHEMA,
            slot,
            captured_at,
        )
        _sweep_part(
            summary,
            "kis_member",
            snapshots,
            day,
            symbols,
            lambda symbol: fetch_member(client, symbol),
            MEMBER_INTRADAY,
            MEMBER_INTRADAY_SCHEMA,
            slot,
            captured_at,
        )
        _sweep_part(
            summary,
            "kis_program",
            snapshots,
            day,
            symbols,
            lambda symbol: fetch_program_trade(client, symbol),
            PROGRAM_TRADE_INTRADAY,
            PROGRAM_TRADE_INTRADAY_SCHEMA,
            slot,
            captured_at,
        )
        _sweep_rows_part(
            summary,
            "kis_frgnmem_trend",
            snapshots,
            day,
            symbols,
            lambda symbol: fetch_frgnmem_trend(client, symbol),
            FRGNMEM_TREND_INTRADAY,
            FRGNMEM_TREND_INTRADAY_SCHEMA,
            FRGNMEM_TREND_KEY,
            slot,
            captured_at,
        )
        _fanout_part(
            summary,
            "kis_program_market",
            snapshots,
            day,
            PROGRAM_MARKETS,
            lambda market: fetch_program_market(client, market),
            PROGRAM_MARKET_INTRADAY,
            PROGRAM_MARKET_INTRADAY_SCHEMA,
            PROGRAM_MARKET_KEY,
            slot,
            captured_at,
        )
    return summary


def sweep_symbols(cfg: TalonSettings, stock_frame: pl.DataFrame | None) -> list[str]:
    if stock_frame is None or stock_frame.is_empty():
        return [symbol for symbol in cfg.pinned_symbols if symbol]
    ranked = (
        stock_frame.filter(pl.col("value").is_not_null())
        .sort("value", descending=True)
        .head(cfg.kis_sweep_size)["symbol"]
        .to_list()
    )
    pinned = [symbol for symbol in cfg.pinned_symbols if symbol and symbol not in set(ranked)]
    return ranked + pinned


def _sweep_part(
    summary: PulseSummary,
    name: str,
    snapshots: DatePartitionedStore,
    day: date,
    symbols: list[str],
    fetch: Any,
    dataset: str,
    schema: dict[str, pl.DataType],
    slot: str,
    captured_at: datetime,
) -> None:
    if not symbols:
        summary.parts[name] = "skipped-no-universe"
        summary.rows[name] = 0
        return
    records: list[dict[str, Any]] = []
    failed = 0
    try:
        for symbol in symbols:
            try:
                row = fetch(symbol)
            except Exception as exc:
                failed += 1
                log.warning("%s fetch failed for %s: %s", name, symbol, exc)
                if failed > len(symbols) * MAX_FAILURE_RATIO:
                    raise
                continue
            if row is not None:
                records.append({"day": day, "slot": slot, "captured_at": captured_at, **row})
    except Exception as exc:
        log.exception("%s aborted", name)
        summary.parts[name] = f"error: {exc}"
        summary.rows[name] = 0
        return
    if not records:
        summary.parts[name] = "empty"
        summary.rows[name] = 0
        return
    frame = pl.DataFrame(records, schema=schema)
    rows = snapshots.upsert_date(dataset, day, frame, SWEEP_KEY)
    summary.parts[name] = "ok" if failed == 0 else f"partial: {failed}종목 실패"
    summary.rows[name] = rows


def _sweep_rows_part(
    summary: PulseSummary,
    name: str,
    snapshots: DatePartitionedStore,
    day: date,
    symbols: list[str],
    fetch: Any,
    dataset: str,
    schema: dict[str, pl.DataType],
    key: tuple[str, ...],
    slot: str,
    captured_at: datetime,
) -> None:
    if not symbols:
        summary.parts[name] = "skipped-no-universe"
        summary.rows[name] = 0
        return
    records: list[dict[str, Any]] = []
    failed = 0
    try:
        for symbol in symbols:
            try:
                symbol_rows = fetch(symbol)
            except Exception as exc:
                failed += 1
                log.warning("%s fetch failed for %s: %s", name, symbol, exc)
                if failed > len(symbols) * MAX_FAILURE_RATIO:
                    raise
                continue
            for row in symbol_rows:
                records.append({"day": day, "slot": slot, "captured_at": captured_at, **row})
    except Exception as exc:
        log.exception("%s aborted", name)
        summary.parts[name] = f"error: {exc}"
        summary.rows[name] = 0
        return
    if not records:
        summary.parts[name] = "empty"
        summary.rows[name] = 0
        return
    frame = pl.DataFrame(records, schema=schema)
    rows = snapshots.upsert_date(dataset, day, frame, key)
    summary.parts[name] = "ok" if failed == 0 else f"partial: {failed}종목 실패"
    summary.rows[name] = rows


def _fanout_part(
    summary: PulseSummary,
    name: str,
    snapshots: DatePartitionedStore,
    day: date,
    args: tuple[str, ...],
    fetch: Any,
    dataset: str,
    schema: dict[str, pl.DataType],
    key: tuple[str, ...],
    slot: str,
    captured_at: datetime,
) -> None:
    records: list[dict[str, Any]] = []
    try:
        for arg in args:
            for row in fetch(arg):
                records.append({"day": day, "slot": slot, "captured_at": captured_at, **row})
    except Exception as exc:
        log.exception("%s failed", name)
        summary.parts[name] = f"error: {exc}"
        summary.rows[name] = 0
        return
    if not records:
        summary.parts[name] = "empty"
        summary.rows[name] = 0
        return
    frame = pl.DataFrame(records, schema=schema)
    rows = snapshots.upsert_date(dataset, day, frame, key)
    summary.parts[name] = "ok"
    summary.rows[name] = rows

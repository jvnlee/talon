import logging
from datetime import date
from typing import Any

import polars as pl
from pydantic import BaseModel

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.data.store import STOCK_INFO, DatePartitionedStore
from talon.errors import SourceError
from talon.quant.universe import tradable_symbols
from talon.sources.fdr_daily import fetch_admin_issues

log = logging.getLogger(__name__)


class UniverseBuild(BaseModel):
    symbols: list[str]
    criteria: dict[str, Any]


def latest_stock_info(
    snapshots: DatePartitionedStore,
    day: date,
    *,
    max_stale_days: int,
) -> tuple[date, pl.DataFrame]:
    available = [known for known in snapshots.dates(STOCK_INFO) if known <= day]
    if not available:
        raise SourceError(
            f"{day} 이전 종목기본정보가 없습니다 (talon stock-info backfill 먼저 실행)"
        )
    as_of = available[-1]
    stale_days = (day - as_of).days
    if stale_days > max_stale_days:
        raise SourceError(
            f"종목기본정보가 {as_of} 기준으로 {stale_days}일 낡았습니다 "
            f"(허용 {max_stale_days}일) — reconcile 잡이 도는지 확인하세요"
        )
    frame = snapshots.read_date(STOCK_INFO, as_of)
    if frame is None or frame.is_empty():
        raise SourceError(f"{as_of} 종목기본정보가 비어 있습니다")
    return as_of, frame


def build_universe(
    liquidity: pl.DataFrame,
    *,
    size: int,
    min_value: float,
    info: pl.DataFrame,
    admin: set[str] | None,
    pinned: list[str],
) -> UniverseBuild:
    tradable = tradable_symbols(info)
    frame = liquidity.filter(
        (pl.col("volume") > 0) & pl.col("symbol").is_in(tradable) & (pl.col("value") >= min_value)
    )
    if admin:
        frame = frame.filter(~pl.col("symbol").is_in(sorted(admin)))
    candidates = frame.sort("value", descending=True).get_column("symbol").to_list()
    selected = candidates[:size]
    ordered: dict[str, None] = dict.fromkeys([*pinned, *selected])
    criteria = {
        "size": size,
        "min_value": min_value,
        "admin_excluded": admin is not None,
        "pinned": pinned,
        "candidates": len(candidates),
        "tradable_stocks": len(tradable),
    }
    return UniverseBuild(symbols=list(ordered), criteria=criteria)


def candidate_symbols(liquidity: pl.DataFrame, limit: int) -> list[str]:
    return (
        liquidity.filter(pl.col("volume") > 0)
        .sort("value", descending=True)
        .head(limit)
        .get_column("symbol")
        .to_list()
    )


def rebuild_universe(
    cfg: TalonSettings,
    state: StateDB,
    day: date,
    liquidity: pl.DataFrame,
    *,
    snapshots: DatePartitionedStore,
) -> UniverseBuild:
    as_of, info = latest_stock_info(snapshots, day, max_stale_days=cfg.universe_info_max_stale_days)
    admin = fetch_admin_issues()
    if admin is None:
        log.warning("관리종목 목록 조회 실패 — KOSPI 관리종목이 유니버스에 들어올 수 있습니다")
    build = build_universe(
        liquidity,
        size=cfg.universe_size,
        min_value=cfg.universe_min_trading_value,
        info=info,
        admin=admin,
        pinned=cfg.pinned_symbols,
    )
    build.criteria["info_as_of"] = as_of.isoformat()
    state.save_universe(day, build.symbols, build.criteria)
    log.info(
        "universe rebuilt for %s: %d symbols (종목기본정보 %s 기준)",
        day,
        len(build.symbols),
        as_of,
    )
    return build

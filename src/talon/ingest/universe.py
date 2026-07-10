import logging
from datetime import date
from typing import Any

import polars as pl
from pydantic import BaseModel

from talon.config import TalonSettings
from talon.data.state import StateDB
from talon.errors import SourceError
from talon.models import StockInfo
from talon.sources.fdr_daily import fetch_admin_issues
from talon.sources.toss import TossClient

log = logging.getLogger(__name__)


class UniverseBuild(BaseModel):
    symbols: list[str]
    criteria: dict[str, Any]


def build_universe(
    liquidity: pl.DataFrame,
    *,
    size: int,
    min_value: float,
    admin: set[str] | None,
    stock_info: dict[str, StockInfo] | None,
    pinned: list[str],
) -> UniverseBuild:
    frame = liquidity.filter(pl.col("volume") > 0)
    if admin:
        frame = frame.filter(~pl.col("symbol").is_in(sorted(admin)))
    frame = frame.filter(pl.col("value") >= min_value).sort("value", descending=True)
    candidates = frame.get_column("symbol").to_list()
    if stock_info is not None:
        candidates = [
            symbol
            for symbol in candidates
            if (info := stock_info.get(symbol)) is not None
            and info.security_type == "STOCK"
            and info.is_common_share
            and info.status == "ACTIVE"
        ]
    else:
        candidates = [symbol for symbol in candidates if symbol.endswith("0")]
    selected = candidates[:size]
    ordered: dict[str, None] = dict.fromkeys([*pinned, *selected])
    criteria = {
        "size": size,
        "min_value": min_value,
        "admin_excluded": admin is not None,
        "toss_filtered": stock_info is not None,
        "pinned": pinned,
        "candidates": len(candidates),
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
    toss: TossClient | None = None,
) -> UniverseBuild:
    admin = fetch_admin_issues()
    stock_info: dict[str, StockInfo] | None = None
    if toss is not None:
        candidates = candidate_symbols(liquidity, cfg.universe_size * 2)
        try:
            stock_info = {info.symbol: info for info in toss.stocks(candidates)}
        except SourceError as exc:
            log.warning("toss stock info unavailable, falling back to heuristic: %s", exc)
    build = build_universe(
        liquidity,
        size=cfg.universe_size,
        min_value=cfg.universe_min_trading_value,
        admin=admin,
        stock_info=stock_info,
        pinned=cfg.pinned_symbols,
    )
    state.save_universe(day, build.symbols, build.criteria)
    log.info("universe rebuilt for %s: %d symbols", day, len(build.symbols))
    return build

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl

from talon.backtest.costs import CostModel
from talon.backtest.data import MarketView
from talon.backtest.engine import (
    ClosedTrade,
    EngineConfig,
    Order,
    PortfolioView,
    Strategy,
    run_backtest,
)
from talon.factors.engine import compute_factors

TRADE_KEY = ("entry_day", "symbol", "exit_day")


@dataclass(frozen=True)
class FactorViolation:
    factor: str
    cut: date
    day: date
    symbol: str
    full_value: float | None
    prefix_value: float | None


@dataclass(frozen=True)
class ReplayViolation:
    cut: date
    kind: str
    day: date | None
    detail: str


def pick_cuts(days: Sequence[date], count: int) -> list[date]:
    unique = sorted(set(days))
    interior = unique[1:-1]
    if not interior or count <= 0:
        return []
    if count >= len(interior):
        return interior
    if count == 1:
        return [interior[len(interior) // 2]]
    step = (len(interior) - 1) / (count - 1)
    return sorted({interior[round(index * step)] for index in range(count)})


def _match_mask(full: pl.Series, prefix: pl.Series) -> pl.Series:
    mask = (full == prefix).fill_null(False) | (full.is_null() & prefix.is_null())
    if full.dtype.is_float():
        mask = mask | (full.is_nan().fill_null(False) & prefix.is_nan().fill_null(False))
    return mask


def verify_factors(
    panel: pl.DataFrame,
    factors: dict[str, str],
    cuts: Sequence[date],
    *,
    examples_per_factor: int = 3,
) -> list[FactorViolation]:
    full = compute_factors(panel, factors).sort("day", "symbol")
    violations: list[FactorViolation] = []
    for cut in cuts:
        prefix = compute_factors(panel.filter(pl.col("day") <= cut), factors).sort("day", "symbol")
        full_cut = full.filter(pl.col("day") <= cut)
        for name in factors:
            mask = _match_mask(full_cut.get_column(name), prefix.get_column(name))
            if bool(mask.all()):
                continue
            mismatches = (
                full_cut.select("day", "symbol", pl.col(name).alias("full_value"))
                .with_columns(prefix.get_column(name).alias("prefix_value"))
                .filter(~mask)
                .head(examples_per_factor)
            )
            violations.extend(
                FactorViolation(
                    factor=name,
                    cut=cut,
                    day=row["day"],
                    symbol=row["symbol"],
                    full_value=row["full_value"],
                    prefix_value=row["prefix_value"],
                )
                for row in mismatches.iter_rows(named=True)
            )
    return violations


class _Recorder:
    def __init__(self, inner: Strategy) -> None:
        self.inner = inner
        self.decisions: dict[date, list[Order]] = {}

    def decide(self, view: MarketView, portfolio: PortfolioView) -> list[Order]:
        orders = list(self.inner.decide(view, portfolio))
        self.decisions[view.day] = orders
        return orders

    def on_close(self, trade: ClosedTrade) -> None:
        handler = getattr(self.inner, "on_close", None)
        if handler is not None:
            handler(trade)


def _replay_universe(panel: pl.DataFrame, cut: date) -> pl.DataFrame:
    coverage = panel.group_by("symbol").agg(
        (pl.col("day") == cut).any().alias("at_cut"),
        pl.col("day").max().alias("last_day"),
    )
    symbols = coverage.filter(pl.col("at_cut") & (pl.col("last_day") > cut))["symbol"]
    return panel.filter(pl.col("symbol").is_in(symbols.to_list()))


def _pad_after_cut(truncated: pl.DataFrame, cut: date) -> pl.DataFrame:
    dummy = truncated.filter(pl.col("day") == cut).with_columns(
        pl.lit(cut + timedelta(days=1)).alias("day"),
        pl.col("close").alias("open"),
        pl.col("close").alias("high"),
        pl.col("close").alias("low"),
        pl.col("close").alias("prev_close"),
    )
    return pl.concat([truncated, dummy.select(truncated.columns)]).sort("day", "symbol")


EQUITY_COLUMNS = ("cash", "position_value", "equity", "positions")


def _first_equity_divergence(full: pl.DataFrame, prefix: pl.DataFrame) -> tuple[date, str]:
    joined = full.join(prefix, on="day", suffix="_prefix")
    diff = joined.filter(
        pl.any_horizontal(
            [pl.col(column) != pl.col(f"{column}_prefix") for column in EQUITY_COLUMNS]
        )
    )
    row = diff.row(0, named=True)
    return row["day"], f"equity {row['equity']} vs {row['equity_prefix']}"


def verify_replay(
    panel: pl.DataFrame,
    build_strategy: Callable[[pl.DataFrame], Strategy],
    cuts: Sequence[date],
    *,
    config: EngineConfig | None = None,
    costs: CostModel | None = None,
) -> list[ReplayViolation]:
    violations: list[ReplayViolation] = []
    for cut in cuts:
        universe = _replay_universe(panel, cut)
        if universe.is_empty() or universe["day"].n_unique() < 2:
            continue
        truncated = _pad_after_cut(universe.filter(pl.col("day") <= cut), cut)
        full_recorder = _Recorder(build_strategy(universe))
        full_result = run_backtest(universe, full_recorder, config=config, costs=costs)
        prefix_recorder = _Recorder(build_strategy(truncated))
        prefix_result = run_backtest(truncated, prefix_recorder, config=config, costs=costs)

        for day in sorted(prefix_recorder.decisions):
            if day > cut:
                continue
            prefix_orders = prefix_recorder.decisions[day]
            full_orders = full_recorder.decisions.get(day, [])
            if prefix_orders != full_orders:
                violations.append(
                    ReplayViolation(
                        cut=cut,
                        kind="decision",
                        day=day,
                        detail=f"{len(full_orders)}개 vs {len(prefix_orders)}개 주문 불일치",
                    )
                )

        full_equity = full_result.equity.filter(pl.col("day") <= cut)
        prefix_equity = prefix_result.equity.filter(pl.col("day") <= cut)
        if not full_equity.equals(prefix_equity):
            day, detail = _first_equity_divergence(full_equity, prefix_equity)
            violations.append(ReplayViolation(cut=cut, kind="equity", day=day, detail=detail))

        full_trades = full_result.trades.filter(pl.col("exit_day") <= cut).sort(TRADE_KEY)
        prefix_trades = prefix_result.trades.filter(pl.col("exit_day") <= cut).sort(TRADE_KEY)
        if not full_trades.equals(prefix_trades):
            violations.append(
                ReplayViolation(
                    cut=cut,
                    kind="trade",
                    day=None,
                    detail=f"체결 {full_trades.height}건 vs {prefix_trades.height}건 불일치",
                )
            )
    return violations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

from talon.backtest.costs import CostModel, KrCostModel
from talon.backtest.data import MarketView
from talon.backtest.engine import EngineConfig, Order, PortfolioView, run_backtest
from talon.errors import TalonError

START_DAY = date(2025, 10, 1)
ENTRY_CHANCE = 0.2
EXIT_CHANCE = 0.25
EQUITY_TOLERANCE = 0.01
PNL_TOLERANCE = 0.01


@dataclass(frozen=True)
class Mismatch:
    scenario: int
    symbol: str
    kind: str
    detail: str


@dataclass(frozen=True)
class CrosscheckReport:
    scenarios: int
    symbols: int
    trades: int
    mismatches: list[Mismatch]

    @property
    def ok(self) -> bool:
        return not self.mismatches


@dataclass(frozen=True)
class _Leg:
    signal_index: int
    shares: int


@dataclass(frozen=True)
class _SymbolPlan:
    symbol: str
    entries: list[_Leg]
    exits: list[_Leg]


class _Script:
    def __init__(self, orders: dict[date, list[Order]]) -> None:
        self.orders = orders

    def decide(self, view: MarketView, portfolio: PortfolioView) -> list[Order]:
        return self.orders.get(view.day, [])


def _require_vectorbt() -> Any:
    try:
        import vectorbt
    except ImportError as exc:
        raise TalonError(
            "vectorbt가 설치되어 있지 않습니다 (uv sync --group dev 후 재시도)"
        ) from exc
    return vectorbt


def _random_walk_panel(rng: np.random.Generator, symbols: list[str], days: int) -> pl.DataFrame:
    rows = []
    for offset, symbol in enumerate(symbols):
        close = 10_000.0 * (1 + offset)
        for i in range(days):
            gap = float(np.clip(rng.normal(0.0, 0.01), -0.05, 0.05))
            move = float(np.clip(rng.normal(0.0, 0.015), -0.06, 0.06))
            open_ = close * (1 + gap)
            new_close = open_ * (1 + move)
            high = max(open_, new_close) * (1 + abs(float(rng.normal(0.0, 0.004))))
            low = min(open_, new_close) * (1 - abs(float(rng.normal(0.0, 0.004))))
            rows.append(
                {
                    "day": START_DAY + timedelta(days=i),
                    "symbol": symbol,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": new_close,
                    "volume": 1e12,
                    "value": new_close * 1e12,
                    "raw_close": new_close,
                    "factor": 1.0,
                }
            )
            close = new_close
    return (
        pl.DataFrame(rows)
        .sort("symbol", "day")
        .with_columns(pl.col("close").shift(1).over("symbol").alias("prev_close"))
        .sort("day", "symbol")
    )


def _random_plan(rng: np.random.Generator, symbol: str, days: int) -> _SymbolPlan:
    entries: list[_Leg] = []
    exits: list[_Leg] = []
    holding = False
    shares = 0
    for i in range(days - 1):
        if not holding:
            if rng.random() < ENTRY_CHANCE:
                shares = int(rng.integers(1, 200))
                entries.append(_Leg(signal_index=i, shares=shares))
                holding = True
        elif rng.random() < EXIT_CHANCE:
            exits.append(_Leg(signal_index=i, shares=shares))
            holding = False
    return _SymbolPlan(symbol=symbol, entries=entries, exits=exits)


def _talon_orders(
    panel: pl.DataFrame, plans: list[_SymbolPlan], slippage: float
) -> dict[date, list[Order]]:
    orders: dict[date, list[Order]] = {}

    def add(day: date, order: Order) -> None:
        orders.setdefault(day, []).append(order)

    for plan in plans:
        days = panel.filter(pl.col("symbol") == plan.symbol).sort("day")["day"].to_list()
        opens = panel.filter(pl.col("symbol") == plan.symbol).sort("day")["open"].to_list()
        for leg in plan.entries:
            fill_price = opens[leg.signal_index + 1] * (1 + slippage)
            budget = (leg.shares + 0.5) * fill_price
            add(days[leg.signal_index], Order("buy", plan.symbol, budget=budget))
        for leg in plan.exits:
            add(days[leg.signal_index], Order("sell", plan.symbol))
    return orders


def _vbt_profit_and_trades(
    vbt: Any,
    panel: pl.DataFrame,
    plan: _SymbolPlan,
    *,
    slippage: float,
    costs: CostModel,
    init_cash: float,
) -> tuple[Any, Any]:
    import pandas as pd

    frame = panel.filter(pl.col("symbol") == plan.symbol).sort("day")
    days = frame["day"].to_list()
    index = pd.DatetimeIndex(days)
    open_ = pd.Series(frame["open"].to_list(), index=index)
    close = pd.Series(frame["close"].to_list(), index=index)
    length = len(days)
    entries = np.zeros(length, dtype=bool)
    exits = np.zeros(length, dtype=bool)
    sizes = np.full(length, np.nan)
    fees = np.zeros(length)
    for leg in plan.entries:
        fill = leg.signal_index + 1
        entries[fill] = True
        sizes[fill] = leg.shares
        fees[fill] = costs.buy_fee(1.0, days[fill])
    for leg in plan.exits:
        fill = leg.signal_index + 1
        exits[fill] = True
        sizes[fill] = leg.shares
        fees[fill] = costs.sell_fee(1.0, days[fill])
    portfolio = vbt.Portfolio.from_signals(
        close=close,
        entries=pd.Series(entries, index=index),
        exits=pd.Series(exits, index=index),
        price=open_,
        size=pd.Series(sizes, index=index),
        size_type="amount",
        fees=pd.Series(fees, index=index),
        slippage=slippage,
        init_cash=init_cash,
        direction="longonly",
        freq="1D",
    )
    profit = portfolio.value() - init_cash
    return profit, portfolio.trades.records


def _compare_trades(
    scenario: int,
    plan: _SymbolPlan,
    talon_trades: pl.DataFrame,
    vbt_records: Any,
    days: list[date],
) -> list[Mismatch]:
    from vectorbt.portfolio.enums import TradeStatus

    mismatches: list[Mismatch] = []
    closed = vbt_records[vbt_records["status"] == TradeStatus.Closed].sort_values("entry_idx")
    ours = talon_trades.filter(pl.col("symbol") == plan.symbol).sort("entry_day")
    if ours.height != len(closed):
        return [
            Mismatch(
                scenario,
                plan.symbol,
                "trade-count",
                f"talon {ours.height}건 vs vectorbt {len(closed)}건",
            )
        ]
    for row, (_, record) in zip(ours.iter_rows(named=True), closed.iterrows(), strict=True):
        entry_day = days[int(record["entry_idx"])]
        exit_day = days[int(record["exit_idx"])]
        expected_pnl = float(record["pnl"])
        if row["entry_day"] != entry_day or row["exit_day"] != exit_day:
            mismatches.append(
                Mismatch(
                    scenario,
                    plan.symbol,
                    "trade-days",
                    f"talon {row['entry_day']}→{row['exit_day']} vs "
                    f"vectorbt {entry_day}→{exit_day}",
                )
            )
            continue
        if abs(row["pnl"] - expected_pnl) > PNL_TOLERANCE:
            mismatches.append(
                Mismatch(
                    scenario,
                    plan.symbol,
                    "trade-pnl",
                    f"{entry_day}→{exit_day} talon {row['pnl']:.4f} vs vectorbt {expected_pnl:.4f}",
                )
            )
    return mismatches


def run_crosscheck(
    *,
    seed: int = 42,
    scenarios: int = 10,
    symbols: int = 3,
    days: int = 120,
    slippage: float = 0.001,
    costs: CostModel | None = None,
    init_cash: float = 1_000_000_000.0,
    vbt_slippage_override: float | None = None,
) -> CrosscheckReport:
    vbt = _require_vectorbt()
    cost_model = costs if costs is not None else KrCostModel()
    vbt_slippage = vbt_slippage_override if vbt_slippage_override is not None else slippage
    names = [f"S{i:02d}" for i in range(symbols)]
    mismatches: list[Mismatch] = []
    total_trades = 0
    for scenario in range(scenarios):
        rng = np.random.default_rng(seed + scenario)
        panel = _random_walk_panel(rng, names, days)
        plans = [_random_plan(rng, name, days) for name in names]
        orders = _talon_orders(panel, plans, slippage)
        result = run_backtest(
            panel,
            _Script(orders),
            config=EngineConfig(
                initial_cash=init_cash * len(names),
                slippage_pct=slippage,
            ),
            costs=cost_model,
        )
        total_trades += result.trades.height
        talon_profit = {
            row["day"]: row["equity"] - init_cash * len(names)
            for row in result.equity.iter_rows(named=True)
        }
        combined: dict[date, float] = dict.fromkeys(talon_profit, 0.0)
        for plan in plans:
            profit, records = _vbt_profit_and_trades(
                vbt,
                panel,
                plan,
                slippage=vbt_slippage,
                costs=cost_model,
                init_cash=init_cash,
            )
            symbol_days = panel.filter(pl.col("symbol") == plan.symbol).sort("day")["day"].to_list()
            mismatches.extend(_compare_trades(scenario, plan, result.trades, records, symbol_days))
            for stamp, value in profit.items():
                combined[stamp.date()] += float(value)
        for day, expected in combined.items():
            actual = talon_profit[day]
            if abs(actual - expected) > EQUITY_TOLERANCE:
                mismatches.append(
                    Mismatch(
                        scenario,
                        "*",
                        "equity",
                        f"{day} talon {actual:.4f} vs vectorbt {expected:.4f}",
                    )
                )
                break
    return CrosscheckReport(
        scenarios=scenarios,
        symbols=symbols,
        trades=total_trades,
        mismatches=mismatches,
    )

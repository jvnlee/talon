from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

import polars as pl

from talon.backtest.costs import CostModel, KrCostModel
from talon.backtest.data import MarketView
from talon.backtest.metrics import BacktestStats, summarize

OrderKind = Literal["buy", "sell", "update"]

TRADES_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8(),
    "entry_day": pl.Date(),
    "exit_day": pl.Date(),
    "holding_days": pl.Int64(),
    "entry_price": pl.Float64(),
    "exit_price": pl.Float64(),
    "entry_notional": pl.Float64(),
    "exit_notional": pl.Float64(),
    "fees": pl.Float64(),
    "pnl": pl.Float64(),
    "return_pct": pl.Float64(),
    "reason": pl.Utf8(),
}

EQUITY_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "cash": pl.Float64(),
    "position_value": pl.Float64(),
    "equity": pl.Float64(),
    "positions": pl.Int64(),
}

REJECTION_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "symbol": pl.Utf8(),
    "kind": pl.Utf8(),
    "reason": pl.Utf8(),
}


@dataclass(frozen=True)
class Order:
    kind: OrderKind
    symbol: str
    budget: float | None = None
    stop: float | None = None
    target: float | None = None


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    entry_day: date
    exit_day: date
    entry_notional: float
    pnl: float
    return_pct: float
    reason: str


@dataclass(frozen=True)
class PositionView:
    symbol: str
    shares: float
    entry_day: date
    entry_price: float
    stop: float | None
    target: float | None
    value: float


@dataclass(frozen=True)
class PortfolioView:
    day: date
    cash: float
    equity: float
    positions: dict[str, PositionView]


@dataclass(frozen=True)
class EngineConfig:
    initial_cash: float = 10_000_000.0
    slippage_pct: float = 0.001
    limit_move_pct: float = 0.295
    volume_cap_pct: float = 0.10


class Strategy(Protocol):
    def decide(self, view: MarketView, portfolio: PortfolioView) -> list[Order]: ...


@dataclass
class BacktestResult:
    equity: pl.DataFrame
    trades: pl.DataFrame
    rejections: pl.DataFrame
    stats: BacktestStats


@dataclass
class _Position:
    symbol: str
    shares_adj: float
    entry_day: date
    entry_price: float
    entry_notional: float
    entry_fees: float
    stop: float | None
    target: float | None
    last_mark: float


def _floor_shares(value: float) -> int:
    return max(0, int(value + 1e-9))


class _Run:
    def __init__(
        self,
        panel: pl.DataFrame,
        strategy: Strategy,
        config: EngineConfig,
        costs: CostModel,
    ) -> None:
        self.panel = panel
        self.strategy = strategy
        self.config = config
        self.costs = costs
        self.cash = config.initial_cash
        self.positions: dict[str, _Position] = {}
        self.pending: list[Order] = []
        self.trade_rows: list[dict[str, Any]] = []
        self.equity_rows: list[dict[str, Any]] = []
        self.rejection_rows: list[dict[str, Any]] = []

    def execute(self) -> BacktestResult:
        days: list[date] = self.panel["day"].unique().sort().to_list()
        frames = {
            key[0]: frame for key, frame in self.panel.partition_by("day", as_dict=True).items()
        }
        last_day: dict[str, date] = {
            symbol: day
            for symbol, day in self.panel.group_by("symbol").agg(pl.col("day").max()).iter_rows()
        }
        global_end = days[-1]
        for day in days:
            frame = frames[day]
            needed = set(self.positions) | {order.symbol for order in self.pending}
            bars = self._day_bars(frame, needed)
            self._apply_updates(day)
            self._open_sells(day, bars)
            self._open_buys(day, bars)
            self._intrabar_exits(day, bars)
            self._delist_closes(day, bars, last_day, global_end)
            self._mark(day, bars)
            if day != global_end:
                view = MarketView(self.panel, day)
                self.pending = list(self.strategy.decide(view, self._portfolio_view(day)))
            else:
                self.pending = []
        equity = pl.DataFrame(self.equity_rows, schema=EQUITY_SCHEMA)
        trades = pl.DataFrame(self.trade_rows, schema=TRADES_SCHEMA)
        rejections = pl.DataFrame(self.rejection_rows, schema=REJECTION_SCHEMA)
        stats = summarize(
            equity,
            trades,
            initial_cash=self.config.initial_cash,
            open_positions=len(self.positions),
        )
        return BacktestResult(equity=equity, trades=trades, rejections=rejections, stats=stats)

    def _day_bars(self, frame: pl.DataFrame, needed: set[str]) -> dict[str, dict[str, Any]]:
        if not needed:
            return {}
        rows = frame.filter(pl.col("symbol").is_in(sorted(needed))).to_dicts()
        return {row["symbol"]: row for row in rows}

    def _reject(self, day: date, order: Order, reason: str) -> None:
        self.rejection_rows.append(
            {"day": day, "symbol": order.symbol, "kind": order.kind, "reason": reason}
        )

    def _limit_up(self, bar: dict[str, Any]) -> bool:
        prev = bar["prev_close"]
        return prev is not None and bar["open"] >= prev * (1 + self.config.limit_move_pct)

    def _limit_down(self, bar: dict[str, Any]) -> bool:
        prev = bar["prev_close"]
        return prev is not None and bar["open"] <= prev * (1 - self.config.limit_move_pct)

    def _apply_updates(self, day: date) -> None:
        for order in self.pending:
            if order.kind != "update":
                continue
            position = self.positions.get(order.symbol)
            if position is None:
                self._reject(day, order, "not-held")
                continue
            if order.stop is not None:
                position.stop = order.stop
            if order.target is not None:
                position.target = order.target

    def _open_sells(self, day: date, bars: dict[str, dict[str, Any]]) -> None:
        for order in self.pending:
            if order.kind != "sell":
                continue
            position = self.positions.get(order.symbol)
            if position is None:
                self._reject(day, order, "not-held")
                continue
            bar = bars.get(order.symbol)
            if bar is None:
                self._reject(day, order, "no-bar")
                continue
            if self._limit_down(bar):
                self._reject(day, order, "limit-down")
                continue
            self._close(position, day, bar["open"] * (1 - self.config.slippage_pct), "strategy")
        for symbol in sorted(self.positions):
            position = self.positions[symbol]
            bar = bars.get(symbol)
            if bar is None or self._limit_down(bar):
                continue
            exec_price = bar["open"] * (1 - self.config.slippage_pct)
            if position.stop is not None and bar["open"] <= position.stop:
                self._close(position, day, exec_price, "stop")
            elif position.target is not None and bar["open"] >= position.target:
                self._close(position, day, exec_price, "target")

    def _open_buys(self, day: date, bars: dict[str, dict[str, Any]]) -> None:
        for order in self.pending:
            if order.kind != "buy":
                continue
            if order.budget is None or order.budget <= 0:
                self._reject(day, order, "invalid")
                continue
            bar = bars.get(order.symbol)
            if bar is None:
                self._reject(day, order, "no-bar")
                continue
            if self._limit_up(bar):
                self._reject(day, order, "limit-up")
                continue
            exec_adj = bar["open"] * (1 + self.config.slippage_pct)
            factor = bar["factor"]
            exec_raw = exec_adj / factor
            budget_shares = _floor_shares(order.budget / exec_raw)
            cap_shares = _floor_shares(bar["volume"] * self.config.volume_cap_pct * factor)
            shares = min(budget_shares, cap_shares, _floor_shares(self.cash / exec_raw))
            while shares > 0:
                notional = shares * exec_raw
                if notional + self.costs.buy_fee(notional, day) <= self.cash + 1e-6:
                    break
                shares -= 1
            if shares <= 0:
                if cap_shares == 0:
                    reason = "volume"
                elif budget_shares == 0:
                    reason = "budget"
                else:
                    reason = "no-cash"
                self._reject(day, order, reason)
                continue
            notional = shares * exec_raw
            fee = self.costs.buy_fee(notional, day)
            self.cash -= notional + fee
            self._enter(order, day, shares / factor, exec_adj, notional, fee)

    def _enter(
        self,
        order: Order,
        day: date,
        shares_adj: float,
        exec_adj: float,
        notional: float,
        fee: float,
    ) -> None:
        position = self.positions.get(order.symbol)
        if position is None:
            self.positions[order.symbol] = _Position(
                symbol=order.symbol,
                shares_adj=shares_adj,
                entry_day=day,
                entry_price=exec_adj,
                entry_notional=notional,
                entry_fees=fee,
                stop=order.stop,
                target=order.target,
                last_mark=exec_adj,
            )
            return
        total_shares = position.shares_adj + shares_adj
        position.entry_price = (
            position.entry_price * position.shares_adj + exec_adj * shares_adj
        ) / total_shares
        position.shares_adj = total_shares
        position.entry_notional += notional
        position.entry_fees += fee
        if order.stop is not None:
            position.stop = order.stop
        if order.target is not None:
            position.target = order.target

    def _intrabar_exits(self, day: date, bars: dict[str, dict[str, Any]]) -> None:
        for symbol in sorted(self.positions):
            position = self.positions[symbol]
            bar = bars.get(symbol)
            if bar is None or self._limit_down(bar):
                continue
            if position.stop is not None and bar["low"] <= position.stop:
                self._close(position, day, position.stop * (1 - self.config.slippage_pct), "stop")
            elif position.target is not None and bar["high"] >= position.target:
                self._close(position, day, position.target, "target")

    def _delist_closes(
        self,
        day: date,
        bars: dict[str, dict[str, Any]],
        last_day: dict[str, date],
        global_end: date,
    ) -> None:
        if day == global_end:
            return
        for symbol in sorted(self.positions):
            if last_day[symbol] != day:
                continue
            position = self.positions[symbol]
            bar = bars[symbol]
            self._close(position, day, bar["close"] * (1 - self.config.slippage_pct), "delist")

    def _close(self, position: _Position, day: date, price_adj: float, reason: str) -> None:
        notional = position.shares_adj * price_adj
        fee = self.costs.sell_fee(notional, day)
        self.cash += notional - fee
        fees = position.entry_fees + fee
        pnl = notional - position.entry_notional - fees
        self.trade_rows.append(
            {
                "symbol": position.symbol,
                "entry_day": position.entry_day,
                "exit_day": day,
                "holding_days": (day - position.entry_day).days,
                "entry_price": position.entry_price,
                "exit_price": price_adj,
                "entry_notional": position.entry_notional,
                "exit_notional": notional,
                "fees": fees,
                "pnl": pnl,
                "return_pct": pnl / position.entry_notional,
                "reason": reason,
            }
        )
        del self.positions[position.symbol]
        handler = getattr(self.strategy, "on_close", None)
        if handler is not None:
            handler(
                ClosedTrade(
                    symbol=position.symbol,
                    entry_day=position.entry_day,
                    exit_day=day,
                    entry_notional=position.entry_notional,
                    pnl=pnl,
                    return_pct=pnl / position.entry_notional,
                    reason=reason,
                )
            )

    def _mark(self, day: date, bars: dict[str, dict[str, Any]]) -> None:
        value = 0.0
        for symbol in sorted(self.positions):
            position = self.positions[symbol]
            bar = bars.get(symbol)
            if bar is not None:
                position.last_mark = bar["close"]
            value += position.shares_adj * position.last_mark
        self.equity_rows.append(
            {
                "day": day,
                "cash": self.cash,
                "position_value": value,
                "equity": self.cash + value,
                "positions": len(self.positions),
            }
        )

    def _portfolio_view(self, day: date) -> PortfolioView:
        views = {
            symbol: PositionView(
                symbol=symbol,
                shares=position.shares_adj,
                entry_day=position.entry_day,
                entry_price=position.entry_price,
                stop=position.stop,
                target=position.target,
                value=position.shares_adj * position.last_mark,
            )
            for symbol, position in sorted(self.positions.items())
        }
        equity = self.equity_rows[-1]["equity"] if self.equity_rows else self.cash
        return PortfolioView(day=day, cash=self.cash, equity=equity, positions=views)


def run_backtest(
    panel: pl.DataFrame,
    strategy: Strategy,
    *,
    config: EngineConfig | None = None,
    costs: CostModel | None = None,
) -> BacktestResult:
    if panel.is_empty():
        raise ValueError("빈 패널로는 백테스트를 실행할 수 없습니다")
    return _Run(
        panel,
        strategy,
        config if config is not None else EngineConfig(),
        costs if costs is not None else KrCostModel(),
    ).execute()

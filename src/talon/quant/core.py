from typing import Protocol

import polars as pl

from talon.backtest.data import MarketView
from talon.backtest.engine import ClosedTrade, Order, PortfolioView
from talon.factors.engine import compute_factors
from talon.quant.regime import BreadthRegimeFilter, Regime
from talon.quant.risk import Intervention, RiskGate
from talon.quant.signals import Signal, StrategySpec
from talon.quant.strategies import default_strategies
from talon.quant.universe import LiquidityUniverse


class RegimeAssessor(Protocol):
    def columns(self) -> dict[str, str]: ...

    def assess(self, day_frame: pl.DataFrame) -> Regime: ...


CLOSED_TRADES_SCHEMA: dict[str, pl.DataType] = {
    "strategy": pl.Utf8(),
    "symbol": pl.Utf8(),
    "entry_day": pl.Date(),
    "exit_day": pl.Date(),
    "entry_notional": pl.Float64(),
    "pnl": pl.Float64(),
    "return_pct": pl.Float64(),
    "reason": pl.Utf8(),
}


def closed_trades_frame(closed: list[tuple[str | None, ClosedTrade]]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "strategy": strategy,
                "symbol": trade.symbol,
                "entry_day": trade.entry_day,
                "exit_day": trade.exit_day,
                "entry_notional": trade.entry_notional,
                "pnl": trade.pnl,
                "return_pct": trade.return_pct,
                "reason": trade.reason,
            }
            for strategy, trade in closed
        ],
        schema=CLOSED_TRADES_SCHEMA,
    )


class QuantCore:
    def __init__(
        self,
        panel: pl.DataFrame,
        strategies: list[StrategySpec] | None = None,
        regime_filter: RegimeAssessor | None = None,
        gate: RiskGate | None = None,
        universe: LiquidityUniverse | None = None,
    ) -> None:
        self.strategies = strategies if strategies is not None else default_strategies()
        names = [spec.name for spec in self.strategies]
        if len(set(names)) != len(names):
            raise ValueError(f"전략 이름이 중복됩니다: {names}")
        self.regime_filter: RegimeAssessor = (
            regime_filter if regime_filter is not None else BreadthRegimeFilter()
        )
        self.gate = gate if gate is not None else RiskGate()
        self.universe = universe
        columns: dict[str, str] = {}
        for spec in self.strategies:
            columns.update(spec.columns())
        columns.update(self.regime_filter.columns())
        augmented = compute_factors(panel, columns)
        self._frames = {
            key[0]: frame for key, frame in augmented.partition_by("day", as_dict=True).items()
        }
        self._specs = {spec.name: spec for spec in self.strategies}
        self._owner: dict[str, str] = {}
        self._pending: set[str] = set()
        self.closed_trades: list[tuple[str | None, ClosedTrade]] = []

    @property
    def interventions(self) -> list[Intervention]:
        return self.gate.interventions

    def owner_of(self, symbol: str) -> str | None:
        return self._owner.get(symbol)

    def on_close(self, trade: ClosedTrade) -> None:
        strategy = self._owner.pop(trade.symbol, None)
        self.closed_trades.append((strategy, trade))
        self.gate.record_close(trade, strategy)

    def trades_by(self, strategy: str) -> int:
        return sum(1 for owner, _ in self.closed_trades if owner == strategy)

    def decide(self, view: MarketView, portfolio: PortfolioView) -> list[Order]:
        frame = self._frames.get(view.day)
        if frame is None:
            return []
        for symbol in self._pending:
            if symbol not in portfolio.positions:
                self._owner.pop(symbol, None)
        self._pending = set()

        tradable = frame if self.universe is None else self.universe.filter(frame)
        regime = self.regime_filter.assess(tradable)
        sells: list[Order] = []
        for symbol, position in sorted(portfolio.positions.items()):
            spec = self._specs.get(self._owner.get(symbol, ""))
            if spec is None:
                continue
            held_days = (view.day - position.entry_day).days
            if held_days >= spec.max_hold_days or spec.wants_exit(frame, symbol):
                sells.append(Order("sell", symbol))

        signals: list[Signal] = []
        for spec in self.strategies:
            signals.extend(spec.candidates(tradable))
        result = self.gate.apply(view.day, portfolio, signals, regime)
        for signal in result.approved:
            self._owner[signal.symbol] = signal.strategy
            self._pending.add(signal.symbol)

        selling = {order.symbol for order in sells}
        return sells + [
            order
            for order in result.orders
            if not (order.kind == "sell" and order.symbol in selling)
        ]

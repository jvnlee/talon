import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import polars as pl

from talon.backtest.engine import ClosedTrade, Order, PortfolioView, PositionView
from talon.quant.regime import Regime
from talon.quant.signals import Signal

INTERVENTION_SCHEMA: dict[str, pl.DataType] = {
    "day": pl.Date(),
    "action": pl.Utf8(),
    "reason": pl.Utf8(),
    "strategy": pl.Utf8(),
    "symbol": pl.Utf8(),
    "detail": pl.Utf8(),
}


def interventions_frame(interventions: list["Intervention"]) -> pl.DataFrame:
    return pl.DataFrame(
        [asdict(intervention) for intervention in interventions],
        schema=INTERVENTION_SCHEMA,
    )


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.007
    max_weight_pct: float = 0.20
    max_positions: int = 5
    daily_loss_halt_pct: float = 0.02
    weekly_loss_halt_pct: float = 0.05
    cooldown_after_losses: int = 3
    cooldown_days: int = 5
    drawdown_reduce_pct: float = 0.10
    drawdown_liquidate_pct: float = 0.15
    drawdown_reduce_target: float = 0.5
    reduced_risk_scale: float = 0.5


@dataclass(frozen=True)
class Intervention:
    day: date
    action: str
    reason: str
    strategy: str | None = None
    symbol: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class GateResult:
    orders: list[Order] = field(default_factory=list)
    approved: list[Signal] = field(default_factory=list)


class RiskGate:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config if config is not None else RiskConfig()
        self.interventions: list[Intervention] = []
        self.halted = False
        self._reduced = False
        self._peak_equity: float | None = None
        self._prev_equity: float | None = None
        self._daily_pnl: dict[date, float] = {}
        self._weekly_pnl: dict[tuple[int, int], float] = {}
        self._week: tuple[int, int] | None = None
        self._week_anchor: float | None = None
        self._tripped_days: set[date] = set()
        self._tripped_weeks: set[tuple[int, int]] = set()
        self._streaks: dict[str, int] = {}
        self._cooldown_until: dict[str, date] = {}

    def record_close(self, trade: ClosedTrade, strategy: str | None) -> None:
        week = trade.exit_day.isocalendar()[:2]
        self._daily_pnl[trade.exit_day] = self._daily_pnl.get(trade.exit_day, 0.0) + trade.pnl
        self._weekly_pnl[week] = self._weekly_pnl.get(week, 0.0) + trade.pnl
        if strategy is None:
            return
        if trade.pnl >= 0:
            self._streaks[strategy] = 0
            return
        streak = self._streaks.get(strategy, 0) + 1
        self._streaks[strategy] = streak
        if streak >= self.config.cooldown_after_losses:
            until = trade.exit_day + timedelta(days=self.config.cooldown_days)
            self._cooldown_until[strategy] = until
            self._streaks[strategy] = 0
            self._log(
                trade.exit_day,
                "cooldown",
                "consecutive-losses",
                strategy=strategy,
                detail=f"{until.isoformat()}까지 진입 중단",
            )

    def apply(
        self,
        day: date,
        portfolio: PortfolioView,
        signals: list[Signal],
        regime: Regime,
    ) -> GateResult:
        try:
            return self._apply(day, portfolio, signals, regime)
        finally:
            self._prev_equity = portfolio.equity

    def _apply(
        self,
        day: date,
        portfolio: PortfolioView,
        signals: list[Signal],
        regime: Regime,
    ) -> GateResult:
        config = self.config
        equity = portfolio.equity
        if self.halted:
            self._drop_all(day, signals, "halted")
            return GateResult(orders=[Order("sell", s) for s in sorted(portfolio.positions)])

        peak = max(self._peak_equity if self._peak_equity is not None else equity, equity)
        self._peak_equity = peak
        drawdown = equity / peak - 1.0 if peak > 0 else 0.0
        if drawdown <= -config.drawdown_liquidate_pct:
            self.halted = True
            self._log(day, "liquidate", "drawdown-liquidate", detail=f"낙폭 {drawdown:.1%}")
            self._drop_all(day, signals, "halted")
            return GateResult(orders=[Order("sell", s) for s in sorted(portfolio.positions)])

        orders: list[Order] = []
        if drawdown <= -config.drawdown_reduce_pct:
            if not self._reduced:
                self._reduced = True
                self._log(day, "reduce", "drawdown-reduce", detail=f"낙폭 {drawdown:.1%}")
                orders.extend(self._reduction_sells(portfolio))
        else:
            self._reduced = False
        risk_scale = config.reduced_risk_scale if self._reduced else 1.0

        week = day.isocalendar()[:2]
        if week != self._week:
            self._week = week
            self._week_anchor = self._prev_equity
        daily_anchor = self._prev_equity if self._prev_equity is not None else equity
        week_anchor = self._week_anchor if self._week_anchor is not None else equity
        day_pnl = self._daily_pnl.get(day, 0.0)
        week_pnl = self._weekly_pnl.get(week, 0.0)
        daily_hit = daily_anchor > 0 and day_pnl <= -daily_anchor * config.daily_loss_halt_pct
        if daily_hit and day not in self._tripped_days:
            self._tripped_days.add(day)
            self._log(day, "breaker", "daily-loss", detail=f"실현 {day_pnl:,.0f}")
        weekly_hit = week_anchor > 0 and week_pnl <= -week_anchor * config.weekly_loss_halt_pct
        if weekly_hit and week not in self._tripped_weeks:
            self._tripped_weeks.add(week)
            self._log(day, "breaker", "weekly-loss", detail=f"실현 {week_pnl:,.0f}")
        daily_tripped = day in self._tripped_days
        weekly_tripped = week in self._tripped_weeks

        candidates: list[tuple[Signal, float]] = []
        for signal in signals:
            invalid = self._invalid_levels(signal)
            if invalid is not None:
                self._log_signal(day, signal, "reject", invalid)
                continue
            if signal.symbol in portfolio.positions:
                self._log_signal(day, signal, "block", "already-held")
                continue
            if regime.exposure <= 0:
                self._log_signal(day, signal, "block", "regime-bear")
                continue
            weight = regime.weight(signal.strategy)
            if weight <= 0:
                self._log_signal(day, signal, "block", "regime-weight")
                continue
            cooldown = self._cooldown_until.get(signal.strategy)
            if cooldown is not None and day <= cooldown:
                self._log_signal(day, signal, "block", "cooldown")
                continue
            if daily_tripped:
                self._log_signal(day, signal, "block", "daily-breaker")
                continue
            if weekly_tripped:
                self._log_signal(day, signal, "block", "weekly-breaker")
                continue
            score = signal.score * weight
            candidates.append((signal, score if math.isfinite(score) else 0.0))

        finalists: list[tuple[Signal, float]] = []
        groups: dict[str, list[tuple[Signal, float]]] = {}
        for signal, score in candidates:
            groups.setdefault(signal.symbol, []).append((signal, score))
        for symbol in sorted(groups):
            ranked = sorted(groups[symbol], key=lambda item: (-item[1], item[0].strategy))
            finalists.append(ranked[0])
            for signal, _ in ranked[1:]:
                self._log_signal(day, signal, "block", "duplicate")

        finalists.sort(key=lambda item: (-item[1], item[0].symbol))
        approved: list[Signal] = []
        slots = config.max_positions - len(portfolio.positions)
        exposure_cap = equity * regime.exposure * risk_scale
        invested = sum(position.value for position in portfolio.positions.values())
        for signal, _ in finalists:
            if slots <= 0:
                self._log_signal(day, signal, "block", "max-positions")
                continue
            weight = regime.weight(signal.strategy)
            risk_amount = equity * config.risk_per_trade_pct * weight * risk_scale
            per_share_risk = signal.ref_price - float(signal.stop or 0.0)
            shares = int(risk_amount / per_share_risk)
            if shares < 1:
                self._log_signal(day, signal, "block", "risk-too-small")
                continue
            budget = shares * signal.ref_price
            max_budget = equity * config.max_weight_pct
            if budget > max_budget:
                budget = max_budget
                self._log_signal(day, signal, "trim", "weight-cap")
            room = exposure_cap - invested
            if budget > room:
                if room < signal.ref_price:
                    self._log_signal(day, signal, "block", "exposure-cap")
                    continue
                budget = room
                self._log_signal(day, signal, "trim", "exposure-cap")
            orders.append(
                Order(
                    "buy",
                    signal.symbol,
                    budget=budget,
                    stop=signal.stop,
                    target=signal.target,
                )
            )
            approved.append(signal)
            invested += budget
            slots -= 1
        return GateResult(orders=orders, approved=approved)

    def _invalid_levels(self, signal: Signal) -> str | None:
        if signal.stop is None or signal.target is None:
            return "no-stop-target"
        values = (signal.ref_price, signal.stop, signal.target)
        if not all(math.isfinite(value) for value in values):
            return "non-finite-levels"
        if signal.stop <= 0:
            return "stop-not-positive"
        if signal.stop >= signal.ref_price:
            return "stop-not-below-entry"
        if signal.target <= signal.ref_price:
            return "target-not-above-entry"
        return None

    def _reduction_sells(self, portfolio: PortfolioView) -> list[Order]:
        def unrealized(position: PositionView) -> float:
            if position.shares <= 0 or position.entry_price <= 0:
                return 0.0
            return position.value / (position.shares * position.entry_price) - 1.0

        positions = sorted(
            portfolio.positions.values(),
            key=lambda position: (unrealized(position), position.symbol),
        )
        total = sum(position.value for position in positions)
        target = total * self.config.drawdown_reduce_target
        remaining = total
        orders: list[Order] = []
        for position in positions:
            if remaining <= target:
                break
            orders.append(Order("sell", position.symbol))
            remaining -= position.value
        return orders

    def _drop_all(self, day: date, signals: list[Signal], reason: str) -> None:
        for signal in signals:
            self._log_signal(day, signal, "block", reason)

    def _log_signal(self, day: date, signal: Signal, action: str, reason: str) -> None:
        self._log(day, action, reason, strategy=signal.strategy, symbol=signal.symbol)

    def _log(
        self,
        day: date,
        action: str,
        reason: str,
        *,
        strategy: str | None = None,
        symbol: str | None = None,
        detail: str = "",
    ) -> None:
        self.interventions.append(
            Intervention(
                day=day,
                action=action,
                reason=reason,
                strategy=strategy,
                symbol=symbol,
                detail=detail,
            )
        )

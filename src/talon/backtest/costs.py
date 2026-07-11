from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

KR_SELL_TAX_SCHEDULE: tuple[tuple[date, float], ...] = (
    (date(1996, 1, 1), 0.0030),
    (date(2019, 6, 3), 0.0025),
    (date(2021, 1, 1), 0.0023),
    (date(2023, 1, 1), 0.0020),
    (date(2024, 1, 1), 0.0018),
    (date(2025, 1, 1), 0.0015),
    (date(2026, 1, 1), 0.0020),
)


class CostModel(Protocol):
    def buy_fee(self, notional: float, day: date) -> float: ...

    def sell_fee(self, notional: float, day: date) -> float: ...


@dataclass(frozen=True)
class KrCostModel:
    commission_pct: float = 0.00015
    sell_tax_schedule: tuple[tuple[date, float], ...] = field(default=KR_SELL_TAX_SCHEDULE)

    def sell_tax_rate(self, day: date) -> float:
        rate = 0.0
        for effective, value in self.sell_tax_schedule:
            if day >= effective:
                rate = value
            else:
                break
        return rate

    def buy_fee(self, notional: float, day: date) -> float:
        return notional * self.commission_pct

    def sell_fee(self, notional: float, day: date) -> float:
        return notional * (self.commission_pct + self.sell_tax_rate(day))

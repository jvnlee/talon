from collections.abc import Sequence
from dataclasses import dataclass

import polars as pl

from talon.factors.engine import column_min_lags

EXECUTION_MODES = ("open", "close_overnight")

INTRADAY_EXECUTIONS = frozenset({"close_overnight"})
FORMING_COLUMNS = frozenset(
    {
        "close",
        "high",
        "low",
        "volume",
        "value",
        "raw_close",
        "raw_high",
        "raw_low",
        "intraday_ret",
        "limit_up",
        "limit_down",
        "limit_up_touch",
        "limit_down_touch",
    }
)


@dataclass(frozen=True)
class Signal:
    strategy: str
    symbol: str
    score: float
    ref_price: float
    stop: float | None
    target: float | None
    min_open: float | None = None
    execution: str = "open"


@dataclass(frozen=True)
class StrategySpec:
    name: str
    entry: tuple[str, ...]
    score: str
    stop: str
    target: str | None = None
    exit: str | None = None
    min_open: str | None = None
    ref_price: str = "close"
    execution: str = "open"
    max_hold_days: int = 20

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(f"전략 이름은 식별자여야 합니다: {self.name!r}")
        if not self.entry:
            raise ValueError(f"{self.name}: 진입 조건이 비어 있습니다")
        if self.max_hold_days < 1:
            raise ValueError(f"{self.name}: 최대 보유일은 1 이상이어야 합니다")
        if self.execution not in EXECUTION_MODES:
            raise ValueError(f"{self.name}: 지원하지 않는 실행 모드입니다: {self.execution!r}")
        if self.target is None and self.execution not in INTRADAY_EXECUTIONS:
            raise ValueError(
                f"{self.name}: 목표가 없는 전략은 익일 시가 청산이 강제되는 "
                "close_overnight 실행에서만 허용됩니다 (ADR 0006)"
            )

    def _column(self, part: str) -> str:
        return f"{self.name}__{part}"

    def columns(self) -> dict[str, str]:
        exprs = {self._column(f"entry{i}"): text for i, text in enumerate(self.entry)}
        exprs[self._column("score")] = f"CSRank({self.score})"
        exprs[self._column("stop")] = self.stop
        if self.target is not None:
            exprs[self._column("target")] = self.target
        if self.exit is not None:
            exprs[self._column("exit")] = self.exit
        if self.min_open is not None:
            exprs[self._column("min_open")] = self.min_open
        if self.ref_price != "close":
            exprs[self._column("ref")] = self.ref_price
        return exprs

    def candidates(self, day_frame: pl.DataFrame) -> list[Signal]:
        condition = pl.all_horizontal(
            [pl.col(self._column(f"entry{i}")).fill_null(False) for i in range(len(self.entry))]
        )
        ref_column = "close" if self.ref_price == "close" else self._column("ref")
        selections = [
            pl.col("symbol"),
            pl.col(ref_column).alias("ref_price"),
            pl.col(self._column("score")).alias("score"),
            pl.col(self._column("stop")).alias("stop"),
        ]
        if self.target is not None:
            selections.append(pl.col(self._column("target")).alias("target"))
        if self.min_open is not None:
            selections.append(pl.col(self._column("min_open")).alias("min_open"))
        rows = day_frame.filter(condition).select(selections)
        return [
            Signal(
                strategy=self.name,
                symbol=row["symbol"],
                score=row["score"] if row["score"] is not None else 0.0,
                ref_price=row["ref_price"],
                stop=row["stop"],
                target=row.get("target"),
                min_open=row.get("min_open"),
                execution=self.execution,
            )
            for row in rows.iter_rows(named=True)
        ]

    def wants_exit(self, day_frame: pl.DataFrame, symbol: str) -> bool:
        if self.exit is None:
            return False
        rows = day_frame.filter(pl.col("symbol") == symbol)
        if rows.is_empty():
            return False
        return rows.get_column(self._column("exit")).item() is True

    def decision_columns(self) -> dict[str, str]:
        parts = {f"entry[{index}]": text for index, text in enumerate(self.entry)}
        parts["score"] = self.score
        parts["ref_price"] = self.ref_price
        parts["stop"] = self.stop
        if self.target is not None:
            parts["target"] = self.target
        return parts


@dataclass(frozen=True)
class IntradayViolation:
    strategy: str
    part: str
    column: str
    expression: str

    def describe(self) -> str:
        return f"{self.strategy}.{self.part} 당일 {self.column} 참조: {self.expression}"


def verify_intraday(specs: Sequence[StrategySpec]) -> list[IntradayViolation]:
    violations: list[IntradayViolation] = []
    for spec in specs:
        if spec.execution not in INTRADAY_EXECUTIONS:
            continue
        for part, text in spec.decision_columns().items():
            for column, lag in sorted(column_min_lags(text).items()):
                if lag == 0 and column in FORMING_COLUMNS:
                    violations.append(
                        IntradayViolation(
                            strategy=spec.name,
                            part=part,
                            column=column,
                            expression=text,
                        )
                    )
    return violations

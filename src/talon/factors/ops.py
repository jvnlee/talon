from collections.abc import Callable
from dataclasses import dataclass

import polars as pl

ELEMENT = "element"
TIME_SERIES = "ts"
CROSS_SECTION = "cross"


@dataclass(frozen=True)
class Op:
    name: str
    expr_args: int
    int_args: int
    kind: str
    min_int: int
    build: Callable[[list[pl.Expr], list[int]], pl.Expr]
    warmup: Callable[[list[int], list[int]], int]


def _max_warmup(child_warmups: list[int], params: list[int]) -> int:
    return max(child_warmups, default=0)


def _shift_warmup(child_warmups: list[int], params: list[int]) -> int:
    return child_warmups[0] + params[0]


def _window_warmup(child_warmups: list[int], params: list[int]) -> int:
    return child_warmups[0] + params[0] - 1


def _pair_window_warmup(child_warmups: list[int], params: list[int]) -> int:
    return max(child_warmups) + params[0] - 1


def _ema_warmup(child_warmups: list[int], params: list[int]) -> int:
    return child_warmups[0] + 4 * params[0]


REGISTRY: dict[str, Op] = {}


def _register(op: Op) -> None:
    REGISTRY[op.name] = op


_register(Op("Ref", 1, 1, TIME_SERIES, 0, lambda c, p: c[0].shift(p[0]), _shift_warmup))
_register(Op("Delta", 1, 1, TIME_SERIES, 1, lambda c, p: c[0] - c[0].shift(p[0]), _shift_warmup))
_register(Op("Mean", 1, 1, TIME_SERIES, 1, lambda c, p: c[0].rolling_mean(p[0]), _window_warmup))
_register(Op("Sum", 1, 1, TIME_SERIES, 1, lambda c, p: c[0].rolling_sum(p[0]), _window_warmup))
_register(Op("Std", 1, 1, TIME_SERIES, 2, lambda c, p: c[0].rolling_std(p[0]), _window_warmup))
_register(Op("Max", 1, 1, TIME_SERIES, 1, lambda c, p: c[0].rolling_max(p[0]), _window_warmup))
_register(Op("Min", 1, 1, TIME_SERIES, 1, lambda c, p: c[0].rolling_min(p[0]), _window_warmup))
_register(Op("EMA", 1, 1, TIME_SERIES, 1, lambda c, p: c[0].ewm_mean(span=p[0]), _ema_warmup))
_register(Op("Abs", 1, 0, ELEMENT, 1, lambda c, p: c[0].abs(), _max_warmup))
_register(Op("Log", 1, 0, ELEMENT, 1, lambda c, p: c[0].log(), _max_warmup))
_register(Op("Sign", 1, 0, ELEMENT, 1, lambda c, p: c[0].sign(), _max_warmup))
_register(Op("Greater", 2, 0, ELEMENT, 1, lambda c, p: pl.max_horizontal(c[0], c[1]), _max_warmup))
_register(Op("Less", 2, 0, ELEMENT, 1, lambda c, p: pl.min_horizontal(c[0], c[1]), _max_warmup))
_register(
    Op(
        "If",
        3,
        0,
        ELEMENT,
        1,
        lambda c, p: pl.when(c[0]).then(c[1]).otherwise(c[2]),
        _max_warmup,
    )
)
_register(
    Op(
        "CSRank",
        1,
        0,
        CROSS_SECTION,
        1,
        lambda c, p: c[0].rank("average") / c[0].count(),
        _max_warmup,
    )
)

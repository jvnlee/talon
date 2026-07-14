import difflib

import polars as pl

from talon.errors import FactorExpressionError
from talon.factors.expr import Binary, Column, Compare, Const, Func, Node, Unary
from talon.factors.ops import CROSS_SECTION, REGISTRY, TIME_SERIES, Op
from talon.factors.parser import parse

RESERVED_COLUMNS = frozenset({"day", "symbol"})
MAX_EXPONENT = 10.0


def _suggest(name: str, candidates: list[str]) -> str:
    matches = difflib.get_close_matches(name, candidates, n=1)
    return f" (혹시 {matches[0]!r}?)" if matches else ""


class _Compiler:
    def __init__(self, feature_columns: set[str]) -> None:
        self.feature_columns = feature_columns
        self.stages: list[tuple[str, pl.Expr]] = []
        self.cache: dict[Node, tuple[pl.Expr, int]] = {}

    def compile(self, node: Node) -> tuple[pl.Expr, int]:
        cached = self.cache.get(node)
        if cached is not None:
            return cached
        result = self._compile(node)
        self.cache[node] = result
        return result

    def _compile(self, node: Node) -> tuple[pl.Expr, int]:
        if isinstance(node, Const):
            return pl.lit(node.value), 0
        if isinstance(node, Column):
            if node.name not in self.feature_columns:
                raise FactorExpressionError(
                    f"알 수 없는 컬럼: {node.name}"
                    + _suggest(node.name, sorted(self.feature_columns))
                )
            return pl.col(node.name), 0
        if isinstance(node, Unary):
            expr, warmup = self.compile(node.operand)
            return -expr, warmup
        if isinstance(node, Binary):
            if node.op == "**":
                return self._compile_power(node)
            left, left_warmup = self.compile(node.left)
            right, right_warmup = self.compile(node.right)
            combined = {
                "+": left + right,
                "-": left - right,
                "*": left * right,
                "/": left / right,
            }[node.op]
            return combined, max(left_warmup, right_warmup)
        if isinstance(node, Compare):
            left, left_warmup = self.compile(node.left)
            right, right_warmup = self.compile(node.right)
            combined = {
                "<": left < right,
                "<=": left <= right,
                ">": left > right,
                ">=": left >= right,
                "==": left == right,
                "!=": left != right,
            }[node.op]
            return combined, max(left_warmup, right_warmup)
        assert isinstance(node, Func)
        return self._compile_func(node)

    def _compile_power(self, node: Binary) -> tuple[pl.Expr, int]:
        exponent_node = node.right
        sign = 1.0
        if isinstance(exponent_node, Unary) and exponent_node.op == "-":
            sign = -1.0
            exponent_node = exponent_node.operand
        if not isinstance(exponent_node, Const):
            raise FactorExpressionError("거듭제곱 지수는 상수여야 합니다")
        exponent = sign * exponent_node.value
        if abs(exponent) > MAX_EXPONENT:
            raise FactorExpressionError(
                f"거듭제곱 지수는 |{MAX_EXPONENT:g}| 이하여야 합니다 (받음: {exponent:g})"
            )
        left, left_warmup = self.compile(node.left)
        return left**exponent, left_warmup

    def _compile_func(self, node: Func) -> tuple[pl.Expr, int]:
        op = REGISTRY.get(node.name)
        if op is None:
            raise FactorExpressionError(
                f"알 수 없는 함수: {node.name}" + _suggest(node.name, sorted(REGISTRY))
            )
        expected = op.expr_args + op.int_args
        if len(node.args) != expected:
            raise FactorExpressionError(
                f"{op.name}은(는) 인자 {expected}개가 필요합니다 (받음: {len(node.args)})"
            )
        child_exprs: list[pl.Expr] = []
        child_warmups: list[int] = []
        for arg in node.args[: op.expr_args]:
            expr, warmup = self.compile(arg)
            child_exprs.append(expr)
            child_warmups.append(warmup)
        params = [_int_param(op, arg) for arg in node.args[op.expr_args :]]
        built = op.build(child_exprs, params)
        warmup = op.warmup(child_warmups, params)
        if op.kind == TIME_SERIES:
            return self._materialize(built.over("symbol", order_by="day")), warmup
        if op.kind == CROSS_SECTION:
            return self._materialize(built.over("day")), warmup
        return built, warmup

    def _materialize(self, expr: pl.Expr) -> pl.Expr:
        name = f"_fx{len(self.stages)}"
        self.stages.append((name, expr))
        return pl.col(name)


def compute_factors(
    panel: pl.DataFrame,
    factors: dict[str, str],
    *,
    keep_intermediate: bool = False,
) -> pl.DataFrame:
    feature_columns = set(panel.columns) - RESERVED_COLUMNS
    for name in factors:
        if name in panel.columns:
            raise FactorExpressionError(f"팩터 이름이 기존 컬럼과 충돌합니다: {name}")
    compiler = _Compiler(feature_columns)
    outputs = {name: compiler.compile(parse(text))[0] for name, text in factors.items()}
    frame = panel.sort("day", "symbol")
    for stage_name, stage_expr in compiler.stages:
        frame = frame.with_columns(stage_expr.alias(stage_name))
    frame = frame.with_columns(*[expr.alias(name) for name, expr in outputs.items()])
    if not keep_intermediate:
        frame = frame.drop([name for name, _ in compiler.stages])
    return frame


def warmup_periods(factors: dict[str, str], feature_columns: set[str]) -> dict[str, int]:
    compiler = _Compiler(feature_columns)
    return {name: compiler.compile(parse(text))[1] for name, text in factors.items()}


def _int_param(op: Op, arg: Node) -> int:
    sign = 1
    if isinstance(arg, Unary) and arg.op == "-":
        sign = -1
        arg = arg.operand
    if not isinstance(arg, Const) or not float(arg.value).is_integer():
        raise FactorExpressionError(f"{op.name}의 윈도/시차 인자는 정수 상수여야 합니다")
    value = sign * int(arg.value)
    if value < op.min_int:
        raise FactorExpressionError(
            f"{op.name}의 정수 인자는 {op.min_int} 이상이어야 합니다 (받음: {value}) — "
            "음수 시차는 미래 참조라 금지됩니다"
        )
    return value


def column_min_lags(text: str) -> dict[str, int]:
    lags: dict[str, int] = {}
    _collect_lags(parse(text), 0, lags)
    return lags


def _collect_lags(node: Node, lag: int, lags: dict[str, int]) -> None:
    if isinstance(node, Const):
        return
    if isinstance(node, Column):
        seen = lags.get(node.name)
        if seen is None or lag < seen:
            lags[node.name] = lag
        return
    if isinstance(node, Unary):
        _collect_lags(node.operand, lag, lags)
        return
    if isinstance(node, Binary | Compare):
        _collect_lags(node.left, lag, lags)
        _collect_lags(node.right, lag, lags)
        return
    assert isinstance(node, Func)
    op = REGISTRY.get(node.name)
    if op is None:
        raise FactorExpressionError(
            f"알 수 없는 함수: {node.name}" + _suggest(node.name, sorted(REGISTRY))
        )
    expected = op.expr_args + op.int_args
    if len(node.args) != expected:
        raise FactorExpressionError(
            f"{op.name}은(는) 인자 {expected}개가 필요합니다 (받음: {len(node.args)})"
        )
    shift = _int_param(op, node.args[op.expr_args]) if op.name == "Ref" else 0
    for arg in node.args[: op.expr_args]:
        _collect_lags(arg, lag + shift, lags)

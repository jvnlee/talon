import ast

from talon.errors import FactorExpressionError
from talon.factors.expr import Binary, Column, Compare, Const, Func, Node, Unary

MAX_EXPRESSION_LENGTH = 2_000
MAX_NODE_COUNT = 300
MAX_CONSTANT = 1e12

_BINARY_OPS: dict[type[ast.operator], str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.Pow: "**",
}

_UNARY_OPS: dict[type[ast.unaryop], str] = {
    ast.USub: "-",
}

_COMPARE_OPS: dict[type[ast.cmpop], str] = {
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Eq: "==",
    ast.NotEq: "!=",
}


def parse(text: str) -> Node:
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise FactorExpressionError(f"표현식이 너무 깁니다 ({len(text)} > {MAX_EXPRESSION_LENGTH})")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise FactorExpressionError(f"문법 오류 (offset {exc.offset}): {text!r}") from exc
    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > MAX_NODE_COUNT:
        raise FactorExpressionError(
            f"표현식이 너무 복잡합니다 (노드 {node_count} > {MAX_NODE_COUNT})"
        )
    return _convert(tree.body)


def _fail(node: ast.AST, message: str) -> FactorExpressionError:
    return FactorExpressionError(f"{message} (col {getattr(node, 'col_offset', '?')})")


def _convert(node: ast.expr) -> Node:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise _fail(node, f"숫자 상수만 허용됩니다: {node.value!r}")
        if abs(node.value) > MAX_CONSTANT:
            raise _fail(node, f"상수가 너무 큽니다 (|x| > {MAX_CONSTANT:g})")
        return Const(float(node.value))
    if isinstance(node, ast.Name):
        if node.id.startswith("_"):
            raise _fail(node, f"밑줄로 시작하는 식별자는 허용되지 않습니다: {node.id}")
        return Column(node.id)
    if isinstance(node, ast.UnaryOp):
        unary_type = type(node.op)
        if unary_type not in _UNARY_OPS:
            raise _fail(node, f"허용되지 않는 단항 연산자: {unary_type.__name__}")
        return Unary(_UNARY_OPS[unary_type], _convert(node.operand))
    if isinstance(node, ast.BinOp):
        binary_type = type(node.op)
        if binary_type not in _BINARY_OPS:
            raise _fail(node, f"허용되지 않는 이항 연산자: {binary_type.__name__}")
        return Binary(_BINARY_OPS[binary_type], _convert(node.left), _convert(node.right))
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise _fail(node, "연쇄 비교는 허용되지 않습니다 (a < b < c)")
        compare_type = type(node.ops[0])
        if compare_type not in _COMPARE_OPS:
            raise _fail(node, f"허용되지 않는 비교 연산자: {compare_type.__name__}")
        return Compare(
            _COMPARE_OPS[compare_type], _convert(node.left), _convert(node.comparators[0])
        )
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise _fail(node, "함수 이름은 단순 식별자여야 합니다")
        if node.func.id.startswith("_"):
            raise _fail(node, f"밑줄로 시작하는 함수는 허용되지 않습니다: {node.func.id}")
        if node.keywords:
            raise _fail(node, "키워드 인자는 허용되지 않습니다")
        return Func(node.func.id, tuple(_convert(arg) for arg in node.args))
    raise _fail(node, f"허용되지 않는 구문: {type(node).__name__}")

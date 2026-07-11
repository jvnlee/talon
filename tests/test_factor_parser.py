import pytest

from talon.errors import FactorExpressionError
from talon.factors.expr import Binary, Column, Compare, Const, Func, Unary
from talon.factors.parser import parse


def test_parses_arithmetic_and_calls():
    node = parse("Mean(close, 20) / Ref(close, 5) - 1")
    assert node == Binary(
        "-",
        Binary(
            "/",
            Func("Mean", (Column("close"), Const(20.0))),
            Func("Ref", (Column("close"), Const(5.0))),
        ),
        Const(1.0),
    )


def test_parses_nested_calls_and_unary():
    node = parse("Std(Ref(close, 1), 10) * -2.5")
    assert node == Binary(
        "*",
        Func("Std", (Func("Ref", (Column("close"), Const(1.0))), Const(10.0))),
        Unary("-", Const(2.5)),
    )


def test_parses_comparison():
    node = parse("close > Mean(close, 20)")
    assert node == Compare(">", Column("close"), Func("Mean", (Column("close"), Const(20.0))))


def test_parses_power():
    assert parse("close ** 2") == Binary("**", Column("close"), Const(2.0))


def test_parenthesized_name_call_is_plain_call():
    assert parse("(Mean)(close, 20)") == Func("Mean", (Column("close"), Const(20.0)))


@pytest.mark.parametrize(
    "text",
    [
        "().__class__",
        "close.__class__",
        "close.shift(1)",
        "data[0]",
        "lambda: 1",
        "[c for c in close]",
        "f'{close}'",
        "'close'",
        "True",
        "None",
        "1 < close < 2",
        "Mean(close, window=20)",
        "(lambda: 1)()",
        "__import__('os')",
        "_secret",
        "close and volume",
        "close | volume",
        "close % 2",
        "close // 2",
        "close if volume else open",
        "(x := 1)",
    ],
)
def test_rejects_disallowed_constructs(text):
    with pytest.raises(FactorExpressionError):
        parse(text)


def test_rejects_huge_constant():
    with pytest.raises(FactorExpressionError, match="상수가 너무"):
        parse("close * 1e13")


def test_rejects_oversized_expression():
    with pytest.raises(FactorExpressionError, match="깁니다"):
        parse("close + " * 400 + "close")


def test_rejects_excessive_node_count():
    text = "+".join(["close"] * 200)
    with pytest.raises(FactorExpressionError, match="복잡"):
        parse(text)


def test_syntax_error_reports_position():
    with pytest.raises(FactorExpressionError, match="문법 오류"):
        parse("Mean(close,, 20)")

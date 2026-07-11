from datetime import date, timedelta

import polars as pl
import pytest

from talon.errors import FactorExpressionError
from talon.factors.engine import compute_factors, warmup_periods
from talon.factors.ops import REGISTRY

BASE = date(2026, 1, 5)


def d(i):
    return BASE + timedelta(days=i)


def make_panel(days=6):
    rows = []
    for i in range(days):
        rows.append({"day": d(i), "symbol": "A", "close": float(i + 1), "volume": float(10 - i)})
        rows.append(
            {"day": d(i), "symbol": "B", "close": float((i + 1) * 10), "volume": float(i + 1)}
        )
    return pl.DataFrame(rows)


def column(frame, symbol, name):
    return frame.filter(pl.col("symbol") == symbol).sort("day")[name].to_list()


def test_mean_and_warmup_nulls_per_symbol():
    result = compute_factors(make_panel(), {"f": "Mean(close, 2)"})
    assert column(result, "A", "f") == [None, 1.5, 2.5, 3.5, 4.5, 5.5]
    assert column(result, "B", "f") == [None, 15.0, 25.0, 35.0, 45.0, 55.0]


def test_ref_does_not_bleed_across_symbols():
    result = compute_factors(make_panel(), {"f": "Ref(close, 1)"})
    assert column(result, "A", "f") == [None, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert column(result, "B", "f") == [None, 10.0, 20.0, 30.0, 40.0, 50.0]


def test_nested_expression_and_arithmetic():
    result = compute_factors(make_panel(), {"f": "Mean(Ref(close, 1), 2) * 2"})
    assert column(result, "A", "f") == [None, None, 3.0, 5.0, 7.0, 9.0]


def test_cross_sectional_rank_excludes_nulls():
    panel = make_panel(3)
    result = compute_factors(panel, {"f": "CSRank(Ref(close, 1))"})
    assert column(result, "A", "f") == [None, 0.5, 0.5]
    assert column(result, "B", "f") == [None, 1.0, 1.0]


def test_if_compare_and_greater():
    result = compute_factors(
        make_panel(3),
        {
            "flag": "If(close > volume, 1, 0)",
            "cap": "Greater(close, volume)",
        },
    )
    assert column(result, "A", "flag") == [0.0, 0.0, 0.0]
    assert column(result, "B", "flag") == [1.0, 1.0, 1.0]
    assert column(result, "A", "cap") == [10.0, 9.0, 8.0]


def test_shared_subexpression_is_deduplicated():
    result = compute_factors(
        make_panel(),
        {"a": "Mean(close, 3) / close", "b": "Mean(close, 3) - close"},
        keep_intermediate=True,
    )
    intermediates = [c for c in result.columns if c.startswith("_fx")]
    assert len(intermediates) == 1


def test_warmup_periods_arithmetic():
    features = {"close", "volume"}
    warmups = warmup_periods(
        {
            "ref": "Ref(close, 3)",
            "mean": "Mean(close, 5)",
            "nested": "Mean(Ref(close, 2), 3)",
            "cs": "CSRank(Mean(close, 3))",
            "mix": "If(close > 0, Ref(close, 1), Mean(close, 4))",
            "ema": "EMA(close, 5)",
        },
        features,
    )
    assert warmups == {"ref": 3, "mean": 4, "nested": 4, "cs": 2, "mix": 3, "ema": 20}


def test_ema_matches_pandas_adjusted_semantics():
    result = compute_factors(make_panel(), {"e": "EMA(close, 3)"})
    values = column(result, "A", "e")
    assert values[:3] == pytest.approx([1.0, 1.6666666666666667, 2.4285714285714284])


def test_power_with_constant_exponent():
    result = compute_factors(make_panel(3), {"sq": "close ** 2", "inv": "close ** -1"})
    assert column(result, "A", "sq") == pytest.approx([1.0, 4.0, 9.0])
    assert column(result, "A", "inv") == pytest.approx([1.0, 0.5, 1 / 3])


def test_power_rejects_non_constant_exponent():
    with pytest.raises(FactorExpressionError, match="상수"):
        compute_factors(make_panel(), {"f": "close ** volume"})


def test_power_rejects_large_exponent():
    with pytest.raises(FactorExpressionError, match="이하"):
        compute_factors(make_panel(), {"f": "close ** 11"})


def test_null_in_window_poisons_rolling_result():
    frame = pl.DataFrame(
        {
            "day": [d(i) for i in range(5)],
            "symbol": ["A"] * 5,
            "close": [1.0, None, 3.0, 4.0, 5.0],
        }
    )
    result = compute_factors(frame, {"m": "Mean(close, 3)", "mx": "Max(close, 3)"})
    assert result["m"].to_list() == [None, None, None, None, 4.0]
    assert result["mx"].to_list() == [None, None, None, None, 5.0]


def test_unknown_column_suggests_close_match():
    with pytest.raises(FactorExpressionError, match="close"):
        compute_factors(make_panel(), {"f": "Mean(clse, 2)"})


def test_unknown_function_suggests_close_match():
    with pytest.raises(FactorExpressionError, match="Mean"):
        compute_factors(make_panel(), {"f": "Meen(close, 2)"})


def test_negative_lag_is_rejected_as_lookahead():
    with pytest.raises(FactorExpressionError, match="미래 참조"):
        compute_factors(make_panel(), {"f": "Ref(close, -1)"})


def test_float_window_rejected():
    with pytest.raises(FactorExpressionError, match="정수 상수"):
        compute_factors(make_panel(), {"f": "Mean(close, 2.5)"})


def test_wrong_arg_count_rejected():
    with pytest.raises(FactorExpressionError, match="인자"):
        compute_factors(make_panel(), {"f": "Mean(close)"})


def test_factor_name_collision_rejected():
    with pytest.raises(FactorExpressionError, match="충돌"):
        compute_factors(make_panel(), {"close": "Ref(close, 1)"})


TRUNCATION_CASES = {
    "Ref": "Ref(close, 2)",
    "Delta": "Delta(close, 2)",
    "Mean": "Mean(close, 3)",
    "Sum": "Sum(close, 3)",
    "EMA": "EMA(close, 2)",
    "Std": "Std(close, 3)",
    "Max": "Max(close, 3)",
    "Min": "Min(close, 3)",
    "Abs": "Abs(close - volume)",
    "Log": "Log(close)",
    "Sign": "Sign(close - volume)",
    "Greater": "Greater(close, volume)",
    "Less": "Less(close, volume)",
    "If": "If(close > volume, close, volume)",
    "CSRank": "CSRank(Mean(close, 2))",
}


def test_truncation_cases_cover_registry():
    assert set(TRUNCATION_CASES) == set(REGISTRY)


@pytest.mark.parametrize(("op", "text"), sorted(TRUNCATION_CASES.items()))
def test_truncation_invariance(op, text):
    panel = make_panel(8)
    cutoff = d(5)
    full = compute_factors(panel, {"f": text})
    truncated = compute_factors(panel.filter(pl.col("day") <= cutoff), {"f": text})

    full_at_cutoff = full.filter(pl.col("day") == cutoff).sort("symbol")["f"].to_list()
    trunc_at_cutoff = truncated.filter(pl.col("day") == cutoff).sort("symbol")["f"].to_list()
    assert full_at_cutoff == pytest.approx(trunc_at_cutoff)

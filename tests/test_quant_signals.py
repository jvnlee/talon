import polars as pl
import pytest

from talon.quant.signals import StrategySpec


def spec(**overrides):
    base = {
        "name": "s1",
        "entry": ("close > 100",),
        "score": "close",
        "stop": "close - 10",
        "target": "close + 20",
        "exit": "close < 90",
    }
    base.update(overrides)
    return StrategySpec(**base)


def test_spec_validation():
    with pytest.raises(ValueError, match="식별자"):
        spec(name="foo-bar")
    with pytest.raises(ValueError, match="진입 조건"):
        spec(entry=())
    with pytest.raises(ValueError, match="보유일"):
        spec(max_hold_days=0)


def test_columns_are_prefixed_and_score_is_ranked():
    columns = spec(entry=("close > 100", "volume > 0")).columns()
    assert set(columns) == {
        "s1__entry0",
        "s1__entry1",
        "s1__score",
        "s1__stop",
        "s1__target",
        "s1__exit",
    }
    assert columns["s1__score"] == "CSRank(close)"
    assert columns["s1__entry1"] == "volume > 0"


def test_columns_without_exit():
    assert "s1__exit" not in spec(exit=None).columns()


def test_columns_with_min_open():
    columns = spec(min_open="close * 1.01").columns()
    assert columns["s1__min_open"] == "close * 1.01"
    assert "s1__min_open" not in spec().columns()


def day_frame():
    return pl.DataFrame(
        {
            "symbol": ["AAA", "BBB", "CCC"],
            "close": [110.0, 120.0, 95.0],
            "s1__entry0": pl.Series([True, None, False], dtype=pl.Boolean),
            "s1__score": [0.9, None, 0.1],
            "s1__stop": [100.0, None, None],
            "s1__target": [130.0, None, None],
            "s1__exit": pl.Series([True, None, False], dtype=pl.Boolean),
        }
    )


def test_candidates_skip_null_and_false_entries():
    candidates = spec().candidates(day_frame())
    assert [c.symbol for c in candidates] == ["AAA"]
    candidate = candidates[0]
    assert candidate.strategy == "s1"
    assert candidate.ref_price == 110.0
    assert candidate.score == 0.9
    assert candidate.stop == 100.0
    assert candidate.target == 130.0


def test_candidate_null_score_becomes_zero():
    frame = day_frame().with_columns(pl.lit(None).cast(pl.Float64).alias("s1__score"))
    assert spec().candidates(frame)[0].score == 0.0


def test_candidates_carry_min_open():
    assert spec().candidates(day_frame())[0].min_open is None
    frame = day_frame().with_columns(pl.Series("s1__min_open", [111.0, None, None]))
    candidate = spec(min_open="close * 1.01").candidates(frame)[0]
    assert candidate.min_open == 111.0


def test_wants_exit_true_only_on_true():
    strategy = spec()
    frame = day_frame()
    assert strategy.wants_exit(frame, "AAA") is True
    assert strategy.wants_exit(frame, "BBB") is False
    assert strategy.wants_exit(frame, "CCC") is False
    assert strategy.wants_exit(frame, "ZZZ") is False
    assert spec(exit=None).wants_exit(frame, "AAA") is False

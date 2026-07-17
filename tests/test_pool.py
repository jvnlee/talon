from datetime import UTC, datetime

import pytest

from talon.errors import SourceError
from talon.ingest.pool import parallel_fetch

STAMP = datetime(2026, 7, 16, 6, 10, tzinfo=UTC)


def test_results_keep_input_order_and_are_stamped():
    symbols = [f"{i:06d}" for i in range(20)]

    fetched, failed = parallel_fetch(
        symbols,
        lambda symbol: {"symbol": symbol},
        workers=8,
        max_failure_ratio=0.2,
        log_name="pool",
        now=lambda: STAMP,
    )

    assert failed == 0
    assert [symbol for symbol, _, _ in fetched] == symbols
    assert all(value == {"symbol": symbol} for symbol, value, _ in fetched)
    assert all(stamp == STAMP for _, _, stamp in fetched)


def test_failures_below_threshold_are_tolerated():
    def flaky(symbol):
        if symbol == "000004":
            raise SourceError("timeout")
        return {"symbol": symbol}

    fetched, failed = parallel_fetch(
        [f"{i:06d}" for i in range(5)],
        flaky,
        workers=3,
        max_failure_ratio=0.2,
        log_name="pool",
    )

    assert failed == 1
    assert [symbol for symbol, _, _ in fetched] == ["000000", "000001", "000002", "000003"]


def test_too_many_failures_abort():
    def boom(symbol):
        raise SourceError("kis down")

    with pytest.raises(SourceError):
        parallel_fetch(
            ["000001", "000002", "000003"],
            boom,
            workers=2,
            max_failure_ratio=0.2,
            log_name="pool",
        )

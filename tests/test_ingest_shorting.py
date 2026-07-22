from datetime import UTC, date, datetime, timedelta

import polars as pl

from talon.data.store import (
    DAILY_CANDLES,
    DAILY_SNAPSHOT_SCHEMA,
    SHORTING,
    SHORTING_BALANCE,
    SHORTING_BALANCE_SCHEMA,
    SHORTING_INVESTOR,
    SHORTING_INVESTOR_SCHEMA,
    SHORTING_SCHEMA,
)
from talon.errors import SourceError
from talon.ingest.shorting import (
    ShortingFetchers,
    backfill_shorting,
    daily_shorting,
    verify_shorting,
)

FETCHED = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
START = date(2026, 7, 6)
END = date(2026, 7, 10)


def trade_frame(day, *, symbol="005930", short=100, total=1000, market="KOSPI"):
    ratio = short / total * 100 if total else 0.0
    return pl.DataFrame(
        {
            "day": [day],
            "symbol": [symbol],
            "market": [market],
            "short_volume": [short],
            "total_volume_consolidated": [total],
            "short_ratio_pct": [ratio],
            "short_value": [short * 10],
            "total_value_consolidated": [total * 10],
            "short_value_ratio_pct": [ratio],
            "fetched_at": [FETCHED],
        },
        schema=SHORTING_SCHEMA,
    )


def balance_frame(day, *, symbol="005930", qty=100, listed=1000, market="KOSPI"):
    ratio = qty / listed * 100 if listed else 0.0
    return pl.DataFrame(
        {
            "day": [day],
            "symbol": [symbol],
            "market": [market],
            "short_balance_qty": [qty],
            "listed_shares": [listed],
            "short_balance_value": [qty * 10],
            "market_cap": [listed * 100],
            "short_balance_ratio_pct": [ratio],
            "fetched_at": [FETCHED],
        },
        schema=SHORTING_BALANCE_SCHEMA,
    )


_INVESTOR_PARTS = {"institution": 10, "retail": 20, "foreign": 30, "other": 40}


def investor_long(days, *, markets=("KOSPI", "KOSDAQ")):
    rows = []
    for day in days:
        for market in markets:
            total = sum(_INVESTOR_PARTS.values())
            for investor, value in {**_INVESTOR_PARTS, "total": total}.items():
                rows.append(
                    {
                        "day": day,
                        "market": market,
                        "investor": investor,
                        "vol_shares": value,
                        "value_krw": value * 100,
                        "fetched_at": FETCHED,
                    }
                )
    return pl.DataFrame(rows, schema=SHORTING_INVESTOR_SCHEMA)


def empty_investor():
    return pl.DataFrame(schema=SHORTING_INVESTOR_SCHEMA)


def _balance_ok(day):
    return balance_frame(day)


def _investor_none(start, end):
    return empty_investor()


def _unused(*args, **kwargs):
    raise AssertionError("unexpected fetch")


def fetchers(*, trade=_unused, balance=_unused, investor=_unused):
    return ShortingFetchers(trade=trade, balance=balance, investor=investor)


def test_backfill_trade_loads_sessions(cfg, cal, state, snapshots):
    calls = []

    def trade(day):
        calls.append(day)
        return trade_frame(day)

    summary = backfill_shorting(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        dataset=SHORTING,
        start=START,
        end=END,
        fetchers=fetchers(trade=trade),
    )
    assert summary.status == "ok"
    assert summary.sessions == 5
    assert summary.loaded == 5
    assert len(calls) == 5
    assert snapshots.has_date(SHORTING, date(2026, 7, 8))


def test_backfill_trade_skips_existing(cfg, cal, state, snapshots):
    snapshots.write_date(SHORTING, date(2026, 7, 7), trade_frame(date(2026, 7, 7)))
    calls = []

    def trade(day):
        calls.append(day)
        return trade_frame(day)

    summary = backfill_shorting(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        dataset=SHORTING,
        start=START,
        end=END,
        fetchers=fetchers(trade=trade),
    )
    assert summary.skipped == 1
    assert summary.loaded == 4
    assert date(2026, 7, 7) not in calls


def test_backfill_trade_skips_not_ready_frames(cfg, cal, state, snapshots):
    def trade(day):
        return trade_frame(day, short=0, total=0)

    summary = backfill_shorting(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        dataset=SHORTING,
        start=START,
        end=END,
        fetchers=fetchers(trade=trade),
    )
    assert summary.status == "ok"
    assert summary.loaded == 0
    assert summary.skipped == 5
    assert not snapshots.has_date(SHORTING, date(2026, 7, 8))


def test_backfill_trade_aborts_after_consecutive_failures(cfg, cal, state, snapshots):
    calls = []

    def trade(day):
        calls.append(day)
        raise SourceError("boom")

    summary = backfill_shorting(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        dataset=SHORTING,
        start=START,
        end=END,
        fetchers=fetchers(trade=trade),
    )
    assert summary.status == "aborted"
    assert len(calls) == 3
    assert len(summary.failed) == 3


def test_backfill_trade_partial_on_isolated_failure(cfg, cal, state, snapshots):
    def trade(day):
        if day == date(2026, 7, 8):
            raise SourceError("boom")
        return trade_frame(day)

    summary = backfill_shorting(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        dataset=SHORTING,
        start=START,
        end=END,
        fetchers=fetchers(trade=trade),
    )
    assert summary.status == "partial"
    assert summary.loaded == 4
    assert summary.failed == ["2026-07-08"]


def test_backfill_balance_clamps_to_institution_start(cfg, cal, state, snapshots):
    calls = []

    def balance(day):
        calls.append(day)
        return balance_frame(day)

    summary = backfill_shorting(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        dataset=SHORTING_BALANCE,
        start=date(2016, 6, 1),
        end=date(2016, 7, 1),
        fetchers=fetchers(balance=balance),
    )
    assert min(calls) == date(2016, 6, 30)
    assert summary.loaded == len(calls)


def test_backfill_investor_fetches_one_call_per_year_chunk(cfg, cal, state, snapshots):
    sessions = cal.sessions_between(date(2017, 5, 22), date(2017, 5, 26))
    calls = []

    def investor(start, end):
        calls.append((start, end))
        return investor_long(sessions)

    summary = backfill_shorting(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        dataset=SHORTING_INVESTOR,
        start=date(2017, 1, 1),
        end=date(2017, 5, 26),
        fetchers=fetchers(investor=investor),
    )
    assert len(calls) == 1
    assert summary.loaded == len(sessions)
    for day in sessions:
        assert snapshots.has_date(SHORTING_INVESTOR, day)


def test_backfill_investor_resume_skips_present_chunk_without_fetch(cfg, cal, state, snapshots):
    sessions = cal.sessions_between(date(2017, 5, 22), date(2017, 5, 26))
    for day in sessions:
        snapshots.write_date(SHORTING_INVESTOR, day, investor_long([day]))
    calls = []

    def investor(start, end):
        calls.append((start, end))
        return investor_long(sessions)

    summary = backfill_shorting(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        dataset=SHORTING_INVESTOR,
        start=date(2017, 5, 22),
        end=date(2017, 5, 26),
        fetchers=fetchers(investor=investor),
    )
    assert calls == []
    assert summary.skipped == len(sessions)
    assert summary.loaded == 0


def test_backfill_investor_partial_chunk_writes_missing_days(cfg, cal, state, snapshots):
    sessions = cal.sessions_between(date(2017, 5, 22), date(2017, 5, 26))
    for day in sessions[:2]:
        snapshots.write_date(SHORTING_INVESTOR, day, investor_long([day]))

    def investor(start, end):
        return investor_long(sessions)

    summary = backfill_shorting(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        dataset=SHORTING_INVESTOR,
        start=date(2017, 5, 22),
        end=date(2017, 5, 26),
        fetchers=fetchers(investor=investor),
    )
    assert summary.loaded == len(sessions) - 2
    assert summary.skipped == 2


def kst(hour, minute=0):
    return datetime(2026, 7, 22, hour - 9, minute, tzinfo=UTC)


def test_daily_shorting_gates_trade_today_before_ready(cfg, cal, snapshots):
    calls = []

    def trade(day):
        calls.append(day)
        return trade_frame(day)

    result = daily_shorting(
        cfg,
        cal=cal,
        snapshots=snapshots,
        now=kst(16, 40),
        fetchers=fetchers(trade=trade, balance=_balance_ok, investor=_investor_none),
    )
    assert date(2026, 7, 22) not in calls
    assert date(2026, 7, 21) in calls
    assert "trade" in result


def test_daily_shorting_admits_trade_today_at_ready(cfg, cal, snapshots):
    calls = []

    def trade(day):
        calls.append(day)
        return trade_frame(day)

    daily_shorting(
        cfg,
        cal=cal,
        snapshots=snapshots,
        now=kst(18, 30),
        fetchers=fetchers(trade=trade, balance=_balance_ok, investor=_investor_none),
    )
    assert date(2026, 7, 22) in calls


def test_daily_shorting_balance_stays_three_sessions_back(cfg, cal, snapshots):
    balance_calls = []

    def balance(day):
        balance_calls.append(day)
        return balance_frame(day)

    daily_shorting(
        cfg,
        cal=cal,
        snapshots=snapshots,
        now=kst(18, 30),
        fetchers=fetchers(
            trade=lambda d: trade_frame(d),
            balance=balance,
            investor=lambda s, e: empty_investor(),
        ),
    )
    assert date(2026, 7, 22) not in balance_calls
    assert date(2026, 7, 21) not in balance_calls
    assert date(2026, 7, 20) not in balance_calls
    assert date(2026, 7, 16) in balance_calls


def test_daily_shorting_up_to_date(cfg, cal, snapshots):
    end = cal.latest_trading_day(date(2026, 7, 22))
    for day in cal.sessions_between(end - timedelta(days=40), end):
        snapshots.write_date(SHORTING, day, trade_frame(day))
        snapshots.write_date(SHORTING_BALANCE, day, balance_frame(day))
        snapshots.write_date(SHORTING_INVESTOR, day, investor_long([day]))
    result = daily_shorting(cfg, cal=cal, snapshots=snapshots, now=kst(18, 30), fetchers=fetchers())
    assert result == "trade up-to-date, balance up-to-date, investor up-to-date"


def daily_candle(day, symbol, volume, *, no_trade=False):
    return pl.DataFrame(
        {
            "day": [day],
            "symbol": [symbol],
            "open": [None if no_trade else 100.0],
            "high": [None if no_trade else 100.0],
            "low": [None if no_trade else 100.0],
            "close": [100.0],
            "volume": [volume],
            "value": [volume * 100.0],
            "change_pct": [0.0],
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )


def test_verify_ok_on_clean_data(cfg, snapshots):
    day = date(2026, 7, 10)
    snapshots.write_date(SHORTING, day, trade_frame(day, short=100, total=1000))
    snapshots.write_date(DAILY_CANDLES, day, daily_candle(day, "005930", 50000.0))
    snapshots.write_date(SHORTING_BALANCE, day, balance_frame(day, qty=100, listed=1000))
    snapshots.write_date(SHORTING_INVESTOR, day, investor_long([day]))
    report = verify_shorting(cfg, snapshots=snapshots)
    assert report.status == "ok"
    assert report.trade_rows == 1
    assert report.ratio_violations == 0
    assert report.balance_violations == 0
    assert report.investor_total_mismatches == 0


def test_verify_detects_ratio_violation(cfg, snapshots):
    day = date(2026, 7, 10)
    snapshots.write_date(SHORTING, day, trade_frame(day, short=2000, total=1000))
    report = verify_shorting(cfg, snapshots=snapshots)
    assert report.status == "issues"
    assert report.ratio_violations == 1


def test_verify_detects_balance_ceiling_violation(cfg, snapshots):
    day = date(2026, 7, 10)
    snapshots.write_date(SHORTING_BALANCE, day, balance_frame(day, qty=2000, listed=1000))
    report = verify_shorting(cfg, snapshots=snapshots)
    assert report.status == "issues"
    assert report.balance_violations == 1


def test_verify_detects_investor_total_mismatch(cfg, snapshots):
    day = date(2026, 7, 10)
    frame = investor_long([day]).with_columns(
        pl.when((pl.col("investor") == "total") & (pl.col("market") == "KOSPI"))
        .then(pl.lit(999))
        .otherwise(pl.col("vol_shares"))
        .alias("vol_shares")
    )
    snapshots.write_date(SHORTING_INVESTOR, day, frame)
    report = verify_shorting(cfg, snapshots=snapshots)
    assert report.status == "issues"
    assert report.investor_total_mismatches == 1


def test_verify_candle_alert_is_soft_and_null_guarded(cfg, snapshots):
    day = date(2026, 7, 10)
    trade = pl.concat(
        [
            trade_frame(day, symbol="AAA", short=5000, total=100000),
            trade_frame(day, symbol="CCC", short=10, total=1000),
        ]
    )
    snapshots.write_date(SHORTING, day, trade)
    snapshots.write_date(DAILY_CANDLES, day, daily_candle(day, "AAA", 1000.0))
    report = verify_shorting(cfg, snapshots=snapshots)
    assert report.candle_checked == 1
    assert report.candle_alerts == 1
    assert report.status == "ok"


def test_verify_null_guard_survives_no_trade_candle(cfg, snapshots):
    day = date(2026, 7, 10)
    snapshots.write_date(SHORTING, day, trade_frame(day, symbol="AAA", short=0, total=1000))
    snapshots.write_date(DAILY_CANDLES, day, daily_candle(day, "AAA", 0.0, no_trade=True))
    report = verify_shorting(cfg, snapshots=snapshots)
    assert report.candle_checked == 1
    assert report.candle_alerts == 0
    assert report.status == "ok"

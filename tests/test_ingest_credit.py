import threading
from datetime import UTC, date, datetime, timedelta

import polars as pl

from conftest import write_stock_info
from talon.data.store import CREDIT_BALANCE, CREDIT_BALANCE_1D_SCHEMA
from talon.ingest.credit import backfill_credit, daily_credit, verify_credit
from talon.sources.delisting import DELISTING_SCHEMA

FETCHED = datetime(2026, 7, 23, 6, 0, tzinfo=UTC)
SYMBOLS = ["005930", "000660"]

_NUMERIC_DEFAULTS = {
    "close": 1000.0,
    "open": 1000.0,
    "high": 1000.0,
    "low": 1000.0,
    "change_pct": 0.0,
    "volume": 100000.0,
    "loan_new_qty": 0.0,
    "loan_repay_qty": 0.0,
    "loan_balance_qty": 1000.0,
    "loan_new_amt": 0.0,
    "loan_repay_amt": 0.0,
    "loan_balance_amt": 0.0,
    "loan_balance_rate": 0.0,
    "loan_give_rate": 0.0,
    "short_new_qty": 0.0,
    "short_repay_qty": 0.0,
    "short_balance_qty": 0.0,
    "short_new_amt": 0.0,
    "short_repay_amt": 0.0,
    "short_balance_amt": 0.0,
    "short_balance_rate": 0.0,
    "short_give_rate": 0.0,
}


def make_row(day, **overrides):
    row = {"day": day, "settle_day": day + timedelta(days=2), **_NUMERIC_DEFAULTS}
    row.update(overrides)
    return row


def credit_frame(day, symbols, **overrides):
    records = [
        {**make_row(day, **overrides), "symbol": symbol, "fetched_at": FETCHED}
        for symbol in symbols
    ]
    return pl.DataFrame(records, schema=CREDIT_BALANCE_1D_SCHEMA)


def now_kst(day, hour=6):
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


def test_daily_groups_window_into_one_upsert_per_partition(cfg, snapshots, monkeypatch):
    today = date(2026, 7, 23)
    write_stock_info(snapshots, [date(2026, 7, 22)], SYMBOLS)
    d1 = date(2026, 7, 17)
    d2 = date(2026, 7, 20)

    def fetch(symbol, anchor):
        return [make_row(d1), make_row(d2)]

    calls: list[date] = []
    original = snapshots.upsert_date

    def spy(dataset, day, frame, key):
        calls.append(day)
        return original(dataset, day, frame, key)

    monkeypatch.setattr(snapshots, "upsert_date", spy)

    result = daily_credit(cfg, snapshots=snapshots, now=now_kst(today), fetch=fetch)

    assert sorted(calls) == [d1, d2]
    assert calls.count(d1) == 1
    assert calls.count(d2) == 1
    for day in (d1, d2):
        frame = snapshots.read_date(CREDIT_BALANCE, day)
        assert sorted(frame["symbol"].to_list()) == sorted(SYMBOLS)
    assert result == "2 symbols, 2/2 days, 4 rows"


def test_daily_today_absent_is_normal_t_plus_3(cfg, snapshots):
    today = date(2026, 7, 23)
    write_stock_info(snapshots, [date(2026, 7, 22)], SYMBOLS)
    available = [today - timedelta(days=n) for n in (5, 4, 3)]

    def fetch(symbol, anchor):
        return [make_row(day) for day in available]

    daily_credit(cfg, snapshots=snapshots, now=now_kst(today), fetch=fetch)

    for day in available:
        assert snapshots.has_date(CREDIT_BALANCE, day)
    for day in (today, today - timedelta(days=1), today - timedelta(days=2)):
        assert not snapshots.has_date(CREDIT_BALANCE, day)


def test_daily_skips_existing_partitions(cfg, snapshots):
    today = date(2026, 7, 23)
    write_stock_info(snapshots, [date(2026, 7, 22)], SYMBOLS)
    present = date(2026, 7, 17)
    fresh = date(2026, 7, 20)
    snapshots.write_date(CREDIT_BALANCE, present, credit_frame(present, ["005930"]))

    def fetch(symbol, anchor):
        return [make_row(present), make_row(fresh)]

    result = daily_credit(cfg, snapshots=snapshots, now=now_kst(today), fetch=fetch)

    assert snapshots.read_date(CREDIT_BALANCE, present)["symbol"].to_list() == ["005930"]
    assert sorted(snapshots.read_date(CREDIT_BALANCE, fresh)["symbol"].to_list()) == sorted(SYMBOLS)
    assert result == "2 symbols, 1/2 days, 2 rows"


def test_backfill_walks_back_by_fixed_35_day_stride(cfg, state, snapshots):
    start = date(2026, 5, 1)
    end = date(2026, 7, 20)
    cliff = date(2026, 6, 1)
    lock = threading.Lock()
    calls: dict[str, list[date]] = {}

    def fetch(symbol, anchor):
        with lock:
            calls.setdefault(symbol, []).append(anchor)
        if symbol == "005930" and anchor >= cliff:
            return [make_row(anchor)]
        return []

    summary = backfill_credit(
        cfg,
        state=state,
        snapshots=snapshots,
        start=start,
        end=end,
        symbols=["005930", "999999"],
        fetch=fetch,
    )

    assert calls["005930"] == [date(2026, 7, 20), date(2026, 6, 15), date(2026, 5, 11)]
    strides = [
        (calls["005930"][i] - calls["005930"][i + 1]).days
        for i in range(len(calls["005930"]) - 1)
    ]
    assert strides == [35, 35]
    assert calls["999999"] == [end]
    assert summary.status == "ok"
    assert summary.rows == 2
    assert snapshots.has_date(CREDIT_BALANCE, date(2026, 7, 20))
    assert snapshots.has_date(CREDIT_BALANCE, date(2026, 6, 15))
    assert not snapshots.has_date(CREDIT_BALANCE, date(2026, 5, 11))


def test_backfill_clamps_floor_to_credit_start(cfg, state, snapshots):
    calls: list[date] = []
    below_floor = date(2015, 12, 1)

    def fetch(symbol, anchor):
        calls.append(anchor)
        return [make_row(anchor), make_row(below_floor)]

    backfill_credit(
        cfg,
        state=state,
        snapshots=snapshots,
        start=date(2015, 1, 1),
        end=date(2016, 3, 1),
        symbols=["005930"],
        fetch=fetch,
    )
    assert min(calls) >= date(2016, 1, 4)
    assert all(day >= date(2016, 1, 4) for day in snapshots.dates(CREDIT_BALANCE))
    assert not snapshots.has_date(CREDIT_BALANCE, below_floor)


def _delisting_frame(rows):
    records = [
        {
            "symbol": symbol,
            "name": symbol,
            "market": "KOSPI",
            "secu_group": "주권",
            "listing_date": date(2012, 1, 1),
            "delisting_date": delisting_date,
            "reason": "",
            "arrant_end_date": None,
            "to_symbol": None,
            "classification": "terminal",
        }
        for symbol, delisting_date in rows
    ]
    return pl.DataFrame(records, schema=DELISTING_SCHEMA)


def test_backfill_universe_unions_recently_delisted_symbols(cfg, state, snapshots):
    write_stock_info(snapshots, [date(2026, 7, 22)], ["005930"])
    delisting = _delisting_frame(
        [
            ("111111", date(2023, 6, 1)),
            ("222222", date(2015, 1, 1)),
            ("333333", None),
        ]
    )
    lock = threading.Lock()
    seen: list[str] = []

    def fetch(symbol, anchor):
        with lock:
            seen.append(symbol)
        return []

    backfill_credit(
        cfg,
        state=state,
        snapshots=snapshots,
        start=date(2016, 1, 4),
        end=date(2026, 7, 20),
        delisting=delisting,
        fetch=fetch,
    )

    assert "005930" in seen
    assert "111111" in seen
    assert "222222" not in seen
    assert "333333" not in seen


def test_verify_ok_on_continuous_clean_data(cfg, snapshots):
    d1 = date(2026, 7, 17)
    d2 = date(2026, 7, 20)
    snapshots.write_date(CREDIT_BALANCE, d1, credit_frame(d1, ["005930"], loan_balance_qty=1000.0))
    snapshots.write_date(
        CREDIT_BALANCE,
        d2,
        credit_frame(d2, ["005930"], loan_balance_qty=1010.0, loan_new_qty=10.0),
    )
    report = verify_credit(cfg, snapshots=snapshots)
    assert report.status == "ok"
    assert report.rows == 2
    assert report.continuity_checked == 1
    assert report.continuity_ok == 1
    assert report.continuity_ratio == 1.0
    assert report.negative_balances == 0
    assert report.duplicate_keys == 0


def test_verify_detects_negative_balance(cfg, snapshots):
    day = date(2026, 7, 20)
    snapshots.write_date(CREDIT_BALANCE, day, credit_frame(day, ["005930"], loan_balance_qty=-5.0))
    report = verify_credit(cfg, snapshots=snapshots)
    assert report.status == "issues"
    assert report.negative_balances == 1


def test_verify_detects_settle_before_deal(cfg, snapshots):
    day = date(2026, 7, 20)
    snapshots.write_date(
        CREDIT_BALANCE, day, credit_frame(day, ["005930"], settle_day=date(2026, 7, 17))
    )
    report = verify_credit(cfg, snapshots=snapshots)
    assert report.status == "issues"
    assert report.settle_violations == 1


def test_verify_empty_when_no_data(cfg, snapshots):
    report = verify_credit(cfg, snapshots=snapshots)
    assert report.status == "empty"
    assert report.rows == 0

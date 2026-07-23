from datetime import UTC, date, datetime, timedelta

import polars as pl

from conftest import write_stock_info
from talon.data.store import (
    PROGRAM_MARKET_1D,
    PROGRAM_MARKET_1D_SCHEMA,
    PROGRAM_STOCK_1D,
    PROGRAM_STOCK_1D_SCHEMA,
)
from talon.errors import SourceError
from talon.ingest.program import (
    backfill_program_market,
    backfill_program_stock,
    daily_program_market,
    daily_program_stock,
    verify_program,
)
from talon.timeutil import KST

FETCHED = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
START = date(2026, 7, 6)
END = date(2026, 7, 10)


def market_rows(day, market):
    return pl.DataFrame(
        {
            "day": [day] * 3,
            "market": [market] * 3,
            "component": ["arb", "nonarb", "total"],
            "sell_qty": [10.0, 100.0, 110.0],
            "buy_qty": [15.0, 140.0, 155.0],
            "net_qty": [5.0, 40.0, 45.0],
            "sell_value": [1000.0, 10000.0, 11000.0],
            "buy_value": [1500.0, 14000.0, 15500.0],
            "net_value": [500.0, 4000.0, 4500.0],
            "fetched_at": [FETCHED] * 3,
        },
        schema=PROGRAM_MARKET_1D_SCHEMA,
    )


def stock_records(symbol, days):
    records = []
    for index, day in enumerate(days):
        sell_qty = 1000.0 + index
        buy_qty = 1500.0 + index
        sell_value = 10_000_000.0 + index
        buy_value = 15_000_000.0 + index
        records.append(
            {
                "day": day,
                "symbol": symbol,
                "close": 70000.0 + index,
                "change_pct": 1.0,
                "volume": 100000.0,
                "value": 7_000_000_000.0,
                "sell_qty": sell_qty,
                "buy_qty": buy_qty,
                "net_qty": buy_qty - sell_qty,
                "sell_value": sell_value,
                "buy_value": buy_value,
                "net_value": buy_value - sell_value,
            }
        )
    return records


def test_backfill_market_writes_six_rows_per_session(cfg, cal, state, snapshots):
    calls = []

    def fetch(day, market):
        calls.append((day, market))
        return market_rows(day, market)

    summary = backfill_program_market(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=START,
        end=END,
        fetch=fetch,
        sleep=lambda _: None,
    )
    sessions = cal.sessions_between(START, END)
    assert summary.status == "ok"
    assert summary.loaded == len(sessions)
    for day in sessions:
        frame = snapshots.read_date(PROGRAM_MARKET_1D, day)
        assert frame.height == 6
        assert set(frame["market"].to_list()) == {"STK", "KSQ"}
    assert len(calls) == len(sessions) * 2


def test_backfill_market_resumes_complete_partitions(cfg, cal, state, snapshots):
    def fetch(day, market):
        return market_rows(day, market)

    backfill_program_market(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END,
        fetch=fetch, sleep=lambda _: None,
    )
    calls = []

    def fetch2(day, market):
        calls.append((day, market))
        return market_rows(day, market)

    summary = backfill_program_market(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END,
        fetch=fetch2, sleep=lambda _: None,
    )
    assert calls == []
    assert summary.skipped == len(cal.sessions_between(START, END))


def test_backfill_market_aborts_after_three_failures(cfg, cal, state, snapshots):
    def fetch(day, market):
        raise SourceError("boom")

    summary = backfill_program_market(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END,
        fetch=fetch, sleep=lambda _: None,
    )
    assert summary.status == "aborted"
    assert len(summary.failed) == 3


def test_daily_market_gate_holds_today_until_18(cfg, cal, snapshots):
    today = date(2026, 7, 22)
    recent = cal.sessions_between(today - timedelta(days=21), today)[-7:]
    for day in recent:
        if day != today:
            frame = pl.concat([market_rows(day, "STK"), market_rows(day, "KSQ")])
            snapshots.write_date(PROGRAM_MARKET_1D, day, frame)

    calls = []

    def fetch(day, market):
        calls.append((day, market))
        return market_rows(day, market)

    before = datetime(2026, 7, 22, 15, 0, tzinfo=KST)
    result = daily_program_market(
        cfg, cal=cal, snapshots=snapshots, now=before, fetch=fetch, sleep=lambda _: None
    )
    assert result == "up-to-date"
    assert calls == []

    after = datetime(2026, 7, 22, 18, 30, tzinfo=KST)
    result = daily_program_market(
        cfg, cal=cal, snapshots=snapshots, now=after, fetch=fetch, sleep=lambda _: None
    )
    assert result == "1/1 days, 6 rows"
    assert calls == [(today, "STK"), (today, "KSQ")]
    assert snapshots.read_date(PROGRAM_MARKET_1D, today).height == 6


def test_daily_stock_groups_by_day_one_upsert_per_partition(
    cfg, cal, snapshots, monkeypatch
):
    today = date(2026, 7, 22)
    prev = date(2026, 7, 21)
    write_stock_info(snapshots, [today], ["005930", "000660"])

    def fetch(symbol, anchor):
        return stock_records(symbol, [today, prev])

    upserts = []
    original = snapshots.upsert_date

    def counting(dataset, day, frame, key):
        upserts.append(day)
        return original(dataset, day, frame, key)

    monkeypatch.setattr(snapshots, "upsert_date", counting)
    now = datetime(2026, 7, 22, 12, 0, tzinfo=KST)
    result = daily_program_stock(cfg, cal=cal, snapshots=snapshots, now=now, fetch=fetch)

    assert result == "2 days, 4 rows"
    assert sorted(upserts) == [prev, today]
    for day in (today, prev):
        frame = snapshots.read_date(PROGRAM_STOCK_1D, day)
        assert frame.height == 2
        assert set(frame["symbol"].to_list()) == {"005930", "000660"}
        assert frame["fetched_at"].null_count() == 0


def test_daily_stock_no_universe(cfg, cal, snapshots):
    now = datetime(2026, 7, 22, 12, 0, tzinfo=KST)
    result = daily_program_stock(
        cfg, cal=cal, snapshots=snapshots, now=now, fetch=lambda s, a: []
    )
    assert result == "no-universe"


def test_backfill_stock_walkback_stops_on_short_page(cfg, cal, state, snapshots):
    start = date(2026, 1, 2)
    end = date(2026, 7, 22)
    days30 = cal.sessions_between(date(2026, 3, 2), end)[:30]
    days5 = cal.sessions_between(date(2026, 2, 2), date(2026, 2, 20))[:5]
    pages = {"A": [stock_records("A", days30), stock_records("A", days5)]}
    counts: dict[str, int] = {}
    calls = []

    def fetch(symbol, anchor):
        calls.append((symbol, anchor))
        index = counts.get(symbol, 0)
        counts[symbol] = index + 1
        seq = pages.get(symbol, [])
        return seq[index] if index < len(seq) else []

    seed = pl.DataFrame(
        [{**stock_records("B", [start])[0], "fetched_at": FETCHED}],
        schema=PROGRAM_STOCK_1D_SCHEMA,
    )
    snapshots.upsert_date(PROGRAM_STOCK_1D, start, seed, ("symbol",))

    summary = backfill_program_stock(
        cfg, cal=cal, state=state, snapshots=snapshots, start=start, end=end,
        symbols=["A", "B"], fetch=fetch,
    )

    assert [symbol for symbol, _ in calls] == ["A", "A"]
    assert summary.skipped == 1
    assert summary.loaded == 1
    stored = snapshots.scan(PROGRAM_STOCK_1D).collect()
    assert stored.filter(pl.col("symbol") == "A").height == 35


def test_verify_market_flags_broken_identity(cfg, cal, snapshots):
    good = pl.concat([market_rows(START, "STK"), market_rows(START, "KSQ")])
    snapshots.write_date(PROGRAM_MARKET_1D, START, good)
    report = verify_program(cfg, snapshots=snapshots, parts=("market",))
    assert report.status == "ok"
    assert report.market.startswith("ok")

    broken = good.with_columns(
        pl.when((pl.col("market") == "STK") & (pl.col("component") == "total"))
        .then(pl.lit(999.0))
        .otherwise(pl.col("net_qty"))
        .alias("net_qty")
    )
    snapshots.write_date(PROGRAM_MARKET_1D, START, broken)
    report = verify_program(cfg, snapshots=snapshots, parts=("market",))
    assert report.status == "issues"
    assert "net-identity" in report.market


def test_verify_stock_flags_broken_identity(cfg, cal, snapshots):
    rows = pl.DataFrame(
        [{**r, "fetched_at": FETCHED} for r in stock_records("005930", [START])],
        schema=PROGRAM_STOCK_1D_SCHEMA,
    )
    snapshots.write_date(PROGRAM_STOCK_1D, START, rows)
    report = verify_program(cfg, snapshots=snapshots, parts=("stock",))
    assert report.status == "ok"
    assert report.stock.startswith("ok")

    broken = rows.with_columns(pl.lit(123.0).alias("net_value"))
    snapshots.write_date(PROGRAM_STOCK_1D, START, broken)
    report = verify_program(cfg, snapshots=snapshots, parts=("stock",))
    assert report.status == "issues"
    assert "net-identity" in report.stock

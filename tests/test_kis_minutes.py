import json
from datetime import UTC, date, datetime

import httpx
import polars as pl
import pytest

from conftest import write_stock_info
from talon.data.adjust import FACTOR_SCHEMA
from talon.data.store import (
    ADJUST_FACTORS,
    DAILY_CANDLES,
    DAILY_SNAPSHOT_SCHEMA,
    KIS_MINUTES,
    KIS_MINUTES_SCHEMA,
)
from talon.errors import SourceError
from talon.ingest.kis_minutes import (
    _day_symbols,
    backfill_kis_minutes,
    daily_kis_minutes,
    probe_kis_minutes,
    verify_kis_minutes,
)
from talon.sources.kis import KisClient
from talon.sources.kis_market import MINUTE_CHART_PATH, MINUTE_CHART_TR, fetch_minute_chart
from talon.timeutil import to_utc

REQUEST_DAY = date(2026, 7, 10)


def minute_bar(time_text, close, *, day="20260710", volume=100, cum=1000):
    return {
        "stck_bsop_date": day,
        "stck_cntg_hour": time_text,
        "stck_oprc": str(close - 5),
        "stck_hgpr": str(close + 10),
        "stck_lwpr": str(close - 10),
        "stck_prpr": str(close),
        "cntg_vol": str(volume),
        "acml_tr_pbmn": str(cum),
    }


def make_minute_client(tmp_path, pages_by_anchor, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == MINUTE_CHART_PATH
        assert request.headers["tr_id"] == MINUTE_CHART_TR
        params = dict(request.url.params)
        calls.append(params)
        anchor = params["FID_INPUT_HOUR_1"]
        payload = pages_by_anchor.get(anchor, {"rt_cd": "0", "output2": []})
        return httpx.Response(200, json=payload)

    token_path = tmp_path / "kis_token.json"
    token_path.write_text(json.dumps({"access_token": "tok", "expired_at": "2099-01-01 00:00:00"}))
    return KisClient(
        "key",
        "secret",
        base_url="https://kis.test",
        token_path=token_path,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def test_fetch_minute_chart_maps_and_sorts_ascending(tmp_path):
    payload = {
        "rt_cd": "0",
        "output1": {"ignored": "meta"},
        "output2": [
            minute_bar("153000", 70000),
            minute_bar("152900", 69900),
            minute_bar("152800", 69800),
            minute_bar("152700", 111, day="20260709"),
        ],
    }
    calls: list[dict] = []
    with make_minute_client(tmp_path, {"153000": payload}, calls) as client:
        rows = fetch_minute_chart(client, "005930", REQUEST_DAY, anchor="153000")

    params = calls[0]
    assert params["FID_COND_MRKT_DIV_CODE"] == "J"
    assert params["FID_INPUT_ISCD"] == "005930"
    assert params["FID_INPUT_DATE_1"] == "20260710"
    assert params["FID_INPUT_HOUR_1"] == "153000"
    assert params["FID_PW_DATA_INCU_YN"] == "N"
    assert params["FID_FAKE_TICK_INCU_YN"] == ""

    assert [row["time"] for row in rows] == ["152800", "152900", "153000"]
    assert rows[-1]["open"] == 69995.0
    assert rows[-1]["high"] == 70010.0
    assert rows[-1]["low"] == 69990.0
    assert rows[-1]["close"] == 70000.0
    assert rows[-1]["volume"] == 100.0
    assert rows[-1]["cum_value"] == 1000.0


def test_fetch_minute_chart_empty_output_returns_empty(tmp_path):
    calls: list[dict] = []
    with make_minute_client(tmp_path, {"153000": {"rt_cd": "0", "output2": []}}, calls) as client:
        assert fetch_minute_chart(client, "005930", REQUEST_DAY, anchor="153000") == []


def test_fetch_minute_chart_paginates_and_dedups(tmp_path):
    pages = {
        "153000": {
            "rt_cd": "0",
            "output2": [minute_bar("153000", 70000), minute_bar("152900", 69900)],
        },
        "152800": {
            "rt_cd": "0",
            "output2": [
                minute_bar("152900", 69900),
                minute_bar("152800", 69800),
                minute_bar("152700", 69700),
            ],
        },
    }
    calls: list[dict] = []
    with make_minute_client(tmp_path, pages, calls) as client:
        rows = fetch_minute_chart(client, "005930", REQUEST_DAY, anchor="153000", max_pages=3)

    assert calls[1]["FID_INPUT_HOUR_1"] == "152800"
    assert [row["time"] for row in rows] == ["152700", "152800", "152900", "153000"]
    assert len(calls) == 3


def test_fetch_minute_chart_cursor_crosses_hour_boundary(tmp_path):
    pages = {"140000": {"rt_cd": "0", "output2": [minute_bar("140000", 70000)]}}
    calls: list[dict] = []
    with make_minute_client(tmp_path, pages, calls) as client:
        fetch_minute_chart(client, "005930", REQUEST_DAY, anchor="140000", max_pages=2)

    assert calls[1]["FID_INPUT_HOUR_1"] == "135900"


def test_fetch_minute_chart_respects_max_pages(tmp_path):
    pages = {
        "153000": {
            "rt_cd": "0",
            "output2": [minute_bar("153000", 70000), minute_bar("152900", 69900)],
        },
        "152800": {"rt_cd": "0", "output2": [minute_bar("152800", 69800)]},
    }
    calls: list[dict] = []
    with make_minute_client(tmp_path, pages, calls) as client:
        rows = fetch_minute_chart(client, "005930", REQUEST_DAY, anchor="153000", max_pages=1)

    assert len(calls) == 1
    assert [row["time"] for row in rows] == ["152900", "153000"]


def test_fetch_minute_chart_stops_on_all_seen_followup(tmp_path):
    pages = {
        "153000": {
            "rt_cd": "0",
            "output2": [minute_bar("153000", 70000), minute_bar("152900", 69900)],
        },
        "152800": {
            "rt_cd": "0",
            "output2": [minute_bar("153000", 70000), minute_bar("152900", 69900)],
        },
    }
    calls: list[dict] = []
    with make_minute_client(tmp_path, pages, calls) as client:
        rows = fetch_minute_chart(client, "005930", REQUEST_DAY, anchor="153000", max_pages=5)

    assert len(calls) == 2
    assert [row["time"] for row in rows] == ["152900", "153000"]


def test_fetch_minute_chart_stops_when_oldest_not_decreasing(tmp_path):
    pages = {
        "153000": {
            "rt_cd": "0",
            "output2": [minute_bar("153000", 70000), minute_bar("152900", 69900)],
        },
        "152800": {
            "rt_cd": "0",
            "output2": [minute_bar("153100", 70100)],
        },
    }
    calls: list[dict] = []
    with make_minute_client(tmp_path, pages, calls) as client:
        rows = fetch_minute_chart(client, "005930", REQUEST_DAY, anchor="153000", max_pages=5)

    assert len(calls) == 2
    assert [row["time"] for row in rows] == ["152900", "153000", "153100"]


def bars(times):
    return [
        {
            "time": text,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 10.0,
            "cum_value": 1000.0,
        }
        for text in times
    ]


START = date(2026, 7, 6)
END = date(2026, 7, 10)
SYMBOLS = ["005930", "000660"]


def test_backfill_loads_all_sessions_oldest_first(cfg, cal, state, snapshots):
    sessions = cal.sessions_between(START, END)
    write_stock_info(snapshots, sessions, SYMBOLS)
    seen: list[date] = []

    def fetch(symbol, day, anchor):
        seen.append(day)
        return bars(["153000"])

    summary = backfill_kis_minutes(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END, fetch=fetch
    )
    assert summary.status == "ok"
    assert summary.sessions == 5
    assert summary.loaded == 5
    assert summary.rows == 10
    assert summary.skipped == 0
    assert seen[:2] == [sessions[0], sessions[0]]
    seen_days = sorted(set(seen))
    assert seen_days == sessions
    assert snapshots.has_date(KIS_MINUTES, sessions[0])
    assert state.recent_runs("kis-minutes-backfill")[0].ok is True


def test_backfill_skips_existing_and_force_recollects(cfg, cal, state, snapshots):
    sessions = cal.sessions_between(START, END)
    write_stock_info(snapshots, sessions, SYMBOLS)
    existing = _minutes_frame(sessions[1], "005930", [("153000", 100.0)])
    snapshots.write_date(KIS_MINUTES, sessions[1], existing)

    def fetch(symbol, day, anchor):
        return bars(["153000"])

    summary = backfill_kis_minutes(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END, fetch=fetch
    )
    assert summary.skipped == 1
    assert summary.loaded == 4

    forced = backfill_kis_minutes(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=START,
        end=END,
        fetch=fetch,
        force=True,
    )
    assert forced.skipped == 0
    assert forced.loaded == 5


def test_backfill_aborts_after_three_consecutive_failures(cfg, cal, state, snapshots):
    sessions = cal.sessions_between(START, END)
    write_stock_info(snapshots, sessions, SYMBOLS)
    seen: list[date] = []

    def fetch(symbol, day, anchor):
        seen.append(day)
        raise SourceError("boom")

    summary = backfill_kis_minutes(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END, fetch=fetch
    )
    assert summary.status == "aborted"
    assert len(summary.failed) == 3
    assert sorted(set(seen)) == sessions[:3]


def test_backfill_treats_zero_rows_as_failure(cfg, cal, state, snapshots):
    sessions = cal.sessions_between(START, END)
    write_stock_info(snapshots, sessions, SYMBOLS)

    def fetch(symbol, day, anchor):
        if day == sessions[2]:
            return []
        return bars(["153000"])

    summary = backfill_kis_minutes(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END, fetch=fetch
    )
    assert summary.status == "partial"
    assert summary.loaded == 4
    assert summary.failed == [sessions[2].isoformat()]


def test_backfill_streak_resets_on_success(cfg, cal, state, snapshots):
    sessions = cal.sessions_between(START, END)
    write_stock_info(snapshots, sessions, SYMBOLS)
    good = {sessions[2]}

    def fetch(symbol, day, anchor):
        if day in good:
            return bars(["153000"])
        raise SourceError("boom")

    summary = backfill_kis_minutes(
        cfg, cal=cal, state=state, snapshots=snapshots, start=START, end=END, fetch=fetch
    )
    assert summary.status == "partial"
    assert summary.loaded == 1
    assert len(summary.failed) == 4


def test_backfill_pauses_inside_live_window(cfg, cal, state, snapshots):
    day = date(2026, 7, 16)
    write_stock_info(snapshots, [day], SYMBOLS)
    slept: list[float] = []

    def fetch(symbol, d, anchor):
        return bars(["153000"])

    def now():
        return datetime(2026, 7, 16, 14 - 9, 55, tzinfo=UTC)

    summary = backfill_kis_minutes(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=day,
        end=day,
        fetch=fetch,
        now=now,
        sleep=slept.append,
    )
    assert summary.status == "ok"
    assert slept == [55 * 60]


def test_backfill_skips_pause_on_non_trading_day(cfg, cal, state, snapshots):
    day = date(2026, 7, 16)
    write_stock_info(snapshots, [day], SYMBOLS)
    slept: list[float] = []

    def fetch(symbol, d, anchor):
        return bars(["153000"])

    def now():
        return datetime(2026, 7, 18, 14 - 9, 55, tzinfo=UTC)

    summary = backfill_kis_minutes(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=day,
        end=day,
        fetch=fetch,
        now=now,
        sleep=slept.append,
    )
    assert summary.status == "ok"
    assert slept == []


def test_backfill_default_start_uses_probe_cliff(cfg, cal, state, snapshots):
    cliff = date(2026, 7, 13)
    end = date(2026, 7, 16)
    write_stock_info(snapshots, cal.sessions_between(cliff, end), SYMBOLS)

    def fetch(symbol, day, anchor):
        return bars(["153000"]) if day >= cliff else []

    def now():
        return datetime(2026, 7, 17, 3, 0, tzinfo=UTC)

    summary = backfill_kis_minutes(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=None,
        end=end,
        fetch=fetch,
        now=now,
    )
    assert summary.status == "ok"
    assert summary.sessions == 4
    assert summary.loaded == 4


def kst(hour, minute=0):
    return datetime(2026, 7, 16, hour - 9, minute, tzinfo=UTC)


def test_daily_up_to_date(cfg, cal, snapshots):
    for day in cal.sessions_between(date(2026, 7, 1), date(2026, 7, 16)):
        snapshots.write_date(KIS_MINUTES, day, _minutes_frame(day, "005930", [("153000", 100.0)]))
    result = daily_kis_minutes(cfg, cal=cal, snapshots=snapshots, now=kst(18, 0), fetch=None)
    assert result == "up-to-date"


def test_daily_gates_today_before_ready(cfg, cal, snapshots):
    write_stock_info(snapshots, cal.sessions_between(date(2026, 7, 8), date(2026, 7, 16)), SYMBOLS)
    for day in cal.sessions_between(date(2026, 7, 8), date(2026, 7, 15)):
        snapshots.write_date(KIS_MINUTES, day, _minutes_frame(day, "005930", [("153000", 100.0)]))
    calls: list[date] = []

    def fetch(symbol, day, anchor):
        calls.append(day)
        return bars(["153000"])

    result = daily_kis_minutes(cfg, cal=cal, snapshots=snapshots, now=kst(15, 0), fetch=fetch)
    assert date(2026, 7, 16) not in calls
    assert result == "up-to-date"

    calls.clear()
    result = daily_kis_minutes(cfg, cal=cal, snapshots=snapshots, now=kst(16, 30), fetch=fetch)
    assert date(2026, 7, 16) in calls
    assert result == "1/1 days, 2 rows"


def test_daily_gate_boundary_at_ready_time(cfg, cal, snapshots):
    write_stock_info(snapshots, cal.sessions_between(date(2026, 7, 8), date(2026, 7, 16)), SYMBOLS)
    for day in cal.sessions_between(date(2026, 7, 8), date(2026, 7, 15)):
        snapshots.write_date(KIS_MINUTES, day, _minutes_frame(day, "005930", [("153000", 100.0)]))
    calls: list[date] = []

    def fetch(symbol, day, anchor):
        calls.append(day)
        return bars(["153000"])

    result = daily_kis_minutes(cfg, cal=cal, snapshots=snapshots, now=kst(15, 59), fetch=fetch)
    assert date(2026, 7, 16) not in calls
    assert result == "up-to-date"

    calls.clear()
    result = daily_kis_minutes(cfg, cal=cal, snapshots=snapshots, now=kst(16, 0), fetch=fetch)
    assert date(2026, 7, 16) in calls
    assert result == "1/1 days, 2 rows"


def test_daily_counts_zero_row_errors(cfg, cal, snapshots):
    write_stock_info(snapshots, cal.sessions_between(date(2026, 7, 8), date(2026, 7, 16)), SYMBOLS)
    for day in cal.sessions_between(date(2026, 7, 8), date(2026, 7, 15)):
        snapshots.write_date(KIS_MINUTES, day, _minutes_frame(day, "005930", [("153000", 100.0)]))

    def fetch(symbol, day, anchor):
        return []

    result = daily_kis_minutes(cfg, cal=cal, snapshots=snapshots, now=kst(16, 30), fetch=fetch)
    assert result == "0/1 days, 0 rows, errors: 1"
    assert not snapshots.has_date(KIS_MINUTES, date(2026, 7, 16))


def test_probe_bisects_to_cliff_within_log_calls(cfg, cal):
    cliff = date(2026, 6, 15)

    def fetch(symbol, day, anchor):
        return bars(["153000"]) if day >= cliff else []

    def now():
        return datetime(2026, 7, 17, 3, 0, tzinfo=UTC)

    report = probe_kis_minutes(cfg, cal=cal, fetch=fetch, now=now())
    assert report.status == "ok"
    assert report.cliff == cliff
    assert report.calls <= 12


def test_probe_reports_no_data_when_all_empty(cfg, cal):
    def fetch(symbol, day, anchor):
        return []

    def now():
        return datetime(2026, 7, 17, 3, 0, tzinfo=UTC)

    report = probe_kis_minutes(cfg, cal=cal, fetch=fetch, now=now())
    assert report.status == "no-data"
    assert report.cliff is None


def test_probe_single_day_mode(cfg, cal):
    def fetch(symbol, day, anchor):
        assert anchor == "150000"
        return bars(["145900", "150000"])

    report = probe_kis_minutes(cfg, cal=cal, day=date(2026, 7, 10), anchor="150000", fetch=fetch)
    assert report.status == "ok"
    assert report.day == date(2026, 7, 10)
    assert report.anchor == "150000"
    assert report.rows == 2
    assert report.first_ts == to_utc(datetime(2026, 7, 10, 14, 59))
    assert report.last_ts == to_utc(datetime(2026, 7, 10, 15, 0))


def _minutes_frame(day, symbol, entries, *, extra_rows=None):
    rows = []
    for time_text, close in entries:
        ts = to_utc(
            datetime.combine(
                day,
                datetime.min.time().replace(
                    hour=int(time_text[:2]), minute=int(time_text[2:4]), second=int(time_text[4:6])
                ),
            )
        )
        rows.append(
            {
                "day": day,
                "symbol": symbol,
                "ts": ts,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 10.0,
                "cum_value": 1000.0,
                "fetched_at": datetime(2026, 7, 16, tzinfo=UTC),
            }
        )
    rows.extend(extra_rows or [])
    return pl.DataFrame(rows, schema=KIS_MINUTES_SCHEMA)


def test_verify_detects_planted_defects(cfg, cal, snapshots):
    day = date(2026, 7, 10)
    open_ts = to_utc(datetime(2026, 7, 10, 9, 0))
    before_open = to_utc(datetime(2026, 7, 10, 8, 0))
    defects = [
        {
            "day": day,
            "symbol": "005930",
            "ts": open_ts,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 10.0,
            "cum_value": 1.0,
            "fetched_at": datetime(2026, 7, 16, tzinfo=UTC),
        },
        {
            "day": day,
            "symbol": "005930",
            "ts": open_ts,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 10.0,
            "cum_value": 1.0,
            "fetched_at": datetime(2026, 7, 16, tzinfo=UTC),
        },
        {
            "day": day,
            "symbol": "005930",
            "ts": to_utc(datetime(2026, 7, 10, 9, 2)),
            "open": 50.0,
            "high": 60.0,
            "low": 100.0,
            "close": 40.0,
            "volume": 10.0,
            "cum_value": 1.0,
            "fetched_at": datetime(2026, 7, 16, tzinfo=UTC),
        },
        {
            "day": day,
            "symbol": "005930",
            "ts": before_open,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 10.0,
            "cum_value": 1.0,
            "fetched_at": datetime(2026, 7, 16, tzinfo=UTC),
        },
    ]
    frame = _minutes_frame(day, "005930", [("153000", 70000.0)], extra_rows=defects)
    snapshots.write_date(KIS_MINUTES, day, frame)
    snapshots.write_date(DAILY_CANDLES, day, _daily_frame(day, "005930", 70500.0))

    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots)
    assert report.status == "issues"
    assert report.duplicate_keys == 1
    assert report.ohlc_violations == 1
    assert report.out_of_session == 1
    assert report.crosscheck_mismatches == 1
    assert report.crosscheck_symbols == 1
    assert any("005930" in example for example in report.examples)


def test_verify_ok_on_clean_data(cfg, cal, snapshots):
    day = date(2026, 7, 10)
    snapshots.write_date(KIS_MINUTES, day, _minutes_frame(day, "005930", [("153000", 70500.0)]))
    snapshots.write_date(DAILY_CANDLES, day, _daily_frame(day, "005930", 70500.0))
    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots)
    assert report.status == "ok"
    assert report.rows == 1
    assert report.crosscheck_mismatches == 0


def test_verify_csat_shift_admits_late_close_bar(cfg, cal, snapshots):
    day = date(2025, 11, 13)
    frame = _minutes_frame(day, "005930", [("163000", 100.0), ("171000", 100.0)])
    snapshots.write_date(KIS_MINUTES, day, frame)

    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots)
    assert report.out_of_session == 1


def test_verify_crosscheck_csat_shift_uses_late_close_bar(cfg, cal, snapshots):
    day = date(2025, 11, 13)
    matching = _minutes_frame(day, "005930", [("100000", 70500.0), ("163000", 70500.0)])
    diverging = _minutes_frame(day, "000660", [("100000", 70000.0), ("163000", 70000.0)])
    snapshots.write_date(KIS_MINUTES, day, pl.concat([matching, diverging]))
    snapshots.write_date(
        DAILY_CANDLES,
        day,
        pl.concat([_daily_frame(day, "005930", 70500.0), _daily_frame(day, "000660", 70500.0)]),
    )

    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots)
    assert report.crosscheck_symbols == 2
    assert report.crosscheck_mismatches == 1
    assert any("000660" in example for example in report.examples)


def _daily_frame(day, symbol, close):
    return pl.DataFrame(
        {
            "day": [day],
            "symbol": [symbol],
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "volume": [1000.0],
            "value": [1e9],
            "change_pct": [0.0],
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )


def _factor_frame(days_factors):
    return pl.DataFrame(
        {"day": [d for d, _ in days_factors], "factor": [f for _, f in days_factors]},
        schema=FACTOR_SCHEMA,
    )


def _quirk_row(day, symbol, ts, *, open, high, low, close):
    return {
        "day": day,
        "symbol": symbol,
        "ts": ts,
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": 10.0,
        "cum_value": 1.0,
        "fetched_at": datetime(2026, 7, 16, tzinfo=UTC),
    }


def test_verify_admits_close_vi_extension_bar(cfg, cal, snapshots):
    day = date(2026, 7, 10)
    frame = _minutes_frame(day, "005930", [("153200", 100.0), ("153300", 100.0)])
    snapshots.write_date(KIS_MINUTES, day, frame)

    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots)
    assert report.out_of_session == 1


def test_verify_crosscheck_accepts_close_vi_extension_bar(cfg, cal, snapshots):
    day = date(2026, 7, 10)
    frame = _minutes_frame(day, "005930", [("100000", 70500.0), ("153200", 70500.0)])
    snapshots.write_date(KIS_MINUTES, day, frame)
    snapshots.write_date(DAILY_CANDLES, day, _daily_frame(day, "005930", 70500.0))

    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots)
    assert report.out_of_session == 0
    assert report.crosscheck_symbols == 1
    assert report.crosscheck_mismatches == 0


def test_verify_crosscheck_restated_symbol_is_clean(cfg, cal, snapshots, series):
    day = date(2026, 7, 10)
    snapshots.write_date(KIS_MINUTES, day, _minutes_frame(day, "005930", [("153000", 1000.0)]))
    snapshots.write_date(DAILY_CANDLES, day, _daily_frame(day, "005930", 500.0))
    series.replace(ADJUST_FACTORS, "005930", _factor_frame([(day, 0.5)]))

    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots, series=series)
    assert report.crosscheck_symbols == 1
    assert report.crosscheck_mismatches == 0


def test_verify_crosscheck_restated_symbol_still_flags_divergence(cfg, cal, snapshots, series):
    day = date(2026, 7, 10)
    snapshots.write_date(KIS_MINUTES, day, _minutes_frame(day, "000660", [("153000", 1000.0)]))
    snapshots.write_date(DAILY_CANDLES, day, _daily_frame(day, "000660", 600.0))
    series.replace(ADJUST_FACTORS, "000660", _factor_frame([(day, 0.5)]))

    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots, series=series)
    assert report.crosscheck_mismatches == 1
    assert any("factor" in example for example in report.examples)


def test_verify_crosscheck_factor_defaults_without_adjust_file(cfg, cal, snapshots, series):
    day = date(2026, 7, 10)
    snapshots.write_date(KIS_MINUTES, day, _minutes_frame(day, "005930", [("153000", 70500.0)]))
    snapshots.write_date(DAILY_CANDLES, day, _daily_frame(day, "005930", 70500.0))

    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots, series=series)
    assert report.crosscheck_mismatches == 0


def test_day_symbols_falls_back_to_daily_candles(snapshots):
    day = date(2026, 7, 10)
    snapshots.write_date(DAILY_CANDLES, day, _daily_frame(day, "005930", 70000.0))
    assert _day_symbols(snapshots, day) == ["005930"]


def test_day_symbols_errors_when_stock_info_and_daily_absent(snapshots):
    with pytest.raises(SourceError, match="stock_info"):
        _day_symbols(snapshots, date(2026, 7, 10))


def test_verify_open_only_outlier_is_informational(cfg, cal, snapshots):
    day = date(2026, 7, 10)
    quirk = _quirk_row(
        day,
        "005930",
        to_utc(datetime(2026, 7, 10, 9, 0)),
        open=80.0,
        high=105.0,
        low=100.0,
        close=102.0,
    )
    frame = _minutes_frame(day, "005930", [("153000", 70500.0)], extra_rows=[quirk])
    snapshots.write_date(KIS_MINUTES, day, frame)

    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots)
    assert report.open_outliers == 1
    assert report.ohlc_violations == 0
    assert report.status == "ok"


def test_verify_hlc_violation_flips_status(cfg, cal, snapshots):
    day = date(2026, 7, 10)
    bad = _quirk_row(
        day,
        "005930",
        to_utc(datetime(2026, 7, 10, 9, 2)),
        open=106.0,
        high=110.0,
        low=105.0,
        close=100.0,
    )
    frame = _minutes_frame(day, "005930", [("153000", 70500.0)], extra_rows=[bad])
    snapshots.write_date(KIS_MINUTES, day, frame)

    report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots)
    assert report.ohlc_violations == 1
    assert report.open_outliers == 0
    assert report.status == "issues"


@pytest.mark.parametrize("empty_dataset", [True, False])
def test_verify_ok_when_nothing_to_check(cfg, cal, snapshots, empty_dataset):
    if not empty_dataset:
        day = date(2026, 7, 10)
        snapshots.write_date(KIS_MINUTES, day, _minutes_frame(day, "005930", [("153000", 70500.0)]))
        report = verify_kis_minutes(
            cfg,
            cal=cal,
            snapshots=snapshots,
            start=date(2020, 1, 1),
            end=date(2020, 1, 2),
        )
    else:
        report = verify_kis_minutes(cfg, cal=cal, snapshots=snapshots)
    assert report.status == "ok"
    assert report.rows == 0

from datetime import date

import polars as pl
import pytest

from conftest import write_stock_info
from talon.data.store import (
    BREADTH_INTRADAY,
    DART_POLL,
    INDEX_INTRADAY,
    MACRO_INTRADAY,
)
from talon.errors import SourceError
from talon.ingest.pulse import collect_pulse
from talon.sources.dart import DART_FILINGS_SCHEMA
from talon.sources.investing import VkospiQuote
from talon.sources.krx_index import INDEX_SNAPSHOT_SCHEMA
from talon.sources.yahoo import YahooQuote

DAY = date(2026, 7, 14)
SLOT = "15:10"


def index_frame(day, market):
    return pl.DataFrame(
        {
            "day": [day] * 2,
            "market": [market] * 2,
            "name": [f"{market}", f"{market}200"],
            "open": [100.0, 200.0],
            "high": [110.0, 210.0],
            "low": [90.0, 190.0],
            "close": [105.0, 205.0],
            "volume": [1000.0, 2000.0],
            "value": [1e12, 2e12],
            "cap": [1e15, 2e15],
        },
        schema=INDEX_SNAPSHOT_SCHEMA,
    )


def stock_frame(symbols_up=3, symbols_down=2, symbols_flat=1):
    changes = [1.5] * symbols_up + [-2.0] * symbols_down + [0.0] * symbols_flat
    total = len(changes)
    return pl.DataFrame(
        {
            "day": [DAY] * total,
            "symbol": [f"{i:06d}" for i in range(total)],
            "open": [100.0] * total,
            "high": [110.0] * total,
            "low": [90.0] * total,
            "close": [105.0] * total,
            "volume": [1000.0] * total,
            "value": [1e9] * total,
            "change_pct": changes,
        }
    )


def dart_frame(count=2):
    return pl.DataFrame(
        {
            "day": [DAY] * count,
            "symbol": [f"{i:06d}" for i in range(count)],
            "corp_code": [f"c{i}" for i in range(count)],
            "corp_name": [f"회사{i}" for i in range(count)],
            "corp_cls": ["Y"] * count,
            "filing_type": ["A"] * count,
            "report_nm": ["유상증자결정"] * count,
            "rcept_no": [f"2026071400000{i}" for i in range(count)],
        },
        schema=DART_FILINGS_SCHEMA,
    )


@pytest.fixture
def pulse_cfg(cfg):
    cfg.krx_id = "id"
    cfg.krx_password = "pw"
    cfg.dart_api_key = "key"
    return cfg


@pytest.fixture(autouse=True)
def fake_sources(monkeypatch):
    monkeypatch.setattr(
        "talon.ingest.pulse.fetch_index_snapshot",
        lambda day, market, **kw: index_frame(day, market),
    )
    monkeypatch.setattr(
        "talon.ingest.pulse.fetch_quote", lambda symbol, **kw: YahooQuote(100.0, 99.0)
    )
    monkeypatch.setattr("talon.ingest.pulse.fetch_filings", lambda key, day, **kw: dart_frame())
    monkeypatch.setattr("talon.ingest.pulse.fetch_vkospi", lambda **kw: VkospiQuote(32.15, 30.9))


def run(cfg, snapshots, frame=None):
    return collect_pulse(cfg, snapshots=snapshots, slot=SLOT, day=DAY, stock_frame=frame)


def test_collects_every_part(pulse_cfg, snapshots):
    summary = run(pulse_cfg, snapshots, stock_frame())

    assert summary.parts == {
        "index": "ok",
        "macro": "ok",
        "vkospi": "ok",
        "breadth": "ok",
        "dart": "ok",
    }
    stored = snapshots.read_date(INDEX_INTRADAY, DAY)
    assert stored.height == 4
    assert set(stored["market"].unique().to_list()) == {"KOSPI", "KOSDAQ"}
    assert stored["slot"].unique().to_list() == [SLOT]
    assert stored["captured_at"].null_count() == 0


def test_macro_records_all_series(pulse_cfg, snapshots):
    run(pulse_cfg, snapshots, stock_frame())

    stored = snapshots.read_date(MACRO_INTRADAY, DAY)
    assert sorted(stored["series"].to_list()) == ["ES_F", "NQ_F", "USDKRW", "VKOSPI"]
    by_series = {row["series"]: row for row in stored.to_dicts()}
    assert by_series["VKOSPI"]["source"] == "investing"
    assert by_series["VKOSPI"]["price"] == 32.15
    assert by_series["USDKRW"]["source"] == "yahoo"


def test_macro_partial_failure_keeps_the_rest(monkeypatch, pulse_cfg, snapshots):
    def flaky(symbol, **kw):
        if symbol == "ES=F":
            raise SourceError("cme down")
        return YahooQuote(100.0, None)

    monkeypatch.setattr("talon.ingest.pulse.fetch_quote", flaky)

    summary = run(pulse_cfg, snapshots, stock_frame())

    assert summary.parts["macro"].startswith("partial")
    stored = snapshots.read_date(MACRO_INTRADAY, DAY)
    assert sorted(stored["series"].to_list()) == ["NQ_F", "USDKRW", "VKOSPI"]


def test_vkospi_failure_leaves_yahoo_macro_intact(monkeypatch, pulse_cfg, snapshots):
    def boom(**kw):
        raise SourceError("cloudflare challenge")

    monkeypatch.setattr("talon.ingest.pulse.fetch_vkospi", boom)

    summary = run(pulse_cfg, snapshots, stock_frame())

    assert summary.parts["vkospi"].startswith("error")
    assert summary.parts["macro"] == "ok"
    stored = snapshots.read_date(MACRO_INTRADAY, DAY)
    assert sorted(stored["series"].to_list()) == ["ES_F", "NQ_F", "USDKRW"]


def test_breadth_counts_and_market_split(pulse_cfg, snapshots):
    write_stock_info(snapshots, [DAY], [f"{i:06d}" for i in range(4)], market="KOSPI")

    run(pulse_cfg, snapshots, stock_frame(symbols_up=3, symbols_down=2, symbols_flat=1))

    stored = snapshots.read_date(BREADTH_INTRADAY, DAY)
    by_market = {row["market"]: row for row in stored.to_dicts()}
    assert by_market["ALL"]["advancing"] == 3
    assert by_market["ALL"]["declining"] == 2
    assert by_market["ALL"]["unchanged"] == 1
    assert by_market["ALL"]["total"] == 6
    assert by_market["KOSPI"]["total"] == 4


def test_breadth_skipped_without_snapshot(pulse_cfg, snapshots):
    summary = run(pulse_cfg, snapshots, None)

    assert summary.parts["breadth"] == "skipped-no-snapshot"
    assert snapshots.read_date(BREADTH_INTRADAY, DAY) is None


def test_dart_poll_stamps_polled_at(pulse_cfg, snapshots):
    run(pulse_cfg, snapshots, stock_frame())

    stored = snapshots.read_date(DART_POLL, DAY)
    assert stored.height == 2
    assert stored["slot"].unique().to_list() == [SLOT]
    assert stored["polled_at"].null_count() == 0
    assert stored["report_nm"].unique().to_list() == ["유상증자결정"]


def test_dart_skipped_without_key(pulse_cfg, snapshots):
    pulse_cfg.dart_api_key = ""

    summary = run(pulse_cfg, snapshots, stock_frame())

    assert summary.parts["dart"] == "skipped-no-key"


def test_index_skipped_without_credentials(pulse_cfg, snapshots):
    pulse_cfg.krx_id = ""
    pulse_cfg.krx_password = ""

    summary = run(pulse_cfg, snapshots, stock_frame())

    assert summary.parts["index"] == "skipped-no-credentials"
    assert snapshots.read_date(INDEX_INTRADAY, DAY) is None


def test_one_part_failing_does_not_stop_the_others(monkeypatch, pulse_cfg, snapshots):
    def boom(day, market, **kw):
        raise SourceError("krx blocked")

    monkeypatch.setattr("talon.ingest.pulse.fetch_index_snapshot", boom)

    summary = run(pulse_cfg, snapshots, stock_frame())

    assert summary.parts["index"].startswith("error")
    assert summary.parts["macro"] == "ok"
    assert summary.parts["dart"] == "ok"
    assert snapshots.read_date(MACRO_INTRADAY, DAY) is not None


def test_reruns_overwrite_the_same_slot(pulse_cfg, snapshots):
    run(pulse_cfg, snapshots, stock_frame())
    run(pulse_cfg, snapshots, stock_frame())

    stored = snapshots.read_date(INDEX_INTRADAY, DAY)
    assert stored.height == 4
    macro = snapshots.read_date(MACRO_INTRADAY, DAY)
    assert macro.height == 4

from datetime import date

import polars as pl

from conftest import stock_info_frame
from talon.data.store import STOCK_INFO, STOCK_INFO_SCHEMA
from talon.errors import SourceError
from talon.ingest.stockinfo import backfill_stock_info

WED = date(2026, 7, 8)
THU = date(2026, 7, 9)
FRI = date(2026, 7, 10)


class FakeKrx:
    def __init__(self, *, missing: set[date] | None = None, broken: set[date] | None = None):
        self.missing = missing or set()
        self.broken = broken or set()
        self.asked: list[date] = []
        self.closed = False

    def stock_info(self, day):
        self.asked.append(day)
        if day in self.broken:
            raise SourceError("krx down")
        if day in self.missing:
            return pl.DataFrame(schema=STOCK_INFO_SCHEMA)
        return stock_info_frame(day, ["005930", "035720"])

    def close(self):
        self.closed = True


def run(cfg, cal, state, snapshots, source, *, start=THU, end=FRI, force=False):
    return backfill_stock_info(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        start=start,
        end=end,
        source=source,
        force=force,
    )


def test_writes_a_snapshot_per_session(cfg, cal, state, snapshots):
    source = FakeKrx()
    summary = run(cfg, cal, state, snapshots, source, start=WED, end=FRI)

    assert summary.status == "ok"
    assert summary.loaded == 3
    assert source.asked == [WED, THU, FRI]
    assert snapshots.dates(STOCK_INFO) == [WED, THU, FRI]
    assert snapshots.read_date(STOCK_INFO, THU).get_column("symbol").to_list() == [
        "005930",
        "035720",
    ]


def test_skips_days_already_stored(cfg, cal, state, snapshots):
    snapshots.write_date(STOCK_INFO, THU, stock_info_frame(THU, ["005930"]))
    source = FakeKrx()
    summary = run(cfg, cal, state, snapshots, source)

    assert summary.skipped == 1
    assert summary.loaded == 1
    assert source.asked == [FRI]


def test_force_refetches_stored_days(cfg, cal, state, snapshots):
    snapshots.write_date(STOCK_INFO, THU, stock_info_frame(THU, ["005930"]))
    source = FakeKrx()
    summary = run(cfg, cal, state, snapshots, source, force=True)

    assert summary.skipped == 0
    assert source.asked == [THU, FRI]
    assert snapshots.read_date(STOCK_INFO, THU).height == 2


def test_empty_day_is_not_written_and_not_a_failure(cfg, cal, state, snapshots):
    source = FakeKrx(missing={THU})
    summary = run(cfg, cal, state, snapshots, source)

    assert summary.status == "ok"
    assert summary.loaded == 1
    assert summary.failed == []
    assert not snapshots.has_date(STOCK_INFO, THU)


def test_failed_day_is_reported_and_the_rest_still_load(cfg, cal, state, snapshots):
    source = FakeKrx(broken={THU})
    summary = run(cfg, cal, state, snapshots, source)

    assert summary.status == "partial"
    assert summary.failed == [THU.isoformat()]
    assert summary.loaded == 1
    assert snapshots.has_date(STOCK_INFO, FRI)


def test_does_not_close_a_source_it_did_not_open(cfg, cal, state, snapshots):
    source = FakeKrx()
    run(cfg, cal, state, snapshots, source)
    assert source.closed is False

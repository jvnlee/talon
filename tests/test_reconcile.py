from datetime import date

import polars as pl
import pytest

from talon.data.store import (
    DAILY_CANDLES,
    DAILY_SNAPSHOT_SCHEMA,
    MARKET_CAP,
    MARKET_CAP_SCHEMA,
    STOCK_INFO,
    STOCK_INFO_SCHEMA,
)
from talon.errors import SourceError
from talon.ingest.reconcile import apply_official, reconcile_daily

WED = date(2026, 7, 8)
THU = date(2026, 7, 9)
FRI = date(2026, 7, 10)


def daily(day, rows):
    return pl.DataFrame(
        {
            "day": [day] * len(rows),
            "symbol": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
            "value": [r[6] for r in rows],
            "change_pct": [r[7] for r in rows],
        },
        schema=DAILY_SNAPSHOT_SCHEMA,
    )


def caps(day, rows):
    return pl.DataFrame(
        {
            "day": [day] * len(rows),
            "symbol": [r[0] for r in rows],
            "close": [r[1] for r in rows],
            "cap": [r[2] for r in rows],
            "volume": [r[3] for r in rows],
            "value": [r[4] for r in rows],
            "shares": [r[5] for r in rows],
        },
        schema=MARKET_CAP_SCHEMA,
    )


OFFICIAL = [
    ("005930", 70500.0, 71000.0, 69000.0, 70000.0, 33_804_868.0, 5e12, 0.18),
    ("035720", 45000.0, 46000.0, 44000.0, 45500.0, 8_156_179.0, 3e12, -0.30),
]
OFFICIAL_CAPS = [
    ("005930", 70000.0, 4e14, 33_804_868.0, 5e12, 5.9e9),
    ("035720", 45500.0, 1.8e13, 8_156_179.0, 3e12, 4.4e8),
]


def stock_info_frame(day, symbols):
    return pl.DataFrame(
        [
            {
                "day": day,
                "symbol": symbol,
                "name": symbol,
                "market": "KOSPI",
                "security_group": "주권",
                "share_kind": "보통주",
                "section": "",
                "listed_on": date(2010, 1, 4),
                "shares": 1000.0,
            }
            for symbol in symbols
        ],
        schema=STOCK_INFO_SCHEMA,
    )


class FakeKrx:
    def __init__(self, by_day):
        self.by_day = by_day
        self.closed = False
        self.asked = []
        self.info_asked = []

    def snapshot(self, day):
        self.asked.append(day)
        entry = self.by_day.get(day)
        if entry is None:
            return (
                pl.DataFrame(schema=DAILY_SNAPSHOT_SCHEMA),
                pl.DataFrame(schema=MARKET_CAP_SCHEMA),
            )
        if isinstance(entry, Exception):
            raise entry
        return entry

    def stock_info(self, day):
        self.info_asked.append(day)
        return stock_info_frame(day, [row[0] for row in OFFICIAL])

    def close(self):
        self.closed = True


def official_for(day):
    return daily(day, OFFICIAL), caps(day, OFFICIAL_CAPS)


def run(cfg, cal, state, snapshots, alerter, source, *, start=THU, end=FRI):
    return reconcile_daily(
        cfg,
        cal=cal,
        state=state,
        snapshots=snapshots,
        alerter=alerter,
        start=start,
        end=end,
        source=source,
    )


def test_corrects_understated_volume(cfg, cal, state, snapshots, alerter, notifier):
    """FDR이 마감 직후에 준 거래량은 시간외 집계 전이라 덜 차 있다. 확정본으로 덮어써야 한다."""
    stale = [
        ("005930", 70500.0, 71000.0, 69000.0, 70000.0, 33_660_974.0, 4.9e12, 0.18),
        ("035720", 45000.0, 46000.0, 44000.0, 45500.0, 8_071_517.0, 2.9e12, -0.30),
    ]
    snapshots.write_date(DAILY_CANDLES, THU, daily(THU, stale))
    snapshots.write_date(MARKET_CAP, THU, caps(THU, OFFICIAL_CAPS))
    source = FakeKrx({THU: official_for(THU)})

    summary = run(cfg, cal, state, snapshots, alerter, source, start=THU, end=THU)

    assert summary.status == "ok"
    assert summary.corrected == [THU.isoformat()]
    fixed = snapshots.read_date(DAILY_CANDLES, THU).sort("symbol")
    assert fixed["volume"].to_list() == [33_804_868.0, 8_156_179.0]
    assert fixed["value"].to_list() == [5e12, 3e12]
    assert summary.days[0].corrections == {"volume": 2, "value": 2}
    assert any("교정" in text for text in notifier.sent)


def test_fills_missing_day(cfg, cal, state, snapshots, alerter, notifier):
    source = FakeKrx({FRI: official_for(FRI)})
    summary = run(cfg, cal, state, snapshots, alerter, source, start=FRI, end=FRI)

    assert summary.filled == [FRI.isoformat()]
    assert snapshots.read_date(DAILY_CANDLES, FRI).height == 2
    assert snapshots.read_date(MARKET_CAP, FRI).height == 2
    assert any("채웠습니다" in text for text in notifier.sent)


def test_matching_day_is_left_alone(cfg, cal, state, snapshots, alerter, notifier):
    """marcap 백필분은 KRX 공식과 소수점까지 같다. 매일 돌려도 아무것도 건드리면 안 된다."""
    snapshots.write_date(DAILY_CANDLES, THU, daily(THU, OFFICIAL))
    snapshots.write_date(MARKET_CAP, THU, caps(THU, OFFICIAL_CAPS))
    before = snapshots.path(DAILY_CANDLES, THU).read_bytes()
    source = FakeKrx({THU: official_for(THU)})

    summary = run(cfg, cal, state, snapshots, alerter, source, start=THU, end=THU)

    assert summary.status == "ok"
    assert summary.corrected == []
    assert summary.days[0].status == "ok"
    assert snapshots.path(DAILY_CANDLES, THU).read_bytes() == before
    assert notifier.sent == []


def test_keeps_symbols_the_official_source_does_not_cover(cfg, cal, state, snapshots, alerter):
    """KRX 주식 엔드포인트는 ETN 등을 다루지 않는다. 우리가 가진 걸 지우면 안 된다."""
    ours = [*OFFICIAL, ("500001", 100.0, 110.0, 90.0, 105.0, 7.0, 700.0, 1.0)]
    snapshots.write_date(DAILY_CANDLES, THU, daily(THU, ours))
    snapshots.write_date(MARKET_CAP, THU, caps(THU, OFFICIAL_CAPS))
    source = FakeKrx({THU: official_for(THU)})

    run(cfg, cal, state, snapshots, alerter, source, start=THU, end=THU)

    kept = snapshots.read_date(DAILY_CANDLES, THU)
    assert set(kept["symbol"].to_list()) == {"005930", "035720", "500001"}
    assert kept.filter(kept["symbol"] == "500001").row(0, named=True)["close"] == 105.0


def test_adds_symbols_we_are_missing(cfg, cal, state, snapshots, alerter):
    snapshots.write_date(DAILY_CANDLES, THU, daily(THU, OFFICIAL[:1]))
    snapshots.write_date(MARKET_CAP, THU, caps(THU, OFFICIAL_CAPS[:1]))
    source = FakeKrx({THU: official_for(THU)})

    summary = run(cfg, cal, state, snapshots, alerter, source, start=THU, end=THU)

    assert summary.corrected == [THU.isoformat()]
    assert summary.days[0].added == 1
    assert snapshots.read_date(DAILY_CANDLES, THU).height == 2


def test_unpublished_day_is_not_an_error(cfg, cal, state, snapshots, alerter, notifier):
    """KRX Open API는 T+1이라 당일치는 아직 없다. 다음 실행이 주워가면 된다."""
    snapshots.write_date(DAILY_CANDLES, THU, daily(THU, OFFICIAL))
    snapshots.write_date(MARKET_CAP, THU, caps(THU, OFFICIAL_CAPS))
    source = FakeKrx({THU: official_for(THU)})

    summary = run(cfg, cal, state, snapshots, alerter, source, start=THU, end=FRI)

    assert summary.status == "ok"
    assert summary.unavailable == [FRI.isoformat()]
    assert not snapshots.has_date(DAILY_CANDLES, FRI)
    assert notifier.sent == []


def test_source_error_is_reported_and_alerted(cfg, cal, state, snapshots, alerter, notifier):
    source = FakeKrx({THU: SourceError("KRX Open API 인증 거부")})
    summary = run(cfg, cal, state, snapshots, alerter, source, start=THU, end=THU)

    assert summary.status == "error"
    assert "인증 거부" in summary.errors[0]
    assert state.recent_runs("reconcile")[0].ok is False
    assert any("KRX 공식 대조 실패" in text for text in notifier.sent)


def test_walks_every_session_in_range_and_closes_nothing_it_borrowed(
    cfg, cal, state, snapshots, alerter
):
    source = FakeKrx({})
    run(cfg, cal, state, snapshots, alerter, source, start=WED, end=FRI)
    assert source.asked == [WED, THU, FRI]
    assert source.closed is False


def test_stores_official_stock_info_for_the_day(cfg, cal, state, snapshots, alerter):
    source = FakeKrx({THU: official_for(THU)})
    run(cfg, cal, state, snapshots, alerter, source, start=THU, end=THU)

    stored = snapshots.read_date(STOCK_INFO, THU)
    assert stored is not None
    assert stored.get_column("symbol").to_list() == ["005930", "035720"]
    assert source.info_asked == [THU]


def test_skips_stock_info_on_unpublished_days(cfg, cal, state, snapshots, alerter):
    source = FakeKrx({})
    run(cfg, cal, state, snapshots, alerter, source, start=THU, end=THU)

    assert source.info_asked == []
    assert snapshots.read_date(STOCK_INFO, THU) is None


@pytest.mark.parametrize(
    ("ours", "expected"),
    [
        (70000.0, {}),
        (70000.4, {}),
        (70001.0, {"close": 1}),
        (None, {"close": 1}),
    ],
)
def test_apply_official_tolerance(ours, expected):
    mine = daily(THU, [("005930", 70500.0, 71000.0, 69000.0, ours, 33_804_868.0, 5e12, 0.18)])
    theirs = daily(THU, OFFICIAL[:1])
    _, corrections, added = apply_official(mine, theirs, ("close",))
    assert corrections == expected
    assert added == 0

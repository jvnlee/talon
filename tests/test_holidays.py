from datetime import date

from talon.errors import SourceError
from talon.ingest.holidays import sync_holidays
from talon.markets.kr import closures_path, load_stored_closures

TODAY = date(2026, 7, 16)


def _fetch_2026(year):
    if year == 2026:
        return {date(2026, 7, 17): "제헌절", date(2026, 10, 5): "추석"}
    return {}


def test_sync_stores_new_closures_and_alerts(cfg, state, alerter, notifier):
    asked = []

    def fetch(year):
        asked.append(year)
        return _fetch_2026(year)

    summary = sync_holidays(cfg, state=state, alerter=alerter, today=TODAY, fetch=fetch)
    assert summary.status == "ok"
    assert asked == [2026, 2027]
    assert summary.added == ["2026-07-17", "2026-10-05"]
    assert load_stored_closures(closures_path(cfg.data_dir)) == {
        date(2026, 7, 17): "제헌절",
        date(2026, 10, 5): "추석",
    }
    assert any("제헌절" in text for text in notifier.sent)


def test_sync_is_quiet_when_nothing_new(cfg, state, alerter, notifier):
    sync_holidays(cfg, state=state, alerter=alerter, today=TODAY, fetch=_fetch_2026)
    sent_before = len(notifier.sent)
    summary = sync_holidays(cfg, state=state, alerter=alerter, today=TODAY, fetch=_fetch_2026)
    assert summary.status == "ok"
    assert summary.added == []
    assert summary.known == 2
    assert len(notifier.sent) == sent_before


def test_sync_alerts_on_source_failure(cfg, state, alerter, notifier):
    def fetch(year):
        raise SourceError("boom")

    summary = sync_holidays(cfg, state=state, alerter=alerter, today=TODAY, fetch=fetch)
    assert summary.status == "error"
    assert len(summary.errors) == 2
    assert any("휴장일 동기화 실패" in text for text in notifier.sent)


def test_sync_treats_empty_current_year_as_error(cfg, state, alerter, notifier):
    summary = sync_holidays(cfg, state=state, alerter=alerter, today=TODAY, fetch=lambda year: {})
    assert summary.status == "error"
    assert load_stored_closures(closures_path(cfg.data_dir)) == {}


def test_sync_partial_when_next_year_fails(cfg, state, alerter, notifier):
    def fetch(year):
        if year == 2027:
            raise SourceError("boom")
        return _fetch_2026(year)

    summary = sync_holidays(cfg, state=state, alerter=alerter, today=TODAY, fetch=fetch)
    assert summary.status == "partial"
    assert summary.added == ["2026-07-17", "2026-10-05"]

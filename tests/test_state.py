from datetime import date, timedelta


def test_heartbeat_roundtrip(state):
    assert state.get_heartbeat("collect") is None
    state.heartbeat("collect", True, {"status": "ok"})
    beat = state.get_heartbeat("collect")
    assert beat is not None
    assert beat.ok
    assert beat.detail == {"status": "ok"}

    state.heartbeat("collect", False, {"error": "boom"})
    beat = state.get_heartbeat("collect")
    assert not beat.ok
    assert beat.detail == {"error": "boom"}


def test_job_runs_and_consecutive_failures(state):
    run1 = state.start_job("collect")
    state.finish_job(run1, True)
    run2 = state.start_job("collect")
    state.finish_job(run2, False, {"error": "x"})
    run3 = state.start_job("collect")
    state.finish_job(run3, False)
    state.start_job("collect")

    runs = state.recent_runs("collect", limit=10)
    assert len(runs) == 4
    assert runs[0].ok is None
    assert state.consecutive_failures("collect") == 2
    assert state.consecutive_failures("eod") == 0


def test_alert_cooldown(state):
    cooldown = timedelta(hours=1)
    assert state.should_alert("k", cooldown)
    state.mark_alerted("k")
    assert not state.should_alert("k", cooldown)
    assert state.should_alert("k", timedelta(0))
    assert state.should_alert("other", cooldown)


def test_universe_snapshots(state):
    assert state.latest_universe() is None
    state.save_universe(date(2026, 7, 9), ["005930"], {"size": 1})
    state.save_universe(date(2026, 7, 10), ["005930", "000660"], {"size": 2})

    latest = state.latest_universe()
    assert latest.day == date(2026, 7, 10)
    assert latest.symbols == ["005930", "000660"]
    assert latest.criteria == {"size": 2}

    earlier = state.latest_universe(on_or_before=date(2026, 7, 9))
    assert earlier.day == date(2026, 7, 9)

    state.save_universe(date(2026, 7, 10), ["005930"], {"size": 1})
    assert state.latest_universe().symbols == ["005930"]


def test_trials_roundtrip(state):
    assert state.trial_count() == 0
    assert state.trial_sharpes() == []

    first = state.record_trial(
        start=date(2016, 7, 11),
        end=date(2023, 6, 30),
        symbols=[],
        strategies=["momo_breakout", "pullback", "meanrev"],
        sharpe_daily=0.05,
        trades=120,
        total_return_pct=42.5,
    )
    second = state.record_trial(
        start=None,
        end=None,
        symbols=["005930"],
        strategies=["pullback"],
        sharpe_daily=None,
        trades=0,
        total_return_pct=None,
    )

    assert second == first + 1
    assert state.trial_count() == 2
    assert state.trial_sharpes() == [0.05]

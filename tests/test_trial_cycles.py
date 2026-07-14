from datetime import date

import pytest

from talon.data.state import ARCHIVE_CYCLE, StateDB


def add_trial(state: StateDB, sharpe: float) -> int:
    return state.record_trial(
        start=date(2016, 7, 1),
        end=date(2023, 12, 31),
        symbols=["005930"],
        strategies=["probe"],
        sharpe_daily=sharpe,
        trades=10,
        total_return_pct=1.0,
    )


def test_trials_land_in_the_archive_cycle_by_default(state):
    add_trial(state, 0.5)

    assert state.active_cycle() == ARCHIVE_CYCLE
    assert state.trial_count() == 1


def test_opening_a_cycle_resets_the_count_without_deleting(state):
    add_trial(state, 0.5)
    add_trial(state, 0.7)

    state.open_cycle("close-bet-v1", note="ADR 0013 이후 첫 사이클")

    assert state.active_cycle() == "close-bet-v1"
    assert state.trial_count() == 0
    assert state.trial_sharpes() == []
    assert state.cycle_counts() == {ARCHIVE_CYCLE: 2}


def test_new_trials_land_in_the_open_cycle(state):
    add_trial(state, 0.5)
    state.open_cycle("close-bet-v1")
    add_trial(state, 1.2)
    add_trial(state, 1.4)

    assert state.trial_count() == 2
    assert state.trial_sharpes() == [1.2, 1.4]
    assert state.cycle_counts() == {ARCHIVE_CYCLE: 1, "close-bet-v1": 2}


def test_opening_a_second_cycle_closes_the_first(state):
    state.open_cycle("close-bet-v1")
    add_trial(state, 1.2)
    state.open_cycle("close-bet-v2")

    assert state.active_cycle() == "close-bet-v2"
    assert state.trial_count() == 0
    assert state.cycle_counts() == {"close-bet-v1": 1}


def test_cycle_names_cannot_be_reused(state):
    state.open_cycle("close-bet-v1")

    with pytest.raises(ValueError, match="이미 있는"):
        state.open_cycle("close-bet-v1")


def test_blank_cycle_name_is_rejected(state):
    with pytest.raises(ValueError, match="비었습니다"):
        state.open_cycle("   ")


def test_existing_db_migrates_and_keeps_its_trials(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = StateDB(path)
    add_trial(first, 0.9)
    first._conn.execute("ALTER TABLE trials DROP COLUMN cycle")
    first.close()

    second = StateDB(path)

    assert second.trial_count() == 1
    assert second.cycle_counts() == {ARCHIVE_CYCLE: 1}
    second.close()

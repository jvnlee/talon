import json

import pytest

from talon.sources.kis import RatePacer


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def make_pacer(tmp_path, fake, rps=8.0, penalty_rps=2.0, penalty_seconds=30.0):
    return RatePacer(
        tmp_path / "pacer.json",
        rps=rps,
        penalty_rps=penalty_rps,
        penalty_seconds=penalty_seconds,
        clock=fake.clock,
        sleep=fake.sleep,
    )


def test_spacing_is_uniform_across_instances(tmp_path):
    fake = FakeTime()
    first = make_pacer(tmp_path, fake)
    second = make_pacer(tmp_path, fake)

    first.acquire()
    second.acquire()
    first.acquire()

    assert fake.slept == [0.125, 0.125]


def test_rate_limit_report_slows_every_instance(tmp_path):
    fake = FakeTime()
    first = make_pacer(tmp_path, fake)
    second = make_pacer(tmp_path, fake)

    first.acquire()
    first.report_rate_limit()
    second.acquire()
    second.acquire()

    assert fake.slept == [0.125, 0.5]

    fake.now = 1031.0
    second.acquire()
    first.acquire()

    assert fake.slept == [0.125, 0.5, 0.125]


def test_penalty_window_extends_on_repeat_reports(tmp_path):
    fake = FakeTime()
    pacer = make_pacer(tmp_path, fake)

    pacer.report_rate_limit()
    fake.now = 1020.0
    pacer.report_rate_limit()

    state = json.loads((tmp_path / "pacer.json").read_text())
    assert state["penalty_until"] == pytest.approx(1050.0)


def test_corrupt_state_is_reset(tmp_path):
    (tmp_path / "pacer.json").write_text("not json")
    fake = FakeTime()
    pacer = make_pacer(tmp_path, fake)

    pacer.acquire()

    assert fake.slept == []
    state = json.loads((tmp_path / "pacer.json").read_text())
    assert state["next_at"] == pytest.approx(1000.125)


def test_runaway_next_at_is_clamped(tmp_path):
    (tmp_path / "pacer.json").write_text(json.dumps({"next_at": 999999.0, "penalty_until": 0.0}))
    fake = FakeTime()
    pacer = make_pacer(tmp_path, fake)

    pacer.acquire()

    assert fake.slept == []
    state = json.loads((tmp_path / "pacer.json").read_text())
    assert state["next_at"] == pytest.approx(1000.125)

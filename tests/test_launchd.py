import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from talon.errors import TalonError
from talon.launchd import JOBS, install, plist_path, render_plist, uninstall

TALON_BIN = Path("/opt/talon/.venv/bin/talon")
DATA_DIR = Path("/Users/tester/.talon")


def test_collect_plist():
    spec = plistlib.loads(render_plist("collect", TALON_BIN, DATA_DIR))
    assert spec["Label"] == "com.talon.collect"
    assert spec["ProgramArguments"] == ["/usr/bin/caffeinate", "-s", str(TALON_BIN), "collect"]
    assert spec["StartInterval"] == 300
    assert spec["RunAtLoad"] is True
    assert spec["EnvironmentVariables"] == {"TALON_DATA_DIR": str(DATA_DIR)}
    assert spec["StandardErrorPath"].endswith("logs/collect.log")


def test_watchdog_plist():
    spec = plistlib.loads(render_plist("watchdog", TALON_BIN, DATA_DIR))
    assert spec["StartInterval"] == 600
    assert "RunAtLoad" not in spec


def test_eod_plist_schedule():
    spec = plistlib.loads(render_plist("eod", TALON_BIN, DATA_DIR))
    entries = spec["StartCalendarInterval"]
    assert len(entries) == 10
    assert {entry["Weekday"] for entry in entries} == {1, 2, 3, 4, 5}
    assert {(entry["Hour"], entry["Minute"]) for entry in entries} == {(16, 40), (18, 30)}


def test_backfill_plist_runs_daily_catchup():
    spec = plistlib.loads(render_plist("backfill", TALON_BIN, DATA_DIR))
    assert spec["Label"] == "com.talon.backfill"
    assert spec["ProgramArguments"] == [
        "/usr/bin/caffeinate",
        "-s",
        str(TALON_BIN),
        "backfill-daily",
        "--years",
        "1",
    ]
    assert spec["StartCalendarInterval"] == [{"Hour": 19, "Minute": 0}]
    assert "StartInterval" not in spec


def test_reconcile_plist_runs_before_the_next_session():
    spec = plistlib.loads(render_plist("reconcile", TALON_BIN, DATA_DIR))
    assert spec["Label"] == "com.talon.reconcile"
    assert spec["ProgramArguments"][-1] == "reconcile"
    entries = spec["StartCalendarInterval"]
    assert {entry["Weekday"] for entry in entries} == {1, 2, 3, 4, 5}
    assert {entry["Hour"] for entry in entries} == {9, 13}
    assert all(entry["Hour"] < 15 for entry in entries)


def test_adjust_plist_runs_after_eod_and_after_reconcile():
    spec = plistlib.loads(render_plist("adjust", TALON_BIN, DATA_DIR))
    assert spec["Label"] == "com.talon.adjust"
    assert spec["ProgramArguments"][-2:] == ["adjust", "build"]
    entries = spec["StartCalendarInterval"]

    nightly = [e for e in entries if "Weekday" not in e]
    assert nightly == [{"Hour": 20, "Minute": 0}]

    catchup = [e for e in entries if "Weekday" in e]
    assert {e["Weekday"] for e in catchup} == {1, 2, 3, 4, 5}
    assert {e["Hour"] for e in catchup} == {14}
    assert all(e["Hour"] < 15 for e in catchup)


def test_close_auction_plist_covers_the_closing_auction():
    spec = plistlib.loads(render_plist("close-auction", TALON_BIN, DATA_DIR))
    assert spec["Label"] == "com.talon.close-auction"
    assert spec["ProgramArguments"][-1] == "close-auction"
    entries = spec["StartCalendarInterval"]
    assert {entry["Weekday"] for entry in entries} == {1, 2, 3, 4, 5}
    assert {(entry["Hour"], entry["Minute"]) for entry in entries} == {(15, 20)}


def test_us_eod_plist_runs_after_us_close_and_before_briefing():
    spec = plistlib.loads(render_plist("us-eod", TALON_BIN, DATA_DIR))
    assert spec["Label"] == "com.talon.us-eod"
    assert spec["ProgramArguments"][-1] == "us-eod"
    entries = spec["StartCalendarInterval"]
    assert {entry["Weekday"] for entry in entries} == {2, 3, 4, 5, 6}
    assert {(entry["Hour"], entry["Minute"]) for entry in entries} == {(6, 30)}


def test_us_calendar_plist_runs_daily():
    spec = plistlib.loads(render_plist("us-calendar", TALON_BIN, DATA_DIR))
    assert spec["ProgramArguments"][-1] == "us-calendar"
    assert spec["StartCalendarInterval"] == [{"Hour": 6, "Minute": 0}]


def test_briefing_snapshot_plist_runs_on_kr_weekday_mornings():
    spec = plistlib.loads(render_plist("briefing-snapshot", TALON_BIN, DATA_DIR))
    assert spec["ProgramArguments"][-1] == "briefing-snapshot"
    entries = spec["StartCalendarInterval"]
    assert {entry["Weekday"] for entry in entries} == {1, 2, 3, 4, 5}
    assert {(entry["Hour"], entry["Minute"]) for entry in entries} == {(7, 30)}


def test_install_removes_retired_us_night_plist(tmp_path):
    stale = plist_path("us-night", tmp_path)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"stale")

    install(TALON_BIN, DATA_DIR, directory=tmp_path, run_launchctl=False)

    assert not stale.exists()


def test_overtime_plist_runs_after_the_evening_session():
    assert "overtime" in JOBS
    spec = plistlib.loads(render_plist("overtime", TALON_BIN, DATA_DIR))
    assert spec["Label"] == "com.talon.overtime"
    assert spec["ProgramArguments"][-1] == "overtime"
    entries = spec["StartCalendarInterval"]
    assert {entry["Weekday"] for entry in entries} == {1, 2, 3, 4, 5}
    assert {(entry["Hour"], entry["Minute"]) for entry in entries} == {(18, 10)}


def test_every_job_holds_a_sleep_assertion_while_running():
    for job in JOBS:
        spec = plistlib.loads(render_plist(job, TALON_BIN, DATA_DIR))
        assert spec["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-s"]


def test_install_and_uninstall_writes_plists(tmp_path):
    written = install(TALON_BIN, DATA_DIR, directory=tmp_path, run_launchctl=False)
    assert [p.name for p in written] == [f"com.talon.{job}.plist" for job in JOBS]
    for job in JOBS:
        assert plist_path(job, tmp_path).exists()

    removed = uninstall(directory=tmp_path, run_launchctl=False)
    assert len(removed) == len(JOBS)
    assert not any(plist_path(job, tmp_path).exists() for job in JOBS)


def test_bootstrap_retries_while_bootout_settles(tmp_path, monkeypatch):
    attempts = {}

    def fake_run(cmd, capture_output, text):
        if cmd[1] == "bootstrap":
            path = cmd[3]
            attempts[path] = attempts.get(path, 0) + 1
            if attempts[path] == 1:
                return SimpleNamespace(
                    returncode=5, stderr="Bootstrap failed: 5: Input/output error"
                )
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("talon.launchd.subprocess.run", fake_run)
    slept = []

    written = install(TALON_BIN, DATA_DIR, directory=tmp_path, sleep=slept.append)

    assert len(written) == len(JOBS)
    assert all(count == 2 for count in attempts.values())
    assert len(slept) == len(JOBS)


def test_one_stuck_job_does_not_block_the_rest(tmp_path, monkeypatch):
    bootstrapped = []

    def fake_run(cmd, capture_output, text):
        if cmd[1] == "bootstrap":
            path = cmd[3]
            if "reconcile" in path:
                return SimpleNamespace(
                    returncode=5, stderr="Bootstrap failed: 5: Input/output error"
                )
            bootstrapped.append(path)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("talon.launchd.subprocess.run", fake_run)

    with pytest.raises(TalonError, match="reconcile"):
        install(TALON_BIN, DATA_DIR, directory=tmp_path, sleep=lambda seconds: None)

    assert len(bootstrapped) == len(JOBS) - 1
    assert plist_path("close-auction", tmp_path).exists()

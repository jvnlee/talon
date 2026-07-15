import plistlib
from pathlib import Path

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


def test_us_night_plist_runs_after_us_close():
    spec = plistlib.loads(render_plist("us-night", TALON_BIN, DATA_DIR))
    assert spec["Label"] == "com.talon.us-night"
    assert spec["ProgramArguments"][-1] == "us-night"
    entries = spec["StartCalendarInterval"]
    assert {entry["Weekday"] for entry in entries} == {2, 3, 4, 5, 6}
    assert {(entry["Hour"], entry["Minute"]) for entry in entries} == {(9, 20)}


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

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from talon.errors import TalonError

LABEL_PREFIX = "com.talon."
JOBS = ("collect", "watchdog", "eod")


def default_talon_bin() -> Path:
    return Path(sys.executable).parent / "talon"


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(job: str, directory: Path) -> Path:
    return directory / f"{LABEL_PREFIX}{job}.plist"


def render_plist(job: str, talon_bin: Path, data_dir: Path) -> bytes:
    spec: dict[str, object] = {
        "Label": f"{LABEL_PREFIX}{job}",
        "ProgramArguments": [str(talon_bin), job],
        "WorkingDirectory": str(data_dir),
        "StandardOutPath": str(data_dir / "logs" / f"{job}.log"),
        "StandardErrorPath": str(data_dir / "logs" / f"{job}.log"),
        "EnvironmentVariables": {"TALON_DATA_DIR": str(data_dir)},
    }
    if job == "collect":
        spec["StartInterval"] = 300
        spec["RunAtLoad"] = True
    elif job == "watchdog":
        spec["StartInterval"] = 600
    elif job == "eod":
        spec["StartCalendarInterval"] = [
            {"Weekday": weekday, "Hour": hour, "Minute": minute}
            for weekday in range(1, 6)
            for hour, minute in ((16, 40), (18, 30))
        ]
    else:
        raise TalonError(f"unknown launchd job: {job}")
    return plistlib.dumps(spec)


def _launchctl(*args: str, check: bool) -> None:
    result = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise TalonError(f"launchctl {' '.join(args)} failed: {result.stderr.strip()}")


def install(
    talon_bin: Path,
    data_dir: Path,
    *,
    directory: Path | None = None,
    run_launchctl: bool = True,
) -> list[Path]:
    directory = directory or agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for job in JOBS:
        path = plist_path(job, directory)
        path.write_bytes(render_plist(job, talon_bin, data_dir))
        written.append(path)
        if run_launchctl:
            _launchctl("bootout", f"gui/{os.getuid()}/{LABEL_PREFIX}{job}", check=False)
            _launchctl("bootstrap", f"gui/{os.getuid()}", str(path), check=True)
    return written


def uninstall(*, directory: Path | None = None, run_launchctl: bool = True) -> list[Path]:
    directory = directory or agents_dir()
    removed: list[Path] = []
    for job in JOBS:
        path = plist_path(job, directory)
        if run_launchctl:
            _launchctl("bootout", f"gui/{os.getuid()}/{LABEL_PREFIX}{job}", check=False)
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed

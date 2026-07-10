import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from talon.models import Heartbeat, JobRun, UniverseSnapshot
from talon.timeutil import now_utc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeats (
    job TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs (job, id DESC);
CREATE TABLE IF NOT EXISTS alert_log (
    key TEXT PRIMARY KEY,
    last_sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS universe_snapshots (
    day TEXT PRIMARY KEY,
    symbols TEXT NOT NULL,
    criteria TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class StateDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def heartbeat(self, job: str, ok: bool, detail: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            "INSERT INTO heartbeats (job, ts, ok, detail) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(job) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, detail=excluded.detail",
            (job, now_utc().isoformat(), int(ok), json.dumps(detail or {})),
        )

    def get_heartbeat(self, job: str) -> Heartbeat | None:
        row = self._conn.execute(
            "SELECT job, ts, ok, detail FROM heartbeats WHERE job = ?", (job,)
        ).fetchone()
        if row is None:
            return None
        return Heartbeat(job=row[0], ts=_dt(row[1]), ok=bool(row[2]), detail=json.loads(row[3]))

    def start_job(self, job: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO job_runs (job, started_at) VALUES (?, ?)",
            (job, now_utc().isoformat()),
        )
        return int(cursor.lastrowid or 0)

    def finish_job(self, run_id: int, ok: bool, detail: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            "UPDATE job_runs SET finished_at = ?, ok = ?, detail = ? WHERE id = ?",
            (now_utc().isoformat(), int(ok), json.dumps(detail or {}), run_id),
        )

    def recent_runs(self, job: str, limit: int = 5) -> list[JobRun]:
        rows = self._conn.execute(
            "SELECT id, job, started_at, finished_at, ok, detail FROM job_runs "
            "WHERE job = ? ORDER BY id DESC LIMIT ?",
            (job, limit),
        ).fetchall()
        return [
            JobRun(
                id=row[0],
                job=row[1],
                started_at=_dt(row[2]),
                finished_at=_dt(row[3]) if row[3] else None,
                ok=None if row[4] is None else bool(row[4]),
                detail=json.loads(row[5]),
            )
            for row in rows
        ]

    def consecutive_failures(self, job: str) -> int:
        failures = 0
        for run in self.recent_runs(job, limit=20):
            if run.ok is None:
                continue
            if run.ok:
                break
            failures += 1
        return failures

    def should_alert(self, key: str, cooldown: timedelta) -> bool:
        row = self._conn.execute(
            "SELECT last_sent_at FROM alert_log WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return True
        return now_utc() - _dt(row[0]) >= cooldown

    def mark_alerted(self, key: str) -> None:
        self._conn.execute(
            "INSERT INTO alert_log (key, last_sent_at) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET last_sent_at=excluded.last_sent_at",
            (key, now_utc().isoformat()),
        )

    def save_universe(self, day: date, symbols: list[str], criteria: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO universe_snapshots (day, symbols, criteria, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET symbols=excluded.symbols, "
            "criteria=excluded.criteria, created_at=excluded.created_at",
            (day.isoformat(), json.dumps(symbols), json.dumps(criteria), now_utc().isoformat()),
        )

    def latest_universe(self, on_or_before: date | None = None) -> UniverseSnapshot | None:
        if on_or_before is None:
            row = self._conn.execute(
                "SELECT day, symbols, criteria, created_at FROM universe_snapshots "
                "ORDER BY day DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT day, symbols, criteria, created_at FROM universe_snapshots "
                "WHERE day <= ? ORDER BY day DESC LIMIT 1",
                (on_or_before.isoformat(),),
            ).fetchone()
        if row is None:
            return None
        return UniverseSnapshot(
            day=date.fromisoformat(row[0]),
            symbols=json.loads(row[1]),
            criteria=json.loads(row[2]),
            created_at=_dt(row[3]),
        )

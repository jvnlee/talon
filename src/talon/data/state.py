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
CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    start_day TEXT,
    end_day TEXT,
    symbols TEXT NOT NULL DEFAULT '[]',
    strategies TEXT NOT NULL DEFAULT '[]',
    sharpe_daily REAL,
    trades INTEGER NOT NULL DEFAULT 0,
    total_return_pct REAL,
    cycle TEXT NOT NULL DEFAULT 'cycle-0'
);
CREATE TABLE IF NOT EXISTS trial_cycles (
    name TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    note TEXT NOT NULL DEFAULT ''
);
"""

ARCHIVE_CYCLE = "cycle-0"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class StateDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(trials)")}
        if "cycle" not in columns:
            self._conn.execute(
                f"ALTER TABLE trials ADD COLUMN cycle TEXT NOT NULL DEFAULT '{ARCHIVE_CYCLE}'"
            )

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

    def active_cycle(self) -> str:
        row = self._conn.execute(
            "SELECT name FROM trial_cycles WHERE closed_at IS NULL ORDER BY opened_at DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else ARCHIVE_CYCLE

    def open_cycle(self, name: str, *, note: str = "") -> None:
        if not name.strip():
            raise ValueError("사이클 이름이 비었습니다")
        existing = self._conn.execute(
            "SELECT 1 FROM trial_cycles WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            raise ValueError(f"이미 있는 사이클입니다: {name}")
        now = now_utc().isoformat()
        self._conn.execute("UPDATE trial_cycles SET closed_at = ? WHERE closed_at IS NULL", (now,))
        self._conn.execute(
            "INSERT INTO trial_cycles (name, opened_at, closed_at, note) VALUES (?, ?, NULL, ?)",
            (name, now, note),
        )

    def cycle_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT cycle, COUNT(*) FROM trials GROUP BY cycle ORDER BY cycle"
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def record_trial(
        self,
        *,
        start: date | None,
        end: date | None,
        symbols: list[str],
        strategies: list[str],
        sharpe_daily: float | None,
        trades: int,
        total_return_pct: float | None,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO trials "
            "(ts, start_day, end_day, symbols, strategies, sharpe_daily, trades, "
            "total_return_pct, cycle) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now_utc().isoformat(),
                start.isoformat() if start else None,
                end.isoformat() if end else None,
                json.dumps(symbols),
                json.dumps(strategies),
                sharpe_daily,
                trades,
                total_return_pct,
                self.active_cycle(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def trial_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM trials WHERE cycle = ?", (self.active_cycle(),)
        ).fetchone()
        return int(row[0])

    def trial_sharpes(self) -> list[float]:
        rows = self._conn.execute(
            "SELECT sharpe_daily FROM trials "
            "WHERE cycle = ? AND sharpe_daily IS NOT NULL ORDER BY id",
            (self.active_cycle(),),
        ).fetchall()
        return [float(row[0]) for row in rows]

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

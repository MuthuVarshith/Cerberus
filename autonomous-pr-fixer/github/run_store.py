"""
Persistent state for the GitHub App: deliveries and runs, in SQLite.

- Webhook deliveries are claimed atomically by X-GitHub-Delivery id, so a
  redelivered or duplicated webhook starts at most one run, across restarts.
- Every run has a row from queueing to its terminal state, which is the audit
  trail linking a GitHub event to a run.json artifact and a Check Run.
- Per-repository limits (runs per hour, concurrent runs) are read from here.
- Runs left `running` by a crashed process are marked ERROR on startup.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    delivery_id TEXT,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,
    target INTEGER NOT NULL,
    head_sha TEXT,
    requested_by TEXT,
    status TEXT NOT NULL,
    final_state TEXT,
    refusal_code TEXT,
    check_run_id INTEGER,
    artifact_path TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS runs_repo_created ON runs(repo, created_at);
"""

ACTIVE_STATUSES = ("queued", "running")


@dataclass
class RunRow:
    run_id: str
    repo: str
    kind: str
    target: int
    status: str
    head_sha: Optional[str] = None
    requested_by: Optional[str] = None
    final_state: Optional[str] = None
    refusal_code: Optional[str] = None
    check_run_id: Optional[int] = None
    artifact_path: Optional[str] = None
    error: Optional[str] = None


class RunStore:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def claim_delivery(self, delivery_id: str, event: str) -> bool:
        """True exactly once per delivery id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO deliveries (delivery_id, event, received_at) VALUES (?, ?, ?)",
                (delivery_id, event, time.time()),
            )
            return cur.rowcount == 1

    def create_run(self, run_id: str, delivery_id: str, repo: str, kind: str, target: int,
                   head_sha: Optional[str], requested_by: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, delivery_id, repo, kind, target, head_sha, requested_by, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (run_id, delivery_id, repo, kind, int(target), head_sha, requested_by, time.time()),
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "final_state", "refusal_code", "check_run_id", "artifact_path", "error", "started_at", "finished_at", "head_sha"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE runs SET {assignments} WHERE run_id = ?", (*fields.values(), run_id))

    def get_run(self, run_id: str) -> Optional[RunRow]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        return RunRow(**{k: data[k] for k in RunRow.__dataclass_fields__})

    def runs_since(self, repo: str, seconds: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM runs WHERE repo = ? AND created_at >= ?", (repo, time.time() - seconds)
            ).fetchone()
        return int(row[0])

    def active_runs(self, repo: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM runs WHERE repo = ? AND status IN ('queued', 'running')", (repo,)
            ).fetchone()
        return int(row[0])

    def recover_interrupted(self) -> List[str]:
        """Mark runs left active by a previous process as ERROR. Returns their ids."""
        with self._lock:
            rows = self._conn.execute("SELECT run_id FROM runs WHERE status IN ('queued', 'running')").fetchall()
            ids = [r["run_id"] for r in rows]
            self._conn.execute(
                "UPDATE runs SET status = 'finished', final_state = 'ERROR', error = 'interrupted: service restarted', finished_at = ? "
                "WHERE status IN ('queued', 'running')",
                (time.time(),),
            )
        return ids

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

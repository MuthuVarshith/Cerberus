"""
Persistent state for the GitHub App: deliveries and runs, in SQLite.

- Webhook deliveries are claimed atomically by X-GitHub-Delivery id, so a
  redelivered or duplicated webhook starts at most one run, across restarts.
- Every run has a row from queueing to its terminal state, which is the audit
  trail linking a GitHub event to a run.json artifact and a Check Run.
- Per-repository limits (runs per hour, concurrent runs) are read from here.
- Runs left `running` by a crashed process are marked ERROR on startup.
- Issue-triggered repair runs also get an `issue_runs` row that carries the
  human-review lifecycle (admitted -> approved -> pr_opened, or rejected), the
  recorded base SHA, the agent artifact path, the PR URL and the approver. The
  table is additive: missing columns are added in place, nothing is dropped.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
CREATE TABLE IF NOT EXISTS issue_runs (
    run_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS issue_runs_repo_issue ON issue_runs(repo, issue_number, created_at);
"""

# Nullable issue_runs columns, added one by one so an older database is migrated in place.
ISSUE_RUN_COLUMNS: Dict[str, str] = {
    "installation_id": "INTEGER",
    "default_branch": "TEXT",
    "base_sha": "TEXT",
    "requested_by": "TEXT",
    "source": "TEXT",             # github | api
    "final_status": "TEXT",       # the agent's FinalStatus
    "agent_run_path": "TEXT",
    "result_path": "TEXT",        # RepairResult JSON written by github.repair_job
    "comment_id": "INTEGER",      # the issue comment Cerberus keeps updated
    "check_run_id": "INTEGER",    # Check Run on the base commit
    "pr_url": "TEXT",
    "pr_branch": "TEXT",
    "approver": "TEXT",
    "rejected_by": "TEXT",
    "review_note": "TEXT",
    "error": "TEXT",
}

#: Review lifecycle of an issue-triggered repair run.
ISSUE_STATUSES = ("queued", "running", "admitted", "refused", "error", "approved", "rejected", "pr_opened")

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
            self._migrate_issue_runs()

    def _migrate_issue_runs(self) -> None:
        """Add any missing nullable issue_runs column (additive, idempotent)."""
        existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(issue_runs)").fetchall()}
        for name, kind in ISSUE_RUN_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE issue_runs ADD COLUMN {name} {kind}")

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

    def last_trigger_by(self, user: str) -> Optional[float]:
        """When `user` last started a run in any repository (per-user cooldown)."""
        with self._lock:
            row = self._conn.execute("SELECT MAX(created_at) FROM runs WHERE requested_by = ?", (user,)).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def recover_interrupted(self) -> List[str]:
        """Mark runs left active by a previous process as ERROR. Returns their ids."""
        with self._lock:
            rows = self._conn.execute("SELECT run_id FROM runs WHERE status IN ('queued', 'running')").fetchall()
            ids = [r["run_id"] for r in rows]
            now = time.time()
            self._conn.execute(
                "UPDATE runs SET status = 'finished', final_state = 'ERROR', error = 'interrupted: service restarted', finished_at = ? "
                "WHERE status IN ('queued', 'running')",
                (now,),
            )
            self._conn.execute(
                "UPDATE issue_runs SET status = 'error', error = 'interrupted: service restarted', updated_at = ? "
                "WHERE status IN ('queued', 'running')",
                (now,),
            )
        return ids

    # ------------------------------------------------------- issue runs

    def create_issue_run(self, run_id: str, repo: str, issue_number: int, **fields: Any) -> None:
        unknown = set(fields) - set(ISSUE_RUN_COLUMNS)
        if unknown:
            raise ValueError(f"unknown issue run fields: {sorted(unknown)}")
        now = time.time()
        columns = ["run_id", "repo", "issue_number", "status", "created_at", "updated_at", *fields]
        values = [run_id, repo, int(issue_number), "queued", now, now, *fields.values()]
        with self._lock:
            self._conn.execute(
                f"INSERT INTO issue_runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", values)

    def update_issue_run(self, run_id: str, **fields: Any) -> None:
        unknown = set(fields) - set(ISSUE_RUN_COLUMNS) - {"status"}
        if unknown:
            raise ValueError(f"unknown issue run fields: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in ISSUE_STATUSES:
            raise ValueError(f"unknown issue run status {fields['status']!r}")
        if not fields:
            return
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE issue_runs SET {assignments} WHERE run_id = ?", (*fields.values(), run_id))

    def transition_issue_run(self, run_id: str, from_statuses: Tuple[str, ...], to_status: str, **fields: Any) -> bool:
        """Atomically move a run to `to_status` only if it is in one of `from_statuses`."""
        unknown = set(fields) - set(ISSUE_RUN_COLUMNS)
        if unknown or to_status not in ISSUE_STATUSES:
            raise ValueError("invalid issue run transition")
        fields["updated_at"] = time.time()
        assignments = ", ".join(["status = ?", *(f"{k} = ?" for k in fields)])
        marks = ", ".join("?" for _ in from_statuses)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE issue_runs SET {assignments} WHERE run_id = ? AND status IN ({marks})",
                (to_status, *fields.values(), run_id, *from_statuses),
            )
            return cur.rowcount == 1

    def get_issue_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM issue_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest_issue_run(self, repo: str, issue_number: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM issue_runs WHERE repo = ? AND issue_number = ? ORDER BY created_at DESC LIMIT 1",
                (repo, int(issue_number)),
            ).fetchone()
        return dict(row) if row else None

    def installation_for_repo(self, repo: str) -> Optional[int]:
        """The installation id seen most recently for a repository (used by the REST API)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT installation_id FROM issue_runs WHERE repo = ? AND installation_id IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1", (repo,)).fetchone()
        return int(row[0]) if row else None

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

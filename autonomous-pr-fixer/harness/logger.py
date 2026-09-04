"""
Structured logging utilities for the Autonomous Repair Harness.

Emits JSON-structured log records with mandatory context fields:
  run_id, stage, status, timestamp, issue_id.

Secrets are NEVER passed to these helpers. Callers must sanitize inputs.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_root_logger = logging.getLogger("repair_harness")


class StructuredLogRecord:
    """Immutable structured log record as a JSON-serialisable dict."""

    def __init__(
        self,
        run_id: str,
        stage: str,
        status: str,
        issue_id: Optional[int] = None,
        message: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.data: Dict[str, Any] = {
            "run_id": run_id,
            "stage": stage,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "issue_id": issue_id,
            "message": message,
        }
        if extra:
            # Merge extra fields but never overwrite core fields
            for k, v in extra.items():
                if k not in self.data:
                    self.data[k] = v

    def to_json(self) -> str:
        return json.dumps(self.data)

    def __repr__(self) -> str:
        return self.to_json()


def log_event(
    run_id: str,
    stage: str,
    status: str,
    issue_id: Optional[int] = None,
    message: str = "",
    extra: Optional[Dict[str, Any]] = None,
    level: int = logging.INFO,
) -> StructuredLogRecord:
    """
    Emit a structured log event and return the record for persistence.

    Args:
        run_id: Unique identifier for this repair run.
        stage: Pipeline stage name (e.g., "TRIAGE", "REPRODUCTION").
        status: Outcome status (e.g., "STARTED", "PASS", "FAIL", "ERROR").
        issue_id: GitHub issue number, if applicable.
        message: Human-readable summary.
        extra: Additional structured fields (must not contain secrets).
        level: Python logging level.
    """
    record = StructuredLogRecord(run_id, stage, status, issue_id, message, extra)
    _root_logger.log(level, record.to_json())
    return record

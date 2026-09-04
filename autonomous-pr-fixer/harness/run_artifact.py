"""
Machine-readable run artifact generator.

Writes artifacts/<run_id>/run.json with full reproducibility metadata
conforming to the Section 7 schema.
"""
from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def write_run_artifact(
    run_id: str,
    issue_number: int,
    issue_title: str,
    pipeline_state_history: list,
    admission_decision: Dict[str, Any],
    regression_results: Optional[Dict[str, Any]] = None,
    blast_radius: Optional[Dict[str, Any]] = None,
    patch_attempts: int = 0,
    reached_green: bool = False,
    base_commit_sha: str = "unknown",
    model_config: Optional[Dict[str, Any]] = None,
    artifacts_dir: str = "artifacts",
) -> str:
    """
    Write a run.json artifact for this repair attempt.

    Returns the absolute path to the written file.
    """
    run_dir = os.path.join(artifacts_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)

    artifact: Dict[str, Any] = {
        # Identification
        "schema_version": "1.0",
        "run_id": run_id,
        "issue": {"number": issue_number, "title": issue_title},

        # Pipeline trace
        "pipeline_state_history": pipeline_state_history,
        "admission_decision": admission_decision,

        # Repair metrics
        "patch_attempts": patch_attempts,
        "reached_green": reached_green,

        # Reproducibility fields
        "reproducibility": {
            "base_commit_sha": base_commit_sha,
            "python_version": sys.version,
            "platform": platform.platform(),
            "model_config": model_config or {},
            "run_started_utc": datetime.now(timezone.utc).isoformat(),
        },

        # Test results
        "regression_results": regression_results or {},
        "blast_radius": blast_radius or {},
    }

    path = os.path.join(run_dir, "run.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2)
    return os.path.abspath(path)

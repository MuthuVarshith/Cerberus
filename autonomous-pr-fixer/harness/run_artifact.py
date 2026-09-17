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
    execution_mode: str = "local",
    patch_changed: bool = False,
    diff_hash: str = "",
    diff_files: Optional[list] = None,
    diff_lines_added: int = 0,
    diff_lines_deleted: int = 0,
    patch_verified_against_test: bool = False,
    github_integration_enabled: bool = False,
    pr_created: bool = False,
    pr_url: Optional[str] = None,
    admission_rejection_reason: Optional[str] = None,
    final_state: Optional[str] = None,
    reproduction_test_code: str = "",
    reproduction_output: str = "",
    diff_text: str = "",
    sections: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Write a run.json artifact for this repair attempt.

    Returns the absolute path to the written file.
    """
    run_dir = os.path.join(artifacts_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)

    artifact: Dict[str, Any] = {
        # Identification
        "schema_version": "1.3",
        "run_id": run_id,
        "issue": {"number": issue_number, "title": issue_title},
        "execution_mode": execution_mode,

        # Pipeline trace
        "final_state": final_state or (pipeline_state_history[-1] if pipeline_state_history else None),
        "pipeline_state_history": pipeline_state_history,
        "admission_decision": admission_decision,
        "admission_rejection_reason": admission_rejection_reason or admission_decision.get("rejection_state", ""),

        # Repair metrics & diff verification
        "patch_attempts": patch_attempts,
        "reached_green": reached_green,
        "patch_changed": patch_changed or admission_decision.get("patch_changed", False),
        "diff_hash": diff_hash or admission_decision.get("diff_hash", ""),
        "diff_files": diff_files or [],
        "diff_lines_added": diff_lines_added,
        "diff_lines_deleted": diff_lines_deleted,
        "patch_verified_against_test": patch_verified_against_test or reached_green,

        # GitHub Publishing Info (no fake/simulated URLs)
        "github_integration_enabled": github_integration_enabled,
        "pr_created": pr_created,
        "pr_url": pr_url,

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

        # Evidence: what was actually run and changed
        "evidence": {
            "reproduction_test_code": reproduction_test_code,
            "reproduction_output": reproduction_output,
            "diff": diff_text,
        },
    }

    # Gate-level evidence (RED runs, baseline comparison, scope, sandbox, refusal).
    for key, value in (sections or {}).items():
        if key not in artifact:
            artifact[key] = value

    path = os.path.join(run_dir, "run.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2)
    return os.path.abspath(path)

"""
Thin adapter from the repair agent to the existing gate.

Each submission is one unmodified `main.run_pipeline` call on the original repository at
its base commit, with the agent's diff as a `DiffPatchSource`. The verdict and every piece
of feedback come from that run's run.json; console output goes to a log file and is never
parsed. The adapter cannot admit anything itself.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from agents.patch_sources import DiffPatchSource
from repair.state import GateFeedback, GateStatus

# Fixed guidance per refusal code. Not model output, so it cannot be steered by the repository.
NEXT_ACTION = {
    "GREEN_NOT_REACHED": "The reproduction test still fails with your patch. Re-read the failure detail, run the reproduction test yourself, and fix the root cause.",
    "GREEN_NONDETERMINISTIC": "The reproduction test passed only on some runs. Remove the source of nondeterminism in your change.",
    "REPRODUCTION_TEST_TAMPERED": "The reproduction test was changed. Leave it untouched and fix the source code.",
    "REGRESSION": "Your patch breaks tests that passed before. Read the failing tests listed and keep their behaviour while fixing the bug.",
    "REGRESSION_UNVERIFIABLE": "The test suite produced no readable results with your patch (import error or crash?). Run the suite's tests and fix the error.",
    "SCOPE_VIOLATION": "Your patch changes files or lines outside the allowed scope. Restrict the change to the allowed files and keep it minimal.",
    "EMPTY_PATCH": "The submitted diff makes no substantive change. Edit the source before submitting.",
    "PATCH_APPLY_FAILED": "The diff did not apply to the base commit. Use reset_workspace and edit_file, then check git_diff before submitting.",
}


def _excerpt(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def feedback_from_run_json(artifact: Dict[str, Any], attempt: int) -> GateFeedback:
    """Concise, structured feedback from one gate run.json."""
    state = artifact.get("final_state")
    refusal = artifact.get("refusal") or {}
    regression = artifact.get("regression_results") or {}
    scope = artifact.get("blast_radius") or {}
    history = artifact.get("patch_history") or []
    code = refusal.get("code")
    stage = refusal.get("stage")
    message = refusal.get("message") or artifact.get("admission_rejection_reason") or ""

    failed_tests: List[str] = []
    detail = ""
    if code == "REGRESSION":
        failed_tests = list(regression.get("newly_failing") or []) + [
            f"{t} (no longer runs)" for t in regression.get("missing_tests") or []]
    elif code in ("GREEN_NOT_REACHED", "GREEN_NONDETERMINISTIC"):
        red = artifact.get("red_gate") or {}
        failed_tests = list(red.get("failing_test_ids") or [])
        real = [h for h in history if h.get("status") != "ABORT_DUPLICATE_DIFF"]
        if real:
            detail = _excerpt(real[-1].get("detail", ""))
            if real[-1].get("applied") is False:
                return GateFeedback(
                    attempt=attempt, status=GateStatus.REFUSED, stage=stage, failure_code=code,
                    message=_excerpt(message, 800), failed_tests=failed_tests, detail=detail, patch_applied=False,
                    suggested_next_action=NEXT_ACTION["PATCH_APPLY_FAILED"],
                )
    elif code == "SCOPE_VIOLATION":
        detail = json.dumps({k: scope.get(k) for k in ("unauthorized_files", "modified_test_files", "lines_added", "lines_deleted")
                             if scope.get(k) not in (None, [], "")})
    elif code == "REGRESSION_UNVERIFIABLE":
        detail = _excerpt(regression.get("error_message") or "")

    if state == "ADMITTED":
        status = GateStatus.ADMITTED
    elif state == "REFUSED":
        status = GateStatus.REFUSED
    else:
        status = GateStatus.GATE_ERROR

    return GateFeedback(
        attempt=attempt,
        status=status,
        stage=stage,
        failure_code=code if status != GateStatus.ADMITTED else None,
        message=_excerpt(message, 800),
        failed_tests=failed_tests[:20],
        changed_files=list(scope.get("changed_files") or artifact.get("diff_files") or []),
        allowed_files=list(scope.get("allowed_files") or []),
        lines_added=int(artifact.get("diff_lines_added") or 0),
        lines_deleted=int(artifact.get("diff_lines_deleted") or 0),
        detail=detail,
        suggested_next_action=NEXT_ACTION.get(code or "", "") if status == GateStatus.REFUSED else "",
    )


class CerberusGate:
    """Runs the existing pipeline on one candidate diff."""

    def __init__(self, repo_dir: str, issue_number: int, issue_title: str, issue_body: str,
                 reproduction_test: str, base_ref: Optional[str], artifacts_dir: str,
                 isolation: Optional[str] = None):
        self.repo_dir = repo_dir
        self.issue_number = issue_number
        self.issue_title = issue_title
        self.issue_body = issue_body
        self.reproduction_test = reproduction_test
        self.base_ref = base_ref
        self.artifacts_dir = artifacts_dir
        self.isolation = isolation

    def verify(self, diff: str, attempt: int, run_id: str) -> GateFeedback:
        import main  # the gate; imported lazily so the package can be used without the CLI

        os.makedirs(self.artifacts_dir, exist_ok=True)
        log_path = os.path.join(self.artifacts_dir, f"{run_id}.log")
        previous = os.environ.get("RUN_ARTIFACTS_DIR")
        os.environ["RUN_ARTIFACTS_DIR"] = self.artifacts_dir
        started = time.monotonic()
        root_handlers = list(logging.root.handlers)
        try:
            with open(log_path, "w", encoding="utf-8") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                main.run_pipeline(
                    repo_dir=self.repo_dir,
                    issue_number=self.issue_number,
                    issue_title=self.issue_title,
                    issue_body=self.issue_body,
                    repro_test_code=self.reproduction_test,
                    patch_source=DiffPatchSource([diff], name="cerberus-repair-agent"),
                    sandbox_isolation=self.isolation,
                    base_ref=self.base_ref,
                    run_id=run_id,
                )
        except Exception:
            pass  # run_pipeline records ERROR in run.json before re-raising
        finally:
            # The gate may configure logging while stdout/stderr point at the (now closed) log file.
            for h in list(logging.root.handlers):
                if h not in root_handlers:
                    logging.root.removeHandler(h)
            if previous is None:
                os.environ.pop("RUN_ARTIFACTS_DIR", None)
            else:
                os.environ["RUN_ARTIFACTS_DIR"] = previous
        duration = round(time.monotonic() - started, 2)
        path = os.path.join(self.artifacts_dir, run_id, "run.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                artifact = json.load(f)
        except (OSError, ValueError):
            return GateFeedback(attempt=attempt, status=GateStatus.GATE_ERROR, message="The gate wrote no readable run.json.",
                                gate_run_id=run_id, duration_seconds=duration)
        fb = feedback_from_run_json(artifact, attempt)
        fb.gate_run_id = run_id
        fb.run_json_path = path
        fb.duration_seconds = duration
        return fb

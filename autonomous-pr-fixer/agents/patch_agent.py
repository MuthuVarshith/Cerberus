"""
Patch Agent with Minimal Patch Policy and Structured Failure Feedback Loop.
Generates unified diffs, applies them safely in the sandbox, tracks changed lines
and symbols, and feeds structured JSON failure feedback back into iterative retries (max 5).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional
from harness.diff_utils import DiffUtils, PatchApplicationResult
from harness.docker_sandbox import Sandbox
from harness.tools import ACI
from harness.config import patch_policy_defaults


@dataclass
class StructuredFailure:
    attempt: int
    status: str
    exit_code: int
    error: str
    traceback: str
    failing_test: str
    changed_files: List[str]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


@dataclass
class PatchAttemptResult:
    attempt: int
    diff_text: str
    applied_successfully: bool
    reproduction_test_passed: bool
    stdout: str
    stderr: str
    structured_failure: Optional[StructuredFailure] = None
    lines_added: int = 0
    lines_deleted: int = 0
    changed_files: List[str] = field(default_factory=list)


@dataclass
class PatchLoopResult:
    reached_green: bool
    total_attempts: int
    winning_diff: str
    history: List[PatchAttemptResult]
    total_lines_changed: int = 0
    final_changed_files: List[str] = field(default_factory=list)
    diff_hash: str = ""
    is_empty_diff: bool = False


class PatchAgent:
    """Orchestrates iterative self-healing patch generation and verification."""

    def __init__(
        self,
        sandbox: Sandbox,
        max_attempts: Optional[int] = None,
        max_lines_changed: Optional[int] = None,
    ):
        """Explicit arguments win; anything left unset comes from PATCH_MAX_* env vars.

        The ablation study passes attempt budgets deliberately, so configuration
        fills in only what a caller did not choose.
        """
        env_attempts, env_lines = patch_policy_defaults()
        self.sandbox = sandbox
        self.aci = ACI(sandbox)
        self.max_attempts = env_attempts if max_attempts is None else max_attempts
        self.max_lines_changed = env_lines if max_lines_changed is None else max_lines_changed

    def run_patch_loop(
        self,
        candidate_files: List[str],
        patch_generator_fn: Callable[[int, str], str],
        test_command: Optional[str] = None,
    ) -> PatchLoopResult:
        if test_command is None:
            test_command = f"{self.sandbox.python_cmd} -m pytest test_reproduce.py"
        history: List[PatchAttemptResult] = []
        seen_diffs: set[str] = set()
        feedback_str = "Initial attempt: please provide a minimal unified diff fixing the bug."

        for attempt in range(1, self.max_attempts + 1):
            candidate_diff = patch_generator_fn(attempt, feedback_str)

            # Check for minimal patch constraints before applying
            lines_added, lines_deleted = self._count_diff_lines(candidate_diff)
            total_lines = lines_added + lines_deleted
            targeted_files = DiffUtils.parse_targeted_files(candidate_diff)

            # Detect repeated identical patch to prevent infinite thrashing
            clean_diff = candidate_diff.strip()
            if clean_diff in seen_diffs:
                fail_info = StructuredFailure(
                    attempt=attempt,
                    status="ABORT_DUPLICATE_DIFF",
                    exit_code=1,
                    error="RepeatedIdenticalPatch",
                    traceback="Agent produced an identical diff to a prior attempt. Aborting early.",
                    failing_test="N/A",
                    changed_files=targeted_files,
                )
                history.append(
                    PatchAttemptResult(
                        attempt=attempt,
                        diff_text=candidate_diff,
                        applied_successfully=False,
                        reproduction_test_passed=False,
                        stdout="",
                        stderr="RepeatedIdenticalPatch",
                        structured_failure=fail_info,
                        lines_added=lines_added,
                        lines_deleted=lines_deleted,
                        changed_files=targeted_files,
                    )
                )
                break
            seen_diffs.add(clean_diff)

            if total_lines > self.max_lines_changed:
                # Reject patch violating minimal patch policy
                fail_info = StructuredFailure(
                    attempt=attempt,
                    status="REJECTED_BY_POLICY",
                    exit_code=1,
                    error="MinimalPatchPolicyViolation",
                    traceback=f"Patch changed {total_lines} lines (limit: {self.max_lines_changed}). Rejecting overly broad diff.",
                    failing_test="N/A",
                    changed_files=targeted_files,
                )
                history.append(
                    PatchAttemptResult(
                        attempt=attempt,
                        diff_text=candidate_diff,
                        applied_successfully=False,
                        reproduction_test_passed=False,
                        stdout="",
                        stderr="MinimalPatchPolicyViolation",
                        structured_failure=fail_info,
                        lines_added=lines_added,
                        lines_deleted=lines_deleted,
                        changed_files=targeted_files,
                    )
                )
                feedback_str = f"Attempt #{attempt} violated Minimal Patch Policy: {fail_info.traceback}"
                continue

            # Apply diff
            apply_res = DiffUtils.apply_diff_to_sandbox(self.sandbox, candidate_diff)
            if not apply_res.success:
                fail_info = StructuredFailure(
                    attempt=attempt,
                    status="APPLY_ERROR",
                    exit_code=1,
                    error="GitApplyFailed",
                    traceback=apply_res.error,
                    failing_test="N/A",
                    changed_files=targeted_files,
                )
                history.append(
                    PatchAttemptResult(
                        attempt=attempt,
                        diff_text=candidate_diff,
                        applied_successfully=False,
                        reproduction_test_passed=False,
                        stdout="",
                        stderr=apply_res.error,
                        structured_failure=fail_info,
                        lines_added=lines_added,
                        lines_deleted=lines_deleted,
                        changed_files=targeted_files,
                    )
                )
                feedback_str = f"Attempt #{attempt} failed to apply cleanly:\n{apply_res.error}"
                continue

            # Run target reproduction test
            test_exec = self.sandbox.exec(test_command, timeout=30)
            if test_exec.exit_code == 0:
                # Target test PASSED (GREEN)
                history.append(
                    PatchAttemptResult(
                        attempt=attempt,
                        diff_text=candidate_diff,
                        applied_successfully=True,
                        reproduction_test_passed=True,
                        stdout=test_exec.stdout,
                        stderr=test_exec.stderr,
                        lines_added=lines_added,
                        lines_deleted=lines_deleted,
                        changed_files=targeted_files,
                    )
                )
                diff_h = hashlib.sha256(candidate_diff.strip().encode("utf-8")).hexdigest() if candidate_diff.strip() else ""
                is_empty = (total_lines == 0) or not candidate_diff.strip() or len(targeted_files) == 0
                return PatchLoopResult(
                    reached_green=True,
                    total_attempts=attempt,
                    winning_diff=candidate_diff,
                    history=history,
                    total_lines_changed=total_lines,
                    final_changed_files=targeted_files,
                    diff_hash=diff_h,
                    is_empty_diff=is_empty,
                )
            else:
                # Target test FAILED -> parse structured failure and rollback
                DiffUtils.rollback(self.sandbox, candidate_files)
                fail_info = self._parse_structured_failure(attempt, test_exec.stdout, test_exec.stderr, targeted_files)
                history.append(
                    PatchAttemptResult(
                        attempt=attempt,
                        diff_text=candidate_diff,
                        applied_successfully=True,
                        reproduction_test_passed=False,
                        stdout=test_exec.stdout,
                        stderr=test_exec.stderr,
                        structured_failure=fail_info,
                        lines_added=lines_added,
                        lines_deleted=lines_deleted,
                        changed_files=targeted_files,
                    )
                )
                feedback_str = f"Attempt #{attempt} failed with structured feedback:\n{fail_info.to_json()}"

        return PatchLoopResult(
            reached_green=False,
            # Attempts actually made, which is fewer than the budget when the loop
            # aborted early (duplicate diff). Reporting the budget here inflated
            # the attempt counts in the evaluation metrics.
            total_attempts=len(history) or self.max_attempts,
            winning_diff="",
            history=history,
        )

    def _count_diff_lines(self, diff_text: str) -> tuple[int, int]:
        added = 0
        deleted = 0
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted += 1
        return added, deleted

    def _parse_structured_failure(
        self,
        attempt: int,
        stdout: str,
        stderr: str,
        changed_files: List[str],
    ) -> StructuredFailure:
        raw = stderr if stderr.strip() else stdout
        # Extract error type
        err_match = re.search(r"([A-Z][a-zA-Z0-9]*(?:Error|Exception)):(.*)", raw)
        error_type = err_match.group(1) if err_match else "AssertionError"

        # Extract failing test name
        test_match = re.search(r"FAILED\s+([^\s:]+)", raw)
        failing_test = test_match.group(1) if test_match else "test_reproduce.py"

        # Extract last 20 lines of traceback
        tb_lines = raw.strip().splitlines()[-25:]
        traceback_snippet = "\n".join(tb_lines)

        return StructuredFailure(
            attempt=attempt,
            status="FAIL",
            exit_code=1,
            error=error_type,
            traceback=traceback_snippet,
            failing_test=failing_test,
            changed_files=changed_files,
        )

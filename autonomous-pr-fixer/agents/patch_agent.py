"""
Patch loop: apply candidate patches until one passes the GREEN check.

The loop does not decide what a patch is or where it comes from; it receives a
`patch_generator_fn(attempt, feedback) -> diff` and a `verifier() -> (passed,
output)`. Each attempt is checked against the minimal-patch policy, applied
with `git apply`, verified, and rolled back to the base commit if it fails.
Structured failure information is fed back to the generator on the next attempt.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional, Tuple

from harness.config import patch_policy_defaults
from harness.diff_utils import DiffUtils
from harness.docker_sandbox import HARNESS_DIR, Sandbox

Verifier = Callable[[], Tuple[bool, str]]


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


def command_verifier(sandbox: Sandbox, test_command: str, timeout: int = 60) -> Verifier:
    """A verifier that passes when a command exits 0. Used by fixtures without RED metadata."""
    if not test_command or not test_command.strip():
        # GREEN is a claim about a test run. Without a command there is no run,
        # so there is nothing that could make the claim true.
        raise ValueError("A non-empty test command is required; GREEN cannot be established without one.")

    def _verify() -> Tuple[bool, str]:
        res = sandbox.exec(test_command, timeout=timeout)
        return res.exit_code == 0, res.output

    return _verify


class PatchAgent:
    """Runs the bounded patch → verify → rollback loop."""

    def __init__(
        self,
        sandbox: Sandbox,
        max_attempts: Optional[int] = None,
        max_lines_changed: Optional[int] = None,
    ):
        """Explicit arguments win; anything left unset comes from PATCH_MAX_* env vars."""
        env_attempts, env_lines = patch_policy_defaults()
        self.sandbox = sandbox
        self.max_attempts = env_attempts if max_attempts is None else max_attempts
        self.max_lines_changed = env_lines if max_lines_changed is None else max_lines_changed

    def run_patch_loop(
        self,
        candidate_files: List[str],
        patch_generator_fn: Callable[[int, str], str],
        test_command: Optional[str] = None,
        verifier: Optional[Verifier] = None,
    ) -> PatchLoopResult:
        if verifier is None:
            if test_command is None:
                test_command = f"{self.sandbox.python_cmd} -m pytest -p no:cacheprovider {HARNESS_DIR}/test_reproduce.py"
            if not test_command.strip():
                raise ValueError("run_patch_loop requires a non-empty test command; GREEN cannot be established without one.")
            verifier = command_verifier(self.sandbox, test_command)

        history: List[PatchAttemptResult] = []
        seen_diffs: set[str] = set()
        feedback_str = "Initial attempt: please provide a minimal unified diff fixing the bug."

        for attempt in range(1, self.max_attempts + 1):
            candidate_diff = patch_generator_fn(attempt, feedback_str) or ""
            lines_added, lines_deleted = self._count_diff_lines(candidate_diff)
            total_lines = lines_added + lines_deleted
            targeted_files = DiffUtils.parse_targeted_files(candidate_diff)

            def _record(status: str, error: str, detail: str, applied: bool = False, passed: bool = False,
                        stdout: str = "", failure: Optional[StructuredFailure] = None) -> StructuredFailure:
                info = failure or StructuredFailure(
                    attempt=attempt, status=status, exit_code=1, error=error,
                    traceback=detail, failing_test="N/A", changed_files=targeted_files,
                )
                history.append(PatchAttemptResult(
                    attempt=attempt, diff_text=candidate_diff, applied_successfully=applied,
                    reproduction_test_passed=passed, stdout=stdout, stderr=error,
                    structured_failure=info, lines_added=lines_added, lines_deleted=lines_deleted,
                    changed_files=targeted_files,
                ))
                return info

            clean_diff = candidate_diff.strip()
            if clean_diff in seen_diffs:
                _record("ABORT_DUPLICATE_DIFF", "RepeatedIdenticalPatch",
                        "The generator produced a diff identical to a prior attempt. Aborting early.")
                break
            seen_diffs.add(clean_diff)

            if not clean_diff:
                _record("EMPTY_DIFF", "EmptyPatch", "The generator returned no diff.")
                feedback_str = f"Attempt #{attempt} returned an empty diff."
                continue

            if total_lines > self.max_lines_changed:
                info = _record("REJECTED_BY_POLICY", "MinimalPatchPolicyViolation",
                               f"Patch changed {total_lines} lines (limit: {self.max_lines_changed}).")
                feedback_str = f"Attempt #{attempt} violated the minimal patch policy: {info.traceback}"
                continue

            apply_res = DiffUtils.apply_diff_to_sandbox(self.sandbox, candidate_diff)
            if not apply_res.success:
                _record("APPLY_ERROR", "GitApplyFailed", apply_res.error)
                DiffUtils.rollback(self.sandbox)
                feedback_str = f"Attempt #{attempt} failed to apply cleanly:\n{apply_res.error}"
                continue

            passed, output = verifier()
            if passed:
                history.append(PatchAttemptResult(
                    attempt=attempt, diff_text=candidate_diff, applied_successfully=True,
                    reproduction_test_passed=True, stdout=output, stderr="",
                    lines_added=lines_added, lines_deleted=lines_deleted, changed_files=targeted_files,
                ))
                return PatchLoopResult(
                    reached_green=True,
                    total_attempts=attempt,
                    winning_diff=candidate_diff,
                    history=history,
                    total_lines_changed=total_lines,
                    final_changed_files=targeted_files,
                    diff_hash=hashlib.sha256(clean_diff.encode("utf-8")).hexdigest(),
                    is_empty_diff=total_lines == 0 or not targeted_files,
                )

            DiffUtils.rollback(self.sandbox)
            failure = self._parse_structured_failure(attempt, output, targeted_files)
            _record(failure.status, failure.error, failure.traceback, applied=True, stdout=output, failure=failure)
            feedback_str = f"Attempt #{attempt} failed with structured feedback:\n{failure.to_json()}"

        return PatchLoopResult(
            reached_green=False,
            # Attempts actually made, which is fewer than the budget when the loop
            # aborted early; the duplicate-diff record itself is not an attempt.
            total_attempts=sum(
                1 for h in history
                if not (h.structured_failure and h.structured_failure.status == "ABORT_DUPLICATE_DIFF")
            ),
            winning_diff="",
            history=history,
        )

    def _count_diff_lines(self, diff_text: str) -> Tuple[int, int]:
        added = 0
        deleted = 0
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted += 1
        return added, deleted

    def _parse_structured_failure(self, attempt: int, output: str, changed_files: List[str]) -> StructuredFailure:
        err_match = re.search(r"([A-Z][a-zA-Z0-9]*(?:Error|Exception)):(.*)", output)
        error_type = err_match.group(1) if err_match else "AssertionError"
        test_match = re.search(r"FAILED\s+([^\s:]+)", output)
        failing_test = test_match.group(1) if test_match else "reproduction test"
        tb_lines = output.strip().splitlines()[-25:]
        return StructuredFailure(
            attempt=attempt,
            status="FAIL",
            exit_code=1,
            error=error_type,
            traceback="\n".join(tb_lines),
            failing_test=failing_test,
            changed_files=changed_files,
        )

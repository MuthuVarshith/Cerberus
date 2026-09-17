"""
Admission controller: the programmatic decision on PR eligibility.

  Gate 1 GREEN:      the candidate patch made the validated reproduction test pass
  Gate 2 Regression: no test newly fails or disappears relative to the baseline,
                     and the results were machine-readable
  Gate 3 Scope:      changed files are inside the allowed scope and within limits
  Gate 4 Real diff:  the workspace differs from the base commit by non-whitespace content

admit = gate_1 and gate_2 and gate_3 and gate_4. RED is a precondition checked
before a patch is ever requested; a run without it never reaches this point.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

from agents.patch_agent import PatchLoopResult
from agents.regression_agent import RegressionReport
from harness.pipeline_state import RefusalCode
from harness.scope_gate import ScopeReport


@dataclass
class AdmissionDecision:
    approved: bool
    reasons: List[str]
    rejection_summary: str = ""
    gate_1_target_passed: bool = False
    gate_2_regression_passed: bool = False
    gate_3_scope_passed: bool = False
    gate_4_patch_changed: bool = False
    diff_stat: str = ""
    diff_hash: str = ""
    rejection_state: str = ""

    @property
    def admit_pr(self) -> bool:
        return self.approved

    @property
    def target_test_passed(self) -> bool:
        return self.gate_1_target_passed

    @property
    def regression_passed(self) -> bool:
        return self.gate_2_regression_passed

    @property
    def scope_acceptable(self) -> bool:
        return self.gate_3_scope_passed

    @property
    def patch_changed(self) -> bool:
        return self.gate_4_patch_changed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "admit_pr": self.approved,
            "target_test_passed": self.gate_1_target_passed,
            "regression_passed": self.gate_2_regression_passed,
            "scope_acceptable": self.gate_3_scope_passed,
            "patch_changed": self.gate_4_patch_changed,
            "diff_stat": self.diff_stat,
            "diff_hash": self.diff_hash,
            "rejection_state": self.rejection_state,
            "reasons": self.reasons,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def summary_markdown(self) -> str:
        status_icon = "APPROVED" if self.approved else "REJECTED"
        lines = [f"### Admission Decision: **{status_icon}**"]
        for r in self.reasons:
            lines.append(f"- {r}")
        if not self.approved:
            lines.append(f"\n> **Admission Block Reason:** {self.rejection_summary}")
        return "\n".join(lines)


def _has_substantive_change(diff_text: str) -> bool:
    for line in diff_text.splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---")):
            if line[1:].strip():
                return True
    return False


class AdmissionController:
    """Evaluates a candidate patch against the four admission gates."""

    @staticmethod
    def evaluate(
        patch_result: PatchLoopResult,
        regression_result: RegressionReport,
        scope_result: ScopeReport,
    ) -> AdmissionDecision:
        reasons: List[str] = []
        rejections: List[str] = []
        rejection_state = ""

        gate_1 = patch_result.reached_green
        if gate_1:
            reasons.append(f"[GATE 1: PASS] Reproduction test passes with the patch (attempt {patch_result.total_attempts}).")
        else:
            msg = f"[GATE 1: FAIL] Reproduction test did not pass after {patch_result.total_attempts} attempt(s)."
            reasons.append(msg)
            rejections.append(msg)
            rejection_state = RefusalCode.GREEN_NOT_REACHED.value

        gate_2 = regression_result.regression_free
        if gate_2:
            pre = len(regression_result.preexisting_failures)
            reasons.append(
                f"[GATE 2: PASS] No new test failures relative to baseline "
                f"({regression_result.passed_count}/{regression_result.total_tests} passing"
                f"{f', {pre} failing at baseline too' if pre else ''})."
            )
        else:
            if not regression_result.results_parsed:
                detail = f"test results were not machine-readable ({regression_result.error_message})"
                code = RefusalCode.REGRESSION_UNVERIFIABLE
            else:
                parts = []
                if regression_result.newly_failing:
                    parts.append(f"newly failing: {', '.join(regression_result.newly_failing)}")
                if regression_result.missing_tests:
                    parts.append(f"no longer run: {', '.join(regression_result.missing_tests)}")
                detail = "; ".join(parts)
                code = RefusalCode.REGRESSION
            msg = f"[GATE 2: FAIL] Regression gate: {detail}."
            reasons.append(msg)
            rejections.append(msg)
            rejection_state = rejection_state or code.value

        gate_3 = scope_result.is_acceptable
        if gate_3:
            reasons.append(
                f"[GATE 3: PASS] Scope acceptable: {len(scope_result.changed_files)} file(s) changed "
                f"(+{scope_result.lines_added}/-{scope_result.lines_deleted}), none outside the allowed scope."
            )
        else:
            msg = f"[GATE 3: FAIL] Scope violation: {scope_result.violation_reason}."
            reasons.append(msg)
            rejections.append(msg)
            rejection_state = rejection_state or RefusalCode.SCOPE_VIOLATION.value

        diff_stat = f"+{scope_result.lines_added}/-{scope_result.lines_deleted}"
        gate_4 = bool(scope_result.changed_files) and _has_substantive_change(scope_result.diff_text)
        if gate_4:
            reasons.append(f"[GATE 4: PASS] Non-empty change against the base commit ({diff_stat}).")
        else:
            msg = "[GATE 4: FAIL] The workspace has no non-whitespace change against the base commit."
            reasons.append(msg)
            rejections.append(msg)
            rejection_state = rejection_state or RefusalCode.EMPTY_PATCH.value

        approved = gate_1 and gate_2 and gate_3 and gate_4
        return AdmissionDecision(
            approved=approved,
            reasons=reasons,
            rejection_summary="; ".join(rejections) if rejections else "All 4 verification gates passed.",
            gate_1_target_passed=gate_1,
            gate_2_regression_passed=gate_2,
            gate_3_scope_passed=gate_3,
            gate_4_patch_changed=gate_4,
            diff_stat=diff_stat,
            diff_hash=getattr(patch_result, "diff_hash", "") or "",
            rejection_state="" if approved else rejection_state,
        )

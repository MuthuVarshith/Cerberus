"""
Patch Admission Controller.
The authoritative programmatic gatekeeper that determines PR eligibility.
Evaluates:
- Gate 1: Target Verification (Reproduction test = PASS)
- Gate 2: Regression Verification (Regression suite = PASS, 0 regressions)
- Gate 3: Structural Safety (Structural blast radius = ACCEPTABLE)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional
from agents.patch_agent import PatchLoopResult
from agents.regression_agent import RegressionReport


@dataclass
class AdmissionDecision:
    approved: bool
    reasons: List[str]
    rejection_summary: str = ""
    gate_1_target_passed: bool = False
    gate_2_regression_passed: bool = False
    gate_3_blast_radius_passed: bool = False

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
    def blast_radius_acceptable(self) -> bool:
        return self.gate_3_blast_radius_passed

    def to_dict(self) -> Dict[str, Any]:
        """Conforms to Section 16 structured JSON schema."""
        return {
            "admit_pr": self.approved,
            "target_test_passed": self.gate_1_target_passed,
            "regression_passed": self.gate_2_regression_passed,
            "blast_radius_acceptable": self.gate_3_blast_radius_passed,
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


class AdmissionController:
    """Evaluates patch candidate against the three strict verification gates."""

    @staticmethod
    def evaluate(
        patch_result: PatchLoopResult,
        regression_result: RegressionReport,
    ) -> AdmissionDecision:
        reasons: List[str] = []
        rejections: List[str] = []

        # Gate 1: Target Verification
        gate_1 = patch_result.reached_green
        if gate_1:
            reasons.append(
                f"[GATE 1: PASS] Target reproduction test passed (resolved in {patch_result.total_attempts} iteration(s))."
            )
        else:
            msg = f"[GATE 1: FAIL] Target reproduction test failed after {patch_result.total_attempts} attempt(s)."
            reasons.append(msg)
            rejections.append(msg)

        # Gate 2: Regression Verification
        gate_2 = regression_result.all_tests_passed and (regression_result.failed_count == 0)
        if gate_2:
            reasons.append(
                f"[GATE 2: PASS] Full regression test suite passed cleanly "
                f"({regression_result.passed_count}/{regression_result.total_tests} passed in {regression_result.execution_time_sec}s)."
            )
        else:
            msg = (
                f"[GATE 2: FAIL] Regression detected in existing test suite "
                f"({regression_result.failed_count} failed, {regression_result.error_count} errors)."
            )
            reasons.append(msg)
            rejections.append(msg)

        # Gate 3: Structural Safety (Blast Radius)
        blast = regression_result.blast_radius
        gate_3 = blast.is_acceptable
        if gate_3:
            reasons.append(
                f"[GATE 3: PASS] Structural blast radius acceptable: {len(blast.observed_files)} file(s) changed "
                f"(+{blast.lines_added}/-{blast.lines_deleted}), 0 unauthorized modifications."
            )
        else:
            msg = f"[GATE 3: FAIL] Blast radius violation: {blast.scope_violation_reason or 'unauthorized modifications'}."
            reasons.append(msg)
            rejections.append(msg)

        # Programmatic conjunction: all 3 must pass
        admit_pr = gate_1 and gate_2 and gate_3
        summary = "; ".join(rejections) if rejections else "All 3 verification gates passed."

        return AdmissionDecision(
            approved=admit_pr,
            reasons=reasons,
            rejection_summary=summary,
            gate_1_target_passed=gate_1,
            gate_2_regression_passed=gate_2,
            gate_3_blast_radius_passed=gate_3,
        )

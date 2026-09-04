"""
Tests for Patch Admission Controller conforming to Section 17 & 27.
Verifies the strict conjunction:
admit_pr = target_test_passed and regression_passed and blast_radius_acceptable
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.admission_controller import AdmissionController
from agents.patch_agent import PatchLoopResult
from agents.regression_agent import RegressionReport, StructuralBlastRadius


def test_admission_approves_when_all_gates_pass():
    patch_res = PatchLoopResult(reached_green=True, total_attempts=1, winning_diff="", history=[])
    blast = StructuralBlastRadius(expected_files=["calc.py"], observed_files=["calc.py"], unauthorized_files=[], lines_added=2, lines_deleted=1, is_acceptable=True)
    regr = RegressionReport(all_tests_passed=True, total_tests=5, passed_count=5, failed_count=0, skipped_count=0, error_count=0, execution_time_sec=0.5, raw_output="5 passed", blast_radius=blast)

    decision = AdmissionController.evaluate(patch_res, regr)
    assert decision.approved is True
    assert decision.gate_1_target_passed is True
    assert decision.gate_2_regression_passed is True
    assert decision.gate_3_blast_radius_passed is True
    assert "APPROVED" in decision.summary_markdown()


def test_admission_rejects_when_regression_fails():
    patch_res = PatchLoopResult(reached_green=True, total_attempts=2, winning_diff="", history=[])
    blast = StructuralBlastRadius(expected_files=["calc.py"], observed_files=["calc.py"], unauthorized_files=[], lines_added=2, lines_deleted=1, is_acceptable=True)
    regr = RegressionReport(all_tests_passed=False, total_tests=5, passed_count=4, failed_count=1, skipped_count=0, error_count=0, execution_time_sec=0.5, raw_output="1 failed", blast_radius=blast)

    decision = AdmissionController.evaluate(patch_res, regr)
    assert decision.approved is False
    assert decision.gate_2_regression_passed is False
    assert "Regression detected" in decision.rejection_summary


def test_admission_rejects_when_blast_radius_violated():
    patch_res = PatchLoopResult(reached_green=True, total_attempts=1, winning_diff="", history=[])
    blast = StructuralBlastRadius(expected_files=["calc.py"], observed_files=["calc.py", "vault.py"], unauthorized_files=["vault.py"], lines_added=10, lines_deleted=0, is_acceptable=False, scope_violation_reason="Unauthorized file touched: vault.py")
    regr = RegressionReport(all_tests_passed=True, total_tests=5, passed_count=5, failed_count=0, skipped_count=0, error_count=0, execution_time_sec=0.5, raw_output="5 passed", blast_radius=blast)

    decision = AdmissionController.evaluate(patch_res, regr)
    assert decision.approved is False
    assert decision.gate_3_blast_radius_passed is False
    assert "Blast radius violation" in decision.rejection_summary


def test_admission_rejects_when_target_test_fails():
    """Gate 1: If reproduction target never reached GREEN, admission is blocked."""
    patch_res = PatchLoopResult(reached_green=False, total_attempts=5, winning_diff="", history=[])
    blast = StructuralBlastRadius(expected_files=["calc.py"], observed_files=["calc.py"], unauthorized_files=[], lines_added=2, lines_deleted=1, is_acceptable=True)
    regr = RegressionReport(all_tests_passed=True, total_tests=5, passed_count=5, failed_count=0, skipped_count=0, error_count=0, execution_time_sec=0.5, raw_output="5 passed", blast_radius=blast)

    decision = AdmissionController.evaluate(patch_res, regr)
    assert decision.approved is False
    assert decision.gate_1_target_passed is False
    assert "Target reproduction test failed" in decision.rejection_summary


def test_admission_rejects_when_multiple_gates_fail():
    """All failed gate reasons are collected into the rejection summary."""
    patch_res = PatchLoopResult(reached_green=False, total_attempts=5, winning_diff="", history=[])
    blast = StructuralBlastRadius(expected_files=["calc.py"], observed_files=["calc.py", "secret.py"], unauthorized_files=["secret.py"], lines_added=5, lines_deleted=0, is_acceptable=False, scope_violation_reason="Unauthorized: secret.py")
    regr = RegressionReport(all_tests_passed=False, total_tests=5, passed_count=3, failed_count=2, skipped_count=0, error_count=0, execution_time_sec=0.5, raw_output="2 failed", blast_radius=blast)

    decision = AdmissionController.evaluate(patch_res, regr)
    assert decision.approved is False
    assert decision.gate_1_target_passed is False
    assert decision.gate_2_regression_passed is False
    assert decision.gate_3_blast_radius_passed is False
    # All 3 reasons present in rejection summary
    assert "GATE 1: FAIL" in decision.rejection_summary
    assert "GATE 2: FAIL" in decision.rejection_summary
    assert "GATE 3: FAIL" in decision.rejection_summary


def test_agent_cannot_force_pr_approval_when_gate_fails():
    """
    Demonstrates that the LLM or an agent cannot force PR approval.
    The Admission Controller is a deterministic programmatic boolean conjunction:
    admit_pr = gate_1 and gate_2 and gate_3.
    """
    patch_res = PatchLoopResult(reached_green=False, total_attempts=1, winning_diff="", history=[])
    blast = StructuralBlastRadius(expected_files=["calc.py"], observed_files=["calc.py"], unauthorized_files=[], lines_added=1, lines_deleted=0, is_acceptable=True)
    regr = RegressionReport(all_tests_passed=True, total_tests=5, passed_count=5, failed_count=0, skipped_count=0, error_count=0, execution_time_sec=0.5, raw_output="5 passed", blast_radius=blast)

    decision = AdmissionController.evaluate(patch_res, regr)

    # Even if an external agent attempts to assert approval:
    assert decision.approved is False
    assert decision.admit_pr is False

    # The publisher will not publish PR when decision.approved is False
    from github.pr_publisher import PRPublisher
    from harness.docker_sandbox import Sandbox
    with Sandbox() as sb:
        pub = PRPublisher(sb)
        # Even if someone constructs an evidence report, decision.approved is False
        report = pub.build_evidence_report(
            issue_number=1,
            issue_title="test",
            reproduction_res=None or type("Repro", (), {"raw_output": "", "test_code": "assert True"})(),
            patch_res=patch_res,
            regression_res=regr,
            decision=decision,
            branch_name="fix-branch",
        )
        assert "REJECTED" in report


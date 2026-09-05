"""
Tests for diff verification, Gate 4 enforcement, run.json schema extension,
and stale workspace prevention (Sections 1, 2, 3, 7, 9).
"""
import json
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.diff_utils import DiffUtils
from harness.admission_controller import AdmissionController
from harness.run_artifact import write_run_artifact
from agents.patch_agent import PatchLoopResult
from agents.regression_agent import RegressionReport, StructuralBlastRadius


def test_diff_utils_compute_diff_stats():
    diff = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,3 @@\n"
        "-return a / b\n"
        "+if b == 0: return 0.0\n"
        "+return a / b\n"
    )
    stats = DiffUtils.compute_diff_stats(diff)
    assert stats["lines_added"] == 2
    assert stats["lines_deleted"] == 1
    assert stats["total_lines"] == 3
    assert stats["files"] == ["calc.py"]
    assert stats["is_empty"] is False

    h = DiffUtils.compute_diff_hash(diff)
    assert len(h) == 64  # SHA-256


def test_diff_utils_empty_diff_stats():
    stats = DiffUtils.compute_diff_stats("")
    assert stats["lines_added"] == 0
    assert stats["lines_deleted"] == 0
    assert stats["total_lines"] == 0
    assert stats["files"] == []
    assert stats["is_empty"] is True


def test_run_artifact_records_extended_diff_fields():
    with tempfile.TemporaryDirectory() as tmpdir:
        admission_dict = {
            "admit_pr": True,
            "target_test_passed": True,
            "regression_passed": True,
            "blast_radius_acceptable": True,
            "patch_changed": True,
            "diff_stat": "+2/-0",
            "diff_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "rejection_state": "",
            "reasons": ["All 4 gates passed."],
        }
        path = write_run_artifact(
            run_id="run_test123",
            issue_number=101,
            issue_title="Divide by zero",
            pipeline_state_history=["UNVERIFIED", "ADMITTED"],
            admission_decision=admission_dict,
            regression_results={"passed": 5, "failed": 0, "total": 5},
            blast_radius={"files_changed": ["rate_calculator.py"], "lines_added": 2, "lines_deleted": 0, "is_acceptable": True},
            patch_attempts=1,
            reached_green=True,
            artifacts_dir=tmpdir,
            execution_mode="local",
            patch_changed=True,
            diff_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            diff_files=["rate_calculator.py"],
            diff_lines_added=2,
            diff_lines_deleted=0,
            patch_verified_against_test=True,
            github_integration_enabled=False,
            pr_created=False,
            pr_url=None,
            admission_rejection_reason=None,
        )

        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["schema_version"] == "1.1"
        assert data["execution_mode"] == "local"
        assert data["patch_changed"] is True
        assert data["diff_lines_added"] == 2
        assert data["diff_lines_deleted"] == 0
        assert data["diff_files"] == ["rate_calculator.py"]
        assert data["patch_verified_against_test"] is True
        assert data["github_integration_enabled"] is False
        assert data["pr_created"] is False
        assert data["pr_url"] is None


def test_stale_workspace_prevention():
    """
    If a workspace claims GREEN but produced 0 modified files in git,
    Gate 4 MUST reject the patch as REJECTED_EMPTY_PATCH.
    """
    patch_res = PatchLoopResult(
        reached_green=True,
        total_attempts=2,
        winning_diff="",  # No actual diff applied
        history=[],
        total_lines_changed=0,
        final_changed_files=[],
    )
    blast = StructuralBlastRadius(
        expected_files=["calc.py"],
        observed_files=[],
        unauthorized_files=[],
        lines_added=0,
        lines_deleted=0,
        is_acceptable=True,
    )
    regr = RegressionReport(
        all_tests_passed=True,
        total_tests=5,
        passed_count=5,
        failed_count=0,
        skipped_count=0,
        error_count=0,
        execution_time_sec=0.4,
        raw_output="5 passed",
        blast_radius=blast,
    )

    decision = AdmissionController.evaluate(patch_res, regr)
    assert decision.approved is False
    assert decision.gate_4_patch_changed is False
    assert decision.rejection_state == "REJECTED_EMPTY_PATCH"
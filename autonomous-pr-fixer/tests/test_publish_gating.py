"""
Tests that publication is gated on the Admission Controller decision.

The project's central claim is that a pull request appears only when all four
verification gates hold. That claim is only true if the publish call actually
consults the decision, so it is pinned here rather than left to inspection.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main
from harness.admission_controller import AdmissionDecision

ISSUE_KWARGS = dict(
    issue_number=101,
    issue_title="Divide by zero in rate_calculator",
    issue_body="calculate_rate(10, 0) throws ZeroDivisionError",
)


@pytest.fixture
def recorded_publishes(monkeypatch):
    """Capture every publish_pr call instead of performing one."""
    calls: list[dict] = []

    def _fake_publish(self, **kwargs):
        calls.append(kwargs)
        return {
            "status": "dry_run_success",
            "pr_title": "recorded",
            "branch": kwargs.get("branch_name", ""),
            "pr_url": None,
            "display_url": "recorded",
            "published": False,
            "body": kwargs.get("pr_body", ""),
        }

    monkeypatch.setattr(main.PRPublisher, "publish_pr", _fake_publish)
    return calls


def test_approved_run_publishes_with_a_real_evidence_body(recorded_publishes, tmp_path):
    """An admitted patch reaches publish_pr, and the body is the evidence report."""
    admitted = main.run_pipeline(
        repo_dir=str(tmp_path), dry_run=True, mode="local", **ISSUE_KWARGS
    )

    assert admitted is True
    assert len(recorded_publishes) == 1
    body = recorded_publishes[0]["pr_body"]
    # Not the empty string the CLI used to pass, which made the evidence report
    # unreachable from every real run.
    assert body
    assert "RED Gate" in body
    assert "Applied Unified Diff" in body
    assert "APPROVED" in body


def test_rejected_run_never_reaches_publish(recorded_publishes, monkeypatch, tmp_path):
    """A rejected decision must produce no push and no PR attempt at all."""
    def _rejected(patch_res, regr_res):
        return AdmissionDecision(
            approved=False,
            reasons=["forced rejection for test"],
            rejection_summary="forced rejection for test",
            gate_1_target_passed=True,
            gate_2_regression_passed=True,
            gate_3_blast_radius_passed=False,
            gate_4_patch_changed=True,
            rejection_state="REJECTED_ADMISSION",
        )

    monkeypatch.setattr(main.AdmissionController, "evaluate", staticmethod(_rejected))

    admitted = main.run_pipeline(
        repo_dir=str(tmp_path), dry_run=True, mode="local", **ISSUE_KWARGS
    )

    assert admitted is False
    assert recorded_publishes == []

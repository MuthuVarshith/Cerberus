"""
Tests that publication is gated on the Admission Controller decision.

The project's central claim is that a pull request appears only when all four
verification gates hold. That claim is only true if the publish call actually
consults the decision, so it is pinned here rather than left to inspection.
"""
from __future__ import annotations

import pytest

import main
from agents.llm_patch_generator import ScriptedPatchGenerator
from examples.demo import prepare_rate_calculator_demo
from harness.admission_controller import AdmissionDecision


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


def _run(tmp_path):
    scenario = prepare_rate_calculator_demo(str(tmp_path / "fixture"))
    return main.run_pipeline(
        repo_dir=scenario.repo_dir,
        issue_number=scenario.issue_number,
        issue_title=scenario.issue_title,
        issue_body=scenario.issue_body,
        dry_run=True,
        mode="local",
        repro_test_code=scenario.repro_test_code,
        patch_generator=ScriptedPatchGenerator([scenario.patch_diff]),
    )


def test_approved_run_publishes_with_a_real_evidence_body(recorded_publishes, tmp_path):
    """An admitted patch reaches publish_pr, and the body is the evidence report."""
    assert _run(tmp_path) is True
    assert len(recorded_publishes) == 1
    body = recorded_publishes[0]["pr_body"]
    assert "RED Gate" in body
    assert "Applied diff" in body
    assert "APPROVED" in body
    assert "if total == 0" in body


def test_rejected_run_never_reaches_publish(recorded_publishes, monkeypatch, tmp_path):
    """A rejected decision must produce no push and no PR attempt at all."""
    def _rejected(patch_res, regr_res, scope_res):
        return AdmissionDecision(
            approved=False,
            reasons=["forced rejection for test"],
            rejection_summary="forced rejection for test",
            gate_1_target_passed=True,
            gate_2_regression_passed=True,
            gate_3_scope_passed=True,
            gate_4_patch_changed=False,
            rejection_state="EMPTY_PATCH",
        )

    monkeypatch.setattr(main.AdmissionController, "evaluate", staticmethod(_rejected))

    assert _run(tmp_path) is False
    assert recorded_publishes == []

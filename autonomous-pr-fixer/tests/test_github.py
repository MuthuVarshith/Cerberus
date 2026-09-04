"""
Tests for GitHub Integration & Webhook Handler (Section 19 & 20).
"""
import os
import sys
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from github.webhook_handler import app
from github.pr_publisher import PRPublisher
from harness.docker_sandbox import Sandbox
from harness.admission_controller import AdmissionController
from agents.reproduction_agent import ReproductionResult
from agents.patch_agent import PatchLoopResult
from agents.regression_agent import RegressionReport, StructuralBlastRadius


def test_webhook_triggers_on_bug_issue():
    client = TestClient(app)
    res_health = client.get("/health")
    assert res_health.status_code == 200

    payload = {
        "action": "opened",
        "issue": {
            "number": 42,
            "title": "Division by zero in calculate_rate",
            "labels": [{"name": "bug"}],
        },
        "repository": {"full_name": "acme/corp-repo"},
    }
    resp = client.post("/webhook", json=payload, headers={"x-github-event": "issues"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["issue_number"] == 42


def test_webhook_triggers_on_bot_fix_comment():
    client = TestClient(app)
    payload = {
        "action": "created",
        "comment": {"body": "Hey @bot-fix please investigate and patch this bug."},
        "issue": {"number": 88, "title": "Token parser crash"},
        "repository": {"full_name": "acme/corp-repo"},
    }
    resp = client.post("/webhook", json=payload, headers={"x-github-event": "issue_comment"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert "invocation" in data["trigger"]


def test_pr_publisher_evidence_report_formatting():
    with Sandbox() as sb:
        publisher = PRPublisher(sb)

        repro = ReproductionResult(
            reproduced=True,
            test_code="def test_bug(): assert 1 == 2",
            error_message="",
            returncode=1,
            raw_output="AssertionError: 1 != 2",
        )
        patch = PatchLoopResult(
            reached_green=True,
            total_attempts=2,
            winning_diff="--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-1\n+2\n",
            history=[],
        )
        blast = StructuralBlastRadius(expected_files=["mod.py"], observed_files=["mod.py"], unauthorized_files=[], lines_added=1, lines_deleted=1, is_acceptable=True)
        regr = RegressionReport(all_tests_passed=True, total_tests=10, passed_count=10, failed_count=0, skipped_count=0, error_count=0, execution_time_sec=1.2, raw_output="10 passed", blast_radius=blast)
        decision = AdmissionController.evaluate(patch, regr)

        pr_info = publisher.publish_pr(
            issue_number=101,
            issue_title="Wrong calculation in mod.py",
            repo_slug="my-org/my-repo",
            branch_name="fix/issue-101-mod",
            pr_body=publisher.build_evidence_report(101, "Wrong calculation in mod.py", repro, patch, regr, decision, "fix/issue-101-mod"),
            dry_run=True,
        )

        assert pr_info["status"] == "dry_run_success"
        assert "101" in pr_info["pr_title"]
        assert "RED Gate" in pr_info["body"]
        assert "GREEN Gate" in pr_info["body"]
        assert "APPROVED" in pr_info["body"]
        assert "--- a/mod.py" in pr_info["body"]

"""
Tests for GitHub Integration & Webhook Handler (Section 19 & 20).
"""
import hashlib
import hmac
import json
import os
import sys
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from github.webhook_handler import app
from github.pr_publisher import PRPublisher, redact_secrets, sanitize_ref
from harness.admission_controller import AdmissionController
from agents.reproduction_agent import ReproductionResult
from agents.patch_agent import PatchLoopResult
from agents.regression_agent import RegressionReport
from harness.scope_gate import ScopeReport

SECRET = "integration-webhook-secret"


@pytest.fixture(autouse=True)
def _webhook_env(monkeypatch):
    """Deliveries must be signed, and dispatch must not touch a real checkout.

    TestClient runs FastAPI background tasks for real, so CERBERUS_REPO_DIR is
    cleared to keep the dispatched pipeline a logged no-op.
    """
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("CERBERUS_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.delenv("CERBERUS_REPO_DIR", raising=False)
    import github.webhook_handler as wh
    wh._processed_delivery_ids.clear()
    yield
    wh._processed_delivery_ids.clear()


def _post_signed(client, payload: dict, event: str, delivery: str):
    """POST a webhook the way GitHub does: signed body, explicit delivery id."""
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": event,
            "x-hub-signature-256": sig,
            "x-github-delivery": delivery,
        },
    )


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
    resp = _post_signed(client, payload, "issues", "gh-int-001")
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
    resp = _post_signed(client, payload, "issue_comment", "gh-int-002")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert "invocation" in data["trigger"]


def _evidence_inputs():
    repro = ReproductionResult(
        reproduced=True,
        test_code="def test_bug(): assert 1 == 2",
        error_message="",
        returncode=1,
        raw_output="AssertionError: 1 != 2",
        failing_test_ids=[".cerberus.test_reproduce::test_bug"],
        failure_types={".cerberus.test_reproduce::test_bug": "AssertionError"},
        runs=3,
    )
    diff = "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    patch =PatchLoopResult(reached_green=True, total_attempts=2, winning_diff=diff, history=[])
    regr = RegressionReport(results_parsed=True, total_tests=10, passed_count=10, baseline_total=10, execution_time_sec=1.2)
    scope = ScopeReport(allowed_files=["mod.py"], changed_files=["mod.py"], lines_added=1, lines_deleted=1,
                        changed_symbols=["mod.py::<module>"], is_acceptable=True, diff_text=diff)
    decision = AdmissionController.evaluate(patch, regr, scope)
    return repro, patch, regr, scope, decision


def test_pr_publisher_evidence_report_formatting():
    publisher = PRPublisher()
    repro, patch, regr, scope, decision = _evidence_inputs()
    body = publisher.build_evidence_report(101, "Wrong calculation in mod.py", repro, patch, regr, scope, decision, "fix/issue-101", base_commit="a" * 40)
    pr_info = publisher.publish_pr(
        issue_number=101,
        issue_title="Wrong calculation in mod.py",
        repo_slug="my-org/my-repo",
        branch_name="fix/issue-101",
        pr_body=body,
        dry_run=True,
    )

    assert pr_info["status"] == "dry_run_success"
    assert pr_info["pr_url"] is None
    assert "101" in pr_info["pr_title"]
    for expected in ("RED Gate", "GREEN Gate", "APPROVED", "--- a/mod.py", "3 of 3 runs", "baseline-aware", "a" * 40):
        assert expected in pr_info["body"]


def test_evidence_report_does_not_invent_token_counts():
    """With no model in the loop the report must say so, not print a plausible number."""
    publisher = PRPublisher()
    repro, patch, regr, scope, decision = _evidence_inputs()
    unmeasured = publisher.build_evidence_report(1, "t", repro, patch, regr, scope, decision, "fix/issue-1")
    assert "not measured" in unmeasured
    measured = publisher.build_evidence_report(1, "t", repro, patch, regr, scope, decision, "fix/issue-1",
                                               token_usage={"total_tokens": 4242})
    assert "4242 tokens" in measured


def test_redact_secrets_strips_tokens_and_url_credentials():
    """Git echoes the remote URL on failure; the token must not survive into a log."""
    token = "ghp_" + "a" * 36
    text = (
        f"fatal: unable to access 'https://x-access-token:{token}@github.com/org/repo.git/': "
        f"The requested URL returned error: 403"
    )
    cleaned = redact_secrets(text, token)
    assert token not in cleaned
    assert "x-access-token" not in cleaned
    assert "***REDACTED***" in cleaned
    # Non-secret context is preserved so the error stays diagnosable.
    assert "returned error: 403" in cleaned


def test_redact_secrets_catches_token_shape_without_being_told():
    """A token from a stale remote is redacted even when it is not self.token."""
    stray = "github_pat_" + "b" * 30
    assert stray not in redact_secrets(f"remote said {stray} is bad", None)


def test_redact_secrets_ignores_short_or_empty_values():
    assert redact_secrets("", "tok") == ""
    # A short string would otherwise redact innocuous substrings everywhere.
    assert redact_secrets("no secrets in this line", "abc") == "no secrets in this line"


def test_sanitize_ref_removes_shell_metacharacters():
    """Branch names reach a shell command string, so they must be inert."""
    assert sanitize_ref("fix/issue-42-bug") == "fix/issue-42-bug"
    dangerous = sanitize_ref('fix/x"; rm -rf /; echo "')
    for ch in ('"', ";", " ", "$", "`", "|", "&"):
        assert ch not in dangerous
    # Never returns an empty ref, which git would reject.
    assert sanitize_ref("$(:;)") != ""


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _live_publish(publisher, tmp_path, diff="--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"):
    return publisher.publish_pr(
        issue_number=5, issue_title="t", repo_slug="o/r", branch_name="fix/issue-5", pr_body="body",
        dry_run=False, diff_text=diff, source_repo_dir=str(tmp_path), base_commit="b" * 40,
    )


def test_push_failure_error_is_redacted(tmp_path):
    """A failed live push must not return the token in its error string."""
    token = "ghp_" + "c" * 36
    seen = []

    def _git(args, cwd, env):
        seen.append((list(args), env))
        if args[0] == "push":
            return _Completed(128, stderr=f"fatal: could not read from 'https://x-access-token:{token}@github.com/o/r.git'")
        return _Completed()

    info = _live_publish(PRPublisher(github_token=token, git_runner=_git), tmp_path)
    assert info["status"] == "error"
    assert token not in info["error"]
    assert "***REDACTED***" in info["error"]


def test_token_never_appears_in_git_arguments(tmp_path):
    """The token travels in environment configuration, never argv or a remote URL."""
    token = "ghp_" + "d" * 36
    calls = []

    def _git(args, cwd, env):
        calls.append((list(args), env or {}))
        return _Completed(128 if args[0] == "push" else 0, stderr="stop before API call")

    _live_publish(PRPublisher(github_token=token, git_runner=_git), tmp_path)
    assert any(a[0] == "push" for a, _ in calls)
    for args, env in calls:
        assert all(token not in part for part in args)
        if args[0] != "push":
            assert "GIT_CONFIG_COUNT" not in env
    push_env = next(env for args, env in calls if args[0] == "push")
    assert push_env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert token not in push_env["GIT_CONFIG_VALUE_0"]  # base64-encoded, never raw


@pytest.mark.parametrize(
    "diff,fragment",
    [
        ("--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n", "workflow"),
        ("--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-a\n+b\n", "forbidden paths"),
        ("", "no verified diff"),
    ],
)
def test_publisher_refuses_unsafe_or_missing_diffs(tmp_path, diff, fragment):
    def _git(args, cwd, env):
        raise AssertionError("git must not run for a refused publish")

    info = _live_publish(PRPublisher(github_token="ghp_" + "e" * 36, git_runner=_git), tmp_path, diff=diff)
    assert info["status"] == "error"
    assert fragment in info["error"]

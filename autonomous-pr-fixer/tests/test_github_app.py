"""
Tests for the GitHub App: authentication, API client, persistent run store,
Check Run rendering, event routing and authorization, and an end-to-end PR
verification that runs the real CLI in a subprocess.

GitHub itself is replaced by fakes; nothing here touches the network.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from examples.demo import prepare_rate_calculator_demo
from github.app_auth import InstallationTokenProvider, app_jwt
from github.app_service import AppSettings, CerberusApp, Job
from github.check_report import check_run_output
from github.client import GitHubAPIError, GitHubClient
from github.run_store import RunStore


# ------------------------------------------------------------------ auth

@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return key, pem


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_app_jwt_is_a_valid_rs256_token(rsa_key):
    key, pem = rsa_key
    token = app_jwt("12345", pem, now=1_700_000_000)
    header, payload, signature = token.split(".")
    assert json.loads(_b64decode(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64decode(payload))
    assert claims["iss"] == "12345"
    assert 0 < claims["exp"] - claims["iat"] <= 600
    key.public_key().verify(_b64decode(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_installation_tokens_are_cached_until_near_expiry(rsa_key):
    _, pem = rsa_key
    calls = []

    def _post(url, headers):
        calls.append(url)
        assert headers["Authorization"].startswith("Bearer ")
        return {"token": f"tok-{len(calls)}", "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))}

    provider = InstallationTokenProvider("1", pem, http_post=_post)
    assert provider.token(7) == "tok-1"
    assert provider.token(7) == "tok-1"
    assert calls == ["https://api.github.com/app/installations/7/access_tokens"]
    provider._cache[7].expires_at = time.time() + 60  # within the refresh window
    assert provider.token(7) == "tok-2"


# ---------------------------------------------------------------- client

def test_client_redacts_tokens_in_errors_and_maps_missing_collaborators():
    token = "ghs_" + "a" * 36

    def _transport(method, url, headers, body):
        assert headers["Authorization"] == f"Bearer {token}"
        if url.endswith("/permission"):
            return 404, {"message": "Not Found"}
        return 401, {"message": f"Bad credentials for {token}"}

    client = GitHubClient(token, transport=_transport)
    assert client.collaborator_permission("o", "r", "someone") == "none"
    with pytest.raises(GitHubAPIError) as exc:
        client.get_pull("o", "r", 1)
    assert token not in str(exc.value)


def test_check_run_output_is_clipped_to_github_limits():
    sent = {}

    def _transport(method, url, headers, body):
        sent.update(body or {})
        return 200, {}

    GitHubClient("t", transport=_transport).complete_check_run("o", "r", 1, "failure", "x" * 400, "y" * 70000)
    assert len(sent["output"]["title"]) == 255
    assert len(sent["output"]["summary"]) == 65535


# ----------------------------------------------------------------- store

def test_deliveries_are_claimed_once_across_restarts(tmp_path):
    path = str(tmp_path / "runs.sqlite3")
    assert RunStore(path).claim_delivery("d-1", "pull_request") is True
    assert RunStore(path).claim_delivery("d-1", "pull_request") is False


def test_interrupted_runs_are_marked_error_on_recovery(tmp_path):
    store = RunStore(str(tmp_path / "runs.sqlite3"))
    store.create_run("r1", "d1", "o/r", "verify_pr", 3, "abc", "alice")
    store.update_run("r1", status="running")
    assert store.recover_interrupted() == ["r1"]
    row = store.get_run("r1")
    assert (row.status, row.final_state) == ("finished", "ERROR")
    assert store.active_runs("o/r") == 0


# ---------------------------------------------------------- check report

@pytest.mark.parametrize(
    "state,timed_out,conclusion",
    [("ADMITTED", False, "success"), ("REFUSED", False, "failure"), ("ERROR", False, "neutral"), ("ERROR", True, "timed_out")],
)
def test_check_run_conclusions(state, timed_out, conclusion):
    artifact = {"final_state": state, "refusal": {"code": "SCOPE_VIOLATION", "stage": "SCOPE", "message": "m"} if state == "REFUSED" else None}
    got, title, summary, _ = check_run_output(artifact, timed_out=timed_out)
    assert got == conclusion
    assert "never merges" in summary
    if state == "REFUSED":
        assert "SCOPE_VIOLATION" in title


# --------------------------------------------------------------- routing

class FakeGitHub:
    def __init__(self, permissions=None, pull=None, issue=None):
        self.permissions = permissions or {}
        self.pull = pull or {}
        self.issue = issue or {}
        self.comments = []
        self.check_runs = {}
        self.completed = []

    def __call__(self, token):
        return self

    def collaborator_permission(self, owner, repo, user):
        return self.permissions.get(user, "none")

    def get_pull(self, owner, repo, number):
        return self.pull

    def get_issue(self, owner, repo, number):
        return self.issue

    def create_check_run(self, owner, repo, head_sha, name, external_id):
        self.check_runs[99] = {"head_sha": head_sha, "name": name, "external_id": external_id}
        return 99

    def complete_check_run(self, owner, repo, check_run_id, conclusion, title, summary, text=""):
        self.completed.append({"id": check_run_id, "conclusion": conclusion, "title": title, "summary": summary, "text": text})

    def create_issue_comment(self, owner, repo, number, body):
        self.comments.append((number, body))


def _app(tmp_path, github, runner=None, **settings):
    base = dict(webhook_secret="s", data_dir=str(tmp_path / "data"), allow_host_sandbox_for_tests=True)
    base.update(settings)
    kwargs = {"runner": runner} if runner else {}
    return CerberusApp(AppSettings(**base), RunStore(":memory:"), lambda installation: "ghs_test", client_factory=github, **kwargs)


def _labeled_payload(sender="alice", sender_type="User", label="cerberus"):
    return {
        "action": "labeled",
        "label": {"name": label},
        "sender": {"login": sender, "type": sender_type},
        "installation": {"id": 5},
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 3, "labels": [{"name": label}], "head": {"sha": "h" * 40}},
    }


def test_bot_events_are_ignored(tmp_path):
    app = _app(tmp_path, FakeGitHub(permissions={"cerberus[bot]": "admin"}))
    response, job = app.route("pull_request", "d1", _labeled_payload(sender="cerberus[bot]", sender_type="Bot"))
    assert job is None and "bots" in response["reason"]


def test_label_by_user_without_write_access_is_ignored(tmp_path):
    app = _app(tmp_path, FakeGitHub(permissions={"mallory": "read"}))
    response, job = app.route("pull_request", "d1", _labeled_payload(sender="mallory"))
    assert job is None and "write access" in response["reason"]


def test_label_by_maintainer_queues_one_run_per_delivery(tmp_path):
    app = _app(tmp_path, FakeGitHub(permissions={"alice": "maintain"}))
    response, job = app.route("pull_request", "d1", _labeled_payload())
    assert response["status"] == "queued" and job.kind == "verify_pr" and job.number == 3
    assert app.store.get_run(job.run_id).status == "queued"
    again, job2 = app.route("pull_request", "d1", _labeled_payload())
    assert job2 is None and again["reason"] == "duplicate delivery"


def test_other_labels_and_unlabeled_pushes_are_ignored(tmp_path):
    app = _app(tmp_path, FakeGitHub(permissions={"alice": "admin"}))
    assert app.route("pull_request", "d1", _labeled_payload(label="bug"))[1] is None
    payload = _labeled_payload(label="bug")
    payload["action"] = "synchronize"
    assert app.route("pull_request", "d2", payload)[1] is None


def test_verify_comment_requires_write_access(tmp_path):
    app = _app(tmp_path, FakeGitHub(permissions={"alice": "write"}))
    payload = {
        "action": "created", "sender": {"login": "bob", "type": "User"}, "installation": {"id": 5},
        "repository": {"full_name": "acme/widgets"},
        "issue": {"number": 3, "pull_request": {"url": "x"}},
        "comment": {"body": "/cerberus verify please", "user": {"login": "bob"}},
    }
    assert app.route("issue_comment", "d1", payload)[1] is None
    payload["comment"]["user"]["login"] = "alice"
    assert app.route("issue_comment", "d2", payload)[1].kind == "verify_pr"


def test_repair_is_refused_when_no_patch_source_is_configured(tmp_path):
    github = FakeGitHub(permissions={"alice": "admin"})
    app = _app(tmp_path, github)
    payload = {
        "action": "created", "sender": {"login": "alice", "type": "User"}, "installation": {"id": 5},
        "repository": {"full_name": "acme/widgets"}, "issue": {"number": 8},
        "comment": {"body": "/cerberus repair", "user": {"login": "alice"}},
    }
    response, job = app.route("issue_comment", "d1", payload)
    assert job is None and response["reason"] == "repair is not enabled"
    assert "not enabled" in github.comments[0][1]


def test_runs_per_hour_are_capped(tmp_path):
    app = _app(tmp_path, FakeGitHub(permissions={"alice": "admin"}), max_runs_per_hour=1)
    assert app.route("pull_request", "d1", _labeled_payload())[0]["status"] == "queued"
    assert app.route("pull_request", "d2", _labeled_payload())[0]["status"] == "rate_limited"


# ------------------------------------------------------------- execution

def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class LocalRepos:
    """Stands in for cloning from GitHub: the 'remote' is a local repository."""

    def __init__(self, path):
        self.path = path
        self.calls = []

    def prepare(self, owner, repo, token, refs):
        self.calls.append(refs)
        return self.path


def _pr_repo(tmp_path, add_test=True):
    scenario = prepare_rate_calculator_demo(str(tmp_path / "remote"))
    repo = scenario.repo_dir
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "pr")
    with open(os.path.join(repo, "rate_calculator.py"), "w", encoding="utf-8") as f:
        f.write('def calculate_rate(amount: float, total: float) -> float:\n    """Calculate the rate as amount / total."""\n'
                "    if total == 0:\n        return 0.0\n    return amount / total\n")
    if add_test:
        with open(os.path.join(repo, "tests", "test_zero_total.py"), "w", encoding="utf-8") as f:
            f.write("from rate_calculator import calculate_rate\n\n\ndef test_zero_total():\n    assert calculate_rate(10, 0) == 0.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix zero division")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", base)
    return scenario, base, head


def _job(app, head):
    app.store.create_run("app_test", "d1", "acme/widgets", "verify_pr", 3, head, "alice")
    return Job("app_test", "verify_pr", "acme", "widgets", 3, 5, "alice", head_sha=head)


def test_pull_request_verification_end_to_end(tmp_path):
    scenario, base, head = _pr_repo(tmp_path)
    github = FakeGitHub(
        pull={"number": 3, "title": "Fix zero division", "body": "Fixes #7", "base": {"sha": base}, "head": {"sha": head}},
        issue={"title": scenario.issue_title, "body": scenario.issue_body},
    )
    app = _app(tmp_path, github)
    app.repos = LocalRepos(scenario.repo_dir)

    app.execute(_job(app, head))

    assert github.check_runs[99]["head_sha"] == head
    result = github.completed[0]
    assert result["conclusion"] == "success", result["summary"]
    assert result["title"].startswith("Admitted")
    assert "test_zero_total" in result["text"] and "if total == 0" in result["text"]
    row = app.store.get_run("app_test")
    assert (row.status, row.final_state) == ("finished", "ADMITTED")
    assert row.artifact_path and os.path.isfile(row.artifact_path)


def test_pull_request_without_a_new_test_is_refused_without_running(tmp_path):
    scenario, base, head = _pr_repo(tmp_path, add_test=False)
    github = FakeGitHub(pull={"number": 3, "title": "t", "body": "", "base": {"sha": base}, "head": {"sha": head}})
    ran = []
    app = _app(tmp_path, github, runner=lambda *a: ran.append(a) or (0, False))
    app.repos = LocalRepos(scenario.repo_dir)

    app.execute(_job(app, head))

    assert ran == []
    assert github.completed[0]["conclusion"] == "failure"
    assert "exactly one new test file" in github.completed[0]["summary"]
    assert app.store.get_run("app_test").refusal_code == "NO_REPRODUCTION_TEST"


def test_app_refuses_to_run_without_docker(tmp_path, monkeypatch):
    monkeypatch.setenv("CERBERUS_SANDBOX", "host-unsafe")
    scenario, base, head = _pr_repo(tmp_path)
    github = FakeGitHub(pull={"number": 3, "title": "t", "body": "", "base": {"sha": base}, "head": {"sha": head}})
    ran = []
    app = _app(tmp_path, github, runner=lambda *a: ran.append(a) or (0, False), allow_host_sandbox_for_tests=False)
    app.repos = LocalRepos(scenario.repo_dir)

    app.execute(_job(app, head))

    assert ran == []
    assert github.completed[0]["conclusion"] == "neutral"
    assert app.store.get_run("app_test").final_state == "ERROR"


def test_timed_out_run_reports_timed_out(tmp_path):
    scenario, base, head = _pr_repo(tmp_path)
    github = FakeGitHub(pull={"number": 3, "title": "t", "body": "", "base": {"sha": base}, "head": {"sha": head}})
    seen = []
    app = _app(tmp_path, github, runner=lambda argv, env, timeout, log: seen.append((env, timeout)) or (-1, True),
               run_timeout_seconds=900)
    app.repos = LocalRepos(scenario.repo_dir)

    app.execute(_job(app, head))

    # A run killed at the timeout cannot remove its containers; they must stop themselves soon after.
    env, timeout = seen[0]
    assert timeout == 900 and env["CERBERUS_SANDBOX_MAX_LIFETIME_SECONDS"] == "1200"
    assert github.completed[0]["conclusion"] == "timed_out"
    row = app.store.get_run("app_test")
    assert (row.final_state, row.error) == ("ERROR", "timed out")

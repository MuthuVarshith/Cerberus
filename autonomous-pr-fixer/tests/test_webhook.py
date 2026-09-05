"""
Tests for the hardened GitHub webhook handler.
Covers: valid requests, invalid HMAC signature, a missing secret (fail-closed),
duplicate delivery, unsupported event types, @bot-fix trigger, and the
background dispatch into the repair pipeline.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

import pytest
from fastapi.testclient import TestClient


def _make_client_with_secret(secret: str):
    os.environ["GITHUB_WEBHOOK_SECRET"] = secret
    # Re-import to pick up env var at module level
    import importlib
    import github.webhook_handler as wh
    importlib.reload(wh)
    return TestClient(wh.app), wh


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


SECRET = "test-webhook-secret-xyz"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    # Both of these change what a delivery is allowed to do, and TestClient runs
    # background tasks for real. Cleared so an operator's own shell cannot make
    # the suite accept unsigned deliveries or point a pipeline at a live checkout.
    monkeypatch.delenv("CERBERUS_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.delenv("CERBERUS_REPO_DIR", raising=False)
    # Clear processed delivery IDs between tests
    import github.webhook_handler as wh
    wh._processed_delivery_ids.clear()
    yield
    wh._processed_delivery_ids.clear()


def _client():
    from github.webhook_handler import app
    return TestClient(app)


def test_health():
    client = _client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_valid_bug_label_triggers(monkeypatch):
    """A bug-labeled issue with valid HMAC signature is queued."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    import importlib, github.webhook_handler as wh
    wh._processed_delivery_ids.clear()
    client = TestClient(wh.app)
    payload = {
        "action": "labeled",
        "issue": {"number": 42, "title": "Test bug", "body": "desc", "labels": [{"name": "bug"}]},
        "repository": {"full_name": "org/repo"},
    }
    body = json.dumps(payload).encode()
    sig = _sig(SECRET, body)
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": "issues",
            "x-hub-signature-256": sig,
            "x-github-delivery": "delivery-001",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["issue_number"] == 42


def test_invalid_signature_returns_403(monkeypatch):
    """A request with a wrong HMAC signature must be rejected with 403."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    import github.webhook_handler as wh
    client = TestClient(wh.app)
    payload = {"action": "labeled", "issue": {"number": 1, "labels": [{"name": "bug"}]}}
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": "issues",
            "x-hub-signature-256": "sha256=deadbeefdeadbeef",
            "x-github-delivery": "delivery-002",
        },
    )
    assert resp.status_code == 403


def test_duplicate_delivery_ignored(monkeypatch):
    """The same X-GitHub-Delivery ID must be processed only once."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    import github.webhook_handler as wh
    wh._processed_delivery_ids.clear()
    client = TestClient(wh.app)
    payload = {
        "action": "labeled",
        "issue": {"number": 7, "title": "dup", "body": "", "labels": [{"name": "bug"}]},
        "repository": {"full_name": "org/repo"},
    }
    body = json.dumps(payload).encode()
    sig = _sig(SECRET, body)
    headers = {
        "content-type": "application/json",
        "x-github-event": "issues",
        "x-hub-signature-256": sig,
        "x-github-delivery": "delivery-dup-999",
    }
    r1 = client.post("/webhook", content=body, headers=headers)
    r2 = client.post("/webhook", content=body, headers=headers)
    assert r1.json()["status"] == "queued"
    assert r2.json()["status"] == "ignored"
    assert "Duplicate" in r2.json()["reason"]


def test_unsupported_event_ignored(monkeypatch):
    """Events that are not issues or issue_comment must be silently ignored."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    import github.webhook_handler as wh
    wh._processed_delivery_ids.clear()
    client = TestClient(wh.app)
    payload = {"action": "opened", "pull_request": {}}
    body = json.dumps(payload).encode()
    sig = _sig(SECRET, body)
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-hub-signature-256": sig,
            "x-github-delivery": "delivery-003",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_bot_fix_comment_triggers(monkeypatch):
    """An issue_comment with @bot-fix body triggers the pipeline."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    import github.webhook_handler as wh
    wh._processed_delivery_ids.clear()
    client = TestClient(wh.app)
    payload = {
        "action": "created",
        "issue": {"number": 55, "title": "Crash", "body": "error", "labels": []},
        "comment": {"body": "Please @bot-fix this issue"},
        "repository": {"full_name": "org/repo"},
    }
    body = json.dumps(payload).encode()
    sig = _sig(SECRET, body)
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": "issue_comment",
            "x-hub-signature-256": sig,
            "x-github-delivery": "delivery-004",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


def test_missing_secret_returns_403(monkeypatch):
    """An unset GITHUB_WEBHOOK_SECRET must reject the delivery, not skip the check.

    This is the fail-closed guarantee: a misconfigured deployment must not accept
    unauthenticated requests that dispatch code-modifying work.
    """
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    import github.webhook_handler as wh
    client = TestClient(wh.app)
    payload = {
        "action": "labeled",
        "issue": {"number": 1, "title": "x", "body": "", "labels": [{"name": "bug"}]},
        "repository": {"full_name": "org/repo"},
    }
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhook",
        content=body,
        # A signature the *caller* computed. Without a server-side secret there is
        # nothing to check it against, so it must not be honoured.
        headers={
            "content-type": "application/json",
            "x-github-event": "issues",
            "x-hub-signature-256": _sig("attacker-chosen-secret", body),
            "x-github-delivery": "delivery-nosecret-1",
        },
    )
    assert resp.status_code == 403


def test_missing_secret_without_signature_returns_403(monkeypatch):
    """No secret and no signature header at all is still a 403."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    import github.webhook_handler as wh
    client = TestClient(wh.app)
    body = json.dumps({"action": "opened", "issue": {"number": 2, "labels": []}}).encode()
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": "issues",
            "x-github-delivery": "delivery-nosecret-2",
        },
    )
    assert resp.status_code == 403


def test_unsigned_delivery_allowed_only_with_explicit_optin(monkeypatch):
    """The local-replay escape hatch works, but only when set on purpose."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("CERBERUS_ALLOW_UNSIGNED_WEBHOOKS", "1")
    import github.webhook_handler as wh
    wh._processed_delivery_ids.clear()
    client = TestClient(wh.app)
    payload = {
        "action": "labeled",
        "issue": {"number": 3, "title": "replay", "body": "", "labels": [{"name": "bug"}]},
        "repository": {"full_name": "org/repo"},
    }
    resp = client.post(
        "/webhook",
        content=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-github-event": "issues",
            "x-github-delivery": "delivery-optin-1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


def test_valid_signature_still_required_when_secret_is_set(monkeypatch):
    """The opt-out must not weaken a correctly configured deployment."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("CERBERUS_ALLOW_UNSIGNED_WEBHOOKS", "1")
    import github.webhook_handler as wh
    client = TestClient(wh.app)
    body = json.dumps({"action": "labeled", "issue": {"number": 4, "labels": []}}).encode()
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": "issues",
            "x-hub-signature-256": "sha256=" + "0" * 64,
            "x-github-delivery": "delivery-optin-2",
        },
    )
    assert resp.status_code == 403


def test_dispatch_skipped_when_repo_dir_unset(caplog):
    """Without CERBERUS_REPO_DIR the background task refuses to run.

    There is no safe default checkout to fall back on, so the dispatch must be a
    logged no-op rather than pointing the patch loop at an arbitrary directory.
    """
    import github.webhook_handler as wh
    with caplog.at_level(logging.ERROR, logger=wh.__name__):
        wh._run_repair_pipeline(
            issue_number=11,
            issue_title="t",
            issue_body="b",
            repo_slug="org/repo",
            delivery_id="d-skip",
        )
    assert "repair_pipeline_skipped" in caplog.text


def test_dispatch_invokes_pipeline_in_dry_run_by_default(monkeypatch, tmp_path, caplog):
    """A dispatch with a valid repo dir reaches run_pipeline, dry-run by default."""
    import main
    import github.webhook_handler as wh

    calls = []

    def _fake_run_pipeline(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(main, "run_pipeline", _fake_run_pipeline)
    monkeypatch.setenv("CERBERUS_REPO_DIR", str(tmp_path))

    with caplog.at_level(logging.INFO, logger=wh.__name__):
        wh._run_repair_pipeline(
            issue_number=12,
            issue_title="Crash on empty input",
            issue_body="traceback",
            repo_slug="org/repo",
            delivery_id="d-run",
        )

    assert len(calls) == 1
    assert calls[0]["repo_dir"] == str(tmp_path)
    assert calls[0]["issue_number"] == 12
    assert calls[0]["mode"] == "github"
    assert calls[0]["dry_run"] is True
    assert "repair_pipeline_finished" in caplog.text


def test_dispatch_honours_explicit_live_mode(monkeypatch, tmp_path):
    """CERBERUS_WEBHOOK_DRY_RUN=0 is the only way to reach the publishing path."""
    import main
    import github.webhook_handler as wh

    calls = []
    monkeypatch.setattr(main, "run_pipeline", lambda **kw: calls.append(kw) or True)
    monkeypatch.setenv("CERBERUS_REPO_DIR", str(tmp_path))
    monkeypatch.setenv("CERBERUS_WEBHOOK_DRY_RUN", "0")

    wh._run_repair_pipeline(
        issue_number=13, issue_title="t", issue_body="b",
        repo_slug="org/repo", delivery_id="d-live",
    )
    assert calls[0]["dry_run"] is False


def test_dispatch_failure_is_logged_not_raised(monkeypatch, tmp_path, caplog):
    """A crashing pipeline must not escape the background task silently."""
    import main
    import github.webhook_handler as wh

    def _boom(**kwargs):
        raise RuntimeError("sandbox exploded")

    monkeypatch.setattr(main, "run_pipeline", _boom)
    monkeypatch.setenv("CERBERUS_REPO_DIR", str(tmp_path))

    with caplog.at_level(logging.ERROR, logger=wh.__name__):
        wh._run_repair_pipeline(
            issue_number=14, issue_title="t", issue_body="b",
            repo_slug="org/repo", delivery_id="d-boom",
        )

    assert "repair_pipeline_failed" in caplog.text
    assert "sandbox exploded" in caplog.text

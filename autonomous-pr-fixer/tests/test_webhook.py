"""
Tests for the hardened GitHub webhook handler.
Covers: valid requests, invalid HMAC signature, duplicate delivery,
unsupported event types, and @bot-fix trigger.
"""
from __future__ import annotations

import hashlib
import hmac
import json
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

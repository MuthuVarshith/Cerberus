"""
Tests for the webhook endpoint: signature verification (fail closed), delivery
handling, and dispatch into the GitHub App.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import github.webhook_handler as wh

SECRET = "test-webhook-secret-xyz"


class _FakeApp:
    def __init__(self, job=None):
        self.routed = []
        self.executed = []
        self._job = job

    def route(self, event, delivery_id, payload):
        self.routed.append((event, delivery_id, payload))
        if self._job is None:
            return {"status": "ignored", "reason": "test"}, None
        return {"status": "queued", "run_id": "r1"}, self._job

    def execute(self, job):
        self.executed.append(job)


class _Job:
    run_id = "r1"
    kind = "verify_pr"
    slug = "o/r"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("CERBERUS_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    yield
    wh.set_cerberus_app(None)


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(body: bytes, headers: dict):
    base = {"content-type": "application/json", "x-github-event": "pull_request", "x-github-delivery": "d-1"}
    base.update(headers)
    return TestClient(wh.app).post("/webhook", content=body, headers=base)


def test_health():
    resp = TestClient(wh.app).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_valid_delivery_is_routed_and_queued_job_executes():
    fake = _FakeApp(job=_Job())
    wh.set_cerberus_app(fake)
    body = json.dumps({"action": "labeled"}).encode()
    resp = _post(body, {"x-hub-signature-256": _sig(SECRET, body)})
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert fake.routed[0][:2] == ("pull_request", "d-1")
    # TestClient runs background tasks before returning.
    assert len(fake.executed) == 1


def test_ignored_event_executes_nothing():
    fake = _FakeApp(job=None)
    wh.set_cerberus_app(fake)
    body = b"{}"
    resp = _post(body, {"x-hub-signature-256": _sig(SECRET, body)})
    assert resp.json()["status"] == "ignored"
    assert fake.executed == []


def test_invalid_signature_returns_403_and_never_routes():
    fake = _FakeApp(job=_Job())
    wh.set_cerberus_app(fake)
    resp = _post(b"{}", {"x-hub-signature-256": "sha256=" + "0" * 64})
    assert resp.status_code == 403
    assert fake.routed == []


def test_missing_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    fake = _FakeApp(job=_Job())
    wh.set_cerberus_app(fake)
    body = b"{}"
    resp = _post(body, {"x-hub-signature-256": _sig("attacker-chosen", body)})
    assert resp.status_code == 403
    assert fake.routed == []


def test_unsigned_delivery_allowed_only_with_explicit_optin(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("CERBERUS_ALLOW_UNSIGNED_WEBHOOKS", "1")
    fake = _FakeApp(job=None)
    wh.set_cerberus_app(fake)
    assert _post(b"{}", {}).status_code == 200


def test_signature_still_required_when_secret_is_set_even_with_optin(monkeypatch):
    monkeypatch.setenv("CERBERUS_ALLOW_UNSIGNED_WEBHOOKS", "1")
    wh.set_cerberus_app(_FakeApp())
    assert _post(b"{}", {"x-hub-signature-256": "sha256=" + "1" * 64}).status_code == 403


def test_missing_delivery_id_is_rejected():
    wh.set_cerberus_app(_FakeApp())
    body = b"{}"
    resp = TestClient(wh.app).post("/webhook", content=body, headers={
        "content-type": "application/json", "x-github-event": "pull_request", "x-hub-signature-256": _sig(SECRET, body),
    })
    assert resp.status_code == 400


def test_unconfigured_app_returns_503(monkeypatch):
    wh.set_cerberus_app(None)

    def _boom():
        raise RuntimeError("GITHUB_APP_ID and a GitHub App private key are required.")

    monkeypatch.setattr(wh, "get_cerberus_app", _boom)
    body = b"{}"
    assert _post(body, {"x-hub-signature-256": _sig(SECRET, body)}).status_code == 503


def test_service_exposes_only_health_and_webhook():
    """No route may trigger repository work without a verified webhook signature."""
    paths = {getattr(r, "path", None) for r in wh.app.routes}
    app_paths = {p for p in paths if p and not p.startswith(("/docs", "/redoc", "/openapi"))}
    assert app_paths == {"/health", "/webhook"}

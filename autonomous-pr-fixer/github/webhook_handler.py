"""
FastAPI service for the Cerberus GitHub App.

Endpoints: `GET /health`, `POST /webhook`. Nothing else is exposed.

Security properties:
  - HMAC SHA-256 signature verification (X-Hub-Signature-256), constant-time
  - Fails closed: an unset GITHUB_WEBHOOK_SECRET rejects every delivery
  - Idempotent: deliveries are claimed by X-GitHub-Delivery in a SQLite store, so
    redeliveries never start a second run, including across restarts
  - Work happens in background tasks; each run is a subprocess with a timeout
  - Only users with write access can trigger runs; bot events are ignored
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

logger = logging.getLogger(__name__)
app = FastAPI(title="Cerberus: verification gate for bug-fix patches")

_cerberus_app = None


def _get_webhook_secret() -> Optional[bytes]:
    """Return the webhook secret bytes from env, or None if not set."""
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    return secret.encode("utf-8") if secret else None


def _unsigned_deliveries_allowed() -> bool:
    """True only when an operator has explicitly opted out of verification (local replay)."""
    return os.environ.get("CERBERUS_ALLOW_UNSIGNED_WEBHOOKS", "").lower() in ("1", "true", "yes")


def _verify_signature(payload_bytes: bytes, signature_header: Optional[str]) -> bool:
    """Verify the HMAC-SHA256 signature. Returns True only for a valid signature.

    Fails closed. A missing secret is a misconfiguration, not permission to skip
    the check: without it the endpoint cannot distinguish GitHub from anyone else
    who found the URL, and this handler dispatches work on repository code.
    """
    secret = _get_webhook_secret()
    if secret is None:
        if _unsigned_deliveries_allowed():
            logger.warning(
                "GITHUB_WEBHOOK_SECRET is not set and CERBERUS_ALLOW_UNSIGNED_WEBHOOKS "
                "is enabled: accepting an UNVERIFIED delivery. Never use this in production."
            )
            return True
        logger.error("GITHUB_WEBHOOK_SECRET is not set: rejecting delivery.")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def set_cerberus_app(instance) -> None:
    """Install a configured CerberusApp (used by tests and custom deployments)."""
    global _cerberus_app
    _cerberus_app = instance


def get_cerberus_app():
    """Build the App from the environment on first use."""
    global _cerberus_app
    if _cerberus_app is None:
        from github.app_auth import InstallationTokenProvider
        from github.app_service import AppSettings, CerberusApp
        from github.run_store import RunStore

        settings = AppSettings.from_env()
        tokens = InstallationTokenProvider(settings.app_id, settings.private_key_pem)
        store = RunStore(settings.db_path)
        interrupted = store.recover_interrupted()
        if interrupted:
            logger.warning("marked %d interrupted run(s) as ERROR on startup", len(interrupted))
        _cerberus_app = CerberusApp(settings, store, tokens.token)
    return _cerberus_app


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness probe."""
    return {"status": "healthy", "service": "Cerberus"}


@app.post("/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(default=None),
    x_github_event: Optional[str] = Header(default=None),
    x_github_delivery: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Receive a GitHub webhook, verify it, and queue at most one run per delivery."""
    raw_body = await request.body()
    if not _verify_signature(raw_body, x_hub_signature_256):
        logger.warning("webhook_signature_invalid delivery_id=%s", x_github_delivery)
        raise HTTPException(status_code=403, detail="Invalid webhook signature.")
    if not x_github_delivery:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Delivery header.")
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON payload.")
    try:
        cerberus = get_cerberus_app()
    except Exception as exc:
        logger.error("github_app_not_configured error=%s", exc)
        raise HTTPException(status_code=503, detail="Cerberus GitHub App is not configured.")

    response, job = cerberus.route(x_github_event or "", x_github_delivery, payload)
    if job is not None:
        background_tasks.add_task(cerberus.execute, job)
        logger.info("run_queued run_id=%s kind=%s repo=%s", job.run_id, job.kind, job.slug)
    return response

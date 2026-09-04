"""
FastAPI Webhook Listener for GitHub Issue and @bot-fix events.

Security hardening:
  - HMAC SHA-256 signature verification (X-Hub-Signature-256)
  - Constant-time comparison via hmac.compare_digest
  - X-GitHub-Delivery idempotency tracking (in-memory set)
  - Background task execution via FastAPI BackgroundTasks
  - Rejects invalid/duplicate/unsupported events with 4xx
  - Webhook secret loaded from GITHUB_WEBHOOK_SECRET env var (never hard-coded)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any, Dict, Optional, Set

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

logger = logging.getLogger(__name__)
app = FastAPI(title="Autonomous Software Repair Webhook Service")
_processed_delivery_ids: Set[str] = set()
SUPPORTED_EVENTS = {"issues", "issue_comment"}


def _get_webhook_secret() -> Optional[bytes]:
    """Return the webhook secret bytes from env, or None if not set."""
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    return secret.encode("utf-8") if secret else None


def _verify_signature(payload_bytes: bytes, signature_header: Optional[str]) -> bool:
    """Verify HMAC-SHA256 signature. Returns True when valid or no secret configured."""
    secret = _get_webhook_secret()
    if secret is None:
        logger.warning(
            "GITHUB_WEBHOOK_SECRET is not set. "
            "Signature verification is DISABLED. Set it in production."
        )
        return True
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _run_repair_pipeline(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    repo_slug: str,
    delivery_id: str,
) -> None:
    """Background task: placeholder for real pipeline invocation."""
    logger.info(
        "repair_pipeline_started delivery_id=%s issue=%s repo=%s",
        delivery_id, issue_number, repo_slug,
    )


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness probe."""
    return {"status": "healthy", "service": "Autonomous Software Repair Harness"}


@app.post("/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(default=None),
    x_github_event: Optional[str] = Header(default=None),
    x_github_delivery: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Receive a GitHub webhook, verify, deduplicate, dispatch pipeline."""
    raw_body = await request.body()
    if not _verify_signature(raw_body, x_hub_signature_256):
        logger.warning("webhook_signature_invalid delivery_id=%s", x_github_delivery)
        raise HTTPException(status_code=403, detail="Invalid webhook signature.")
    if x_github_event not in SUPPORTED_EVENTS:
        return {"status": "ignored", "reason": f"Unsupported event type: {x_github_event}"}
    delivery_id = x_github_delivery or ""
    if delivery_id and delivery_id in _processed_delivery_ids:
        logger.info("webhook_duplicate_ignored delivery_id=%s", delivery_id)
        return {"status": "ignored", "reason": "Duplicate delivery - already processed."}
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON payload.")
    action = payload.get("action", "")
    issue: Dict[str, Any] = payload.get("issue") or {}
    comment: Dict[str, Any] = payload.get("comment") or {}
    repo: Dict[str, Any] = payload.get("repository") or {}
    should_trigger = False
    trigger_reason = ""
    if x_github_event == "issues" and action in ("opened", "labeled"):
        labels = [lbl.get("name", "").lower() for lbl in issue.get("labels", [])]
        if "bug" in labels or "auto-fix" in labels:
            should_trigger = True
            trigger_reason = f"Issue #{issue.get('number')} labeled/opened as bug"
    elif x_github_event == "issue_comment" and action == "created":
        comment_body = comment.get("body", "")
        if "@bot-fix" in comment_body.lower():
            should_trigger = True
            trigger_reason = f"@bot-fix invocation on Issue #{issue.get('number')}"
    if not should_trigger:
        return {"status": "ignored", "reason": "No relevant trigger event detected."}
    if delivery_id:
        _processed_delivery_ids.add(delivery_id)
    inum: int = issue.get("number", 0)
    ititle: str = issue.get("title", "")
    ibody: str = issue.get("body", "")
    rslug: str = repo.get("full_name", "")
    background_tasks.add_task(
        _run_repair_pipeline,
        issue_number=inum,
        issue_title=ititle,
        issue_body=ibody,
        repo_slug=rslug,
        delivery_id=delivery_id,
    )
    logger.info("webhook_queued delivery_id=%s issue=%s repo=%s", delivery_id, inum, rslug)
    return {
        "status": "queued",
        "trigger": trigger_reason,
        "issue_number": inum,
        "issue_title": ititle,
        "repo": rslug,
        "delivery_id": delivery_id,
    }

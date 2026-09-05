"""
FastAPI Webhook Listener for GitHub Issue and @bot-fix events.

Security hardening:
  - HMAC SHA-256 signature verification (X-Hub-Signature-256)
  - Constant-time comparison via hmac.compare_digest
  - Fails *closed*: an unset GITHUB_WEBHOOK_SECRET rejects every delivery
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
app = FastAPI(title="Cerberus: Verification-First Autonomous Software Repair Service")
_processed_delivery_ids: Set[str] = set()
SUPPORTED_EVENTS = {"issues", "issue_comment"}


def _get_webhook_secret() -> Optional[bytes]:
    """Return the webhook secret bytes from env, or None if not set."""
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    return secret.encode("utf-8") if secret else None


def _unsigned_deliveries_allowed() -> bool:
    """True only when an operator has explicitly opted out of verification.

    This exists so a developer replaying captured payloads at localhost is not
    forced to invent a secret. It is deliberately an opt-*in* to the insecure
    path: the variable has to be set on purpose, and the endpoint says so loudly
    on every request it lets through.
    """
    return os.environ.get("CERBERUS_ALLOW_UNSIGNED_WEBHOOKS", "").lower() in ("1", "true", "yes")


def _verify_signature(payload_bytes: bytes, signature_header: Optional[str]) -> bool:
    """Verify the HMAC-SHA256 signature. Returns True only for a valid signature.

    Fails closed. A missing secret is a misconfiguration, not permission to skip
    the check: without it the endpoint cannot distinguish GitHub from anyone else
    who found the URL, and this handler dispatches code-modifying work.
    """
    secret = _get_webhook_secret()
    if secret is None:
        if _unsigned_deliveries_allowed():
            logger.warning(
                "GITHUB_WEBHOOK_SECRET is not set and CERBERUS_ALLOW_UNSIGNED_WEBHOOKS "
                "is enabled: accepting an UNVERIFIED delivery. Never use this in production."
            )
            return True
        logger.error(
            "GITHUB_WEBHOOK_SECRET is not set: rejecting delivery. Set the secret to "
            "enable verification, or CERBERUS_ALLOW_UNSIGNED_WEBHOOKS=1 for local replay."
        )
        return False
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _resolve_repo_dir() -> Optional[str]:
    """Local checkout the pipeline should operate on, or None if unusable.

    A webhook carries a repo *slug*; the sandbox needs a real directory on disk.
    CERBERUS_REPO_DIR names it. When it is absent there is no defensible default
    — running against the server's working directory would point the patch loop
    at whatever happens to be there — so dispatch is refused instead.
    """
    repo_dir = os.environ.get("CERBERUS_REPO_DIR", "").strip()
    if not repo_dir or not os.path.isdir(repo_dir):
        return None
    return repo_dir


def _dry_run_enabled() -> bool:
    """Whether the dispatched pipeline stops short of publishing a real PR.

    Defaults to True: a webhook arriving at a fresh deployment should not open
    pull requests on someone's repository until that is switched on deliberately.
    """
    return os.environ.get("CERBERUS_WEBHOOK_DRY_RUN", "1").lower() not in ("0", "false", "no")


def _run_repair_pipeline(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    repo_slug: str,
    delivery_id: str,
) -> None:
    """Background task: run the full verification pipeline for one issue."""
    logger.info(
        "repair_pipeline_started delivery_id=%s issue=%s repo=%s",
        delivery_id, issue_number, repo_slug,
    )

    repo_dir = _resolve_repo_dir()
    if repo_dir is None:
        logger.error(
            "repair_pipeline_skipped delivery_id=%s issue=%s reason=%s",
            delivery_id, issue_number,
            "CERBERUS_REPO_DIR is unset or does not name a directory",
        )
        return

    # Imported here, not at module scope: it pulls in the whole agent stack and
    # would make `github.webhook_handler` unimportable (and untestable) whenever
    # any agent dependency is missing.
    from main import run_pipeline

    dry_run = _dry_run_enabled()
    try:
        admitted = run_pipeline(
            repo_dir=repo_dir,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            dry_run=dry_run,
            mode="github",
        )
    except Exception:
        # A background task that raises dies silently inside Starlette, so the
        # traceback is captured here or it is lost.
        logger.exception(
            "repair_pipeline_failed delivery_id=%s issue=%s", delivery_id, issue_number,
        )
        return

    logger.info(
        "repair_pipeline_finished delivery_id=%s issue=%s admitted=%s dry_run=%s",
        delivery_id, issue_number, admitted, dry_run,
    )


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness probe."""
    return {"status": "healthy", "service": "Cerberus: Verification-First Autonomous Software Repair Harness"}


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

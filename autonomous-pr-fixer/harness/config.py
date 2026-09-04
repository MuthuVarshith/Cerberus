"""
Configuration loader and startup validator for Cerberus Autonomous Software Repair Harness.

Reads environment variables and fails fast (raises ConfigError) if required
secrets are missing when live GitHub integration is requested.
Secrets are never printed into logs.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised at startup when a required environment variable is missing."""


@dataclass
class HarnessConfig:
    """Parsed, validated harness configuration."""

    # GitHub
    github_token: Optional[str] = field(default=None, repr=False)
    github_webhook_secret: Optional[str] = field(default=None, repr=False)
    github_repo_slug: str = ""

    # LLM
    openai_api_key: Optional[str] = field(default=None, repr=False)

    # Patch loop
    patch_max_attempts: int = 5
    patch_max_lines_changed: int = 200

    # Sandbox
    sandbox_timeout_seconds: int = 60
    sandbox_network_disabled: bool = True

    # Observability
    log_level: str = "INFO"
    run_artifacts_dir: str = "artifacts"

    def has_github_integration(self) -> bool:
        return bool(self.github_token and self.github_repo_slug)

    def has_llm(self) -> bool:
        return bool(self.openai_api_key)


def load_config(require_github: bool = False, require_llm: bool = False) -> HarnessConfig:
    """
    Load configuration from environment variables.

    Args:
        require_github: If True, raises ConfigError when GITHUB_TOKEN or
                        GITHUB_WEBHOOK_SECRET are missing.
        require_llm: If True, raises ConfigError when no LLM API key is set.

    Returns:
        A validated HarnessConfig instance.
    """
    def _int(key: str, default: int) -> int:
        try:
            return int(os.environ.get(key, str(default)))
        except ValueError:
            logger.warning("Invalid value for %s, using default %s", key, default)
            return default

    def _bool(key: str, default: bool) -> bool:
        raw = os.environ.get(key, str(default)).lower()
        return raw in ("1", "true", "yes")

    cfg = HarnessConfig(
        github_token=os.environ.get("GITHUB_TOKEN") or None,
        github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET") or None,
        github_repo_slug=os.environ.get("GITHUB_REPO_SLUG", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        patch_max_attempts=_int("PATCH_MAX_ATTEMPTS", 5),
        patch_max_lines_changed=_int("PATCH_MAX_LINES_CHANGED", 200),
        sandbox_timeout_seconds=_int("SANDBOX_TIMEOUT_SECONDS", 60),
        sandbox_network_disabled=_bool("SANDBOX_NETWORK_DISABLED", True),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        run_artifacts_dir=os.environ.get("RUN_ARTIFACTS_DIR", "artifacts"),
    )

    missing = []
    if require_github:
        if not cfg.github_token:
            missing.append("GITHUB_TOKEN")
        if not cfg.github_webhook_secret:
            missing.append("GITHUB_WEBHOOK_SECRET")
        if not cfg.github_repo_slug:
            missing.append("GITHUB_REPO_SLUG")

    if require_llm:
        if not cfg.openai_api_key:
            missing.append("OPENAI_API_KEY (or equivalent LLM key)")

    if missing:
        raise ConfigError(
            "Required environment variables are not set: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in the values."
        )

    logging.basicConfig(level=cfg.log_level)
    logger.info(
        "harness_config_loaded github_integration=%s llm=%s artifacts_dir=%s",
        cfg.has_github_integration(),
        cfg.has_llm(),
        cfg.run_artifacts_dir,
    )
    return cfg

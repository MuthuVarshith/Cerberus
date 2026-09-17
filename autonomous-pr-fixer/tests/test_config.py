"""
Tests for the harness configuration loader.
Covers: happy path, missing required env vars, and no secrets in logs.
"""
from __future__ import annotations

import pytest
from harness.config import ConfigError, load_config


def test_load_config_defaults(monkeypatch):
    """Config loads with all defaults when no env vars are set."""
    for key in ["GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET", "GITHUB_REPO_SLUG", "OPENAI_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    cfg = load_config()
    assert cfg.patch_max_attempts == 5
    assert cfg.sandbox_timeout_seconds == 60
    assert cfg.log_level == "INFO"
    assert not cfg.has_github_integration()


def test_load_config_require_github_raises_when_missing(monkeypatch):
    """ConfigError must be raised when require_github=True but token missing."""
    for key in ["GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET", "GITHUB_REPO_SLUG"]:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ConfigError) as exc_info:
        load_config(require_github=True)
    assert "GITHUB_TOKEN" in str(exc_info.value)


def test_load_config_require_github_ok_when_set(monkeypatch):
    """Config loads successfully when all GitHub vars are present."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake_token")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "supersecret")
    monkeypatch.setenv("GITHUB_REPO_SLUG", "org/repo")
    cfg = load_config(require_github=True)
    assert cfg.has_github_integration()
    # Ensure token is NOT in repr (repr=False on dataclass field)
    assert "ghp_fake_token" not in repr(cfg)


def test_load_config_require_llm_raises_when_missing(monkeypatch):
    """ConfigError must be raised when require_llm=True but API key missing."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigError) as exc_info:
        load_config(require_llm=True)
    assert "OPENAI_API_KEY" in str(exc_info.value)


def test_int_env_var_parsing(monkeypatch):
    monkeypatch.setenv("PATCH_MAX_ATTEMPTS", "10")
    monkeypatch.setenv("SANDBOX_TIMEOUT_SECONDS", "120")
    cfg = load_config()
    assert cfg.patch_max_attempts == 10
    assert cfg.sandbox_timeout_seconds == 120

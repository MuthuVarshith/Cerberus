"""Repair-agent configuration, read from the environment. No secrets are stored here."""
from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-5-5"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


class RepairConfig(BaseModel):
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    max_attempts: int = Field(3, ge=1, le=10, description="Patch submissions to the Cerberus gate")
    max_tool_calls: int = Field(50, ge=1)
    max_agent_tokens: int = Field(400_000, ge=1, description="Input + output tokens across the run")
    max_run_seconds: int = Field(1800, ge=10)
    max_cost_per_task: float = Field(1.25, gt=0)
    max_evaluation_cost: float = Field(15.0, gt=0)
    max_output_tokens: int = Field(16000, ge=256, description="Per model call (thinking counts toward it)")
    effort: str = Field("medium", description="Anthropic output_config.effort: low|medium|high|xhigh|max")
    server_fallbacks: bool = Field(True, description="Anthropic server-side refusal fallbacks (fallbacks: default)")
    tool_timeout_seconds: int = Field(120, ge=1)
    max_tool_output_chars: int = Field(12_000, ge=500)
    max_text_only_turns: int = Field(3, ge=1, description="Consecutive replies without a tool call before giving up")
    isolation: Optional[str] = None  # None -> Cerberus default (docker)

    @classmethod
    def from_env(cls, **overrides) -> "RepairConfig":
        values = dict(
            provider=_env("LLM_PROVIDER", DEFAULT_PROVIDER).lower(),
            model=_env("LLM_MODEL", DEFAULT_MODEL),
            max_attempts=_env_int("MAX_ATTEMPTS", 3),
            max_tool_calls=_env_int("MAX_TOOL_CALLS", 50),
            max_agent_tokens=_env_int("MAX_AGENT_TOKENS", 400_000),
            max_run_seconds=_env_int("MAX_RUN_TIME", 1800),
            max_cost_per_task=_env_float("MAX_COST_PER_TASK", 1.25),
            max_evaluation_cost=_env_float("MAX_EVALUATION_COST", 15.0),
            effort=_env("LLM_EFFORT", "medium"),
            server_fallbacks=_env("LLM_SERVER_FALLBACKS", "true").lower() not in ("0", "false", "no"),
        )
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

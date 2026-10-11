"""Repair-agent configuration, read from the environment. No secrets are stored here."""
from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "gemini": "gemini-3.8-flash", "openrouter": "qwen/qwen3-coder:free"}
MIN_INTERVAL = {"gemini": 6.0, "openrouter": 4.0}   # free tiers: ~10 and ~20 requests per minute


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


RETRIEVAL_KINDS = ("bm25", "hybrid-bge-small", "hybrid-jina-code")


def _retrieval_env() -> Optional[str]:
    """RETRIEVAL=none|bm25|hybrid-bge-small|hybrid-jina-code; unset or 'none' keeps Phase 1 tools only."""
    value = _env("RETRIEVAL", "none").lower()
    if value == "none":
        return None
    if value not in RETRIEVAL_KINDS:
        raise ValueError(f"RETRIEVAL must be none or one of {RETRIEVAL_KINDS}, not {value!r}")
    return value


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
    retrieval: Optional[str] = Field(None, description="None (Phase 1 tools only), bm25, hybrid-bge-small, hybrid-jina-code")
    min_seconds_between_calls: float = Field(0.0, ge=0, description="Client-side spacing for rate-limited (free) tiers")
    temperature: Optional[float] = Field(None, ge=0, description="Sent to OpenAI-compatible providers when set")
    seed: Optional[int] = Field(None, description="Sent to OpenAI-compatible providers when set; not all honour it")
    input_price_per_mtok: Optional[float] = Field(None, ge=0, description="Overrides the built-in price table (USD)")
    output_price_per_mtok: Optional[float] = Field(None, ge=0)
    orchestrator: str = Field("loop", pattern="^(loop|langgraph)$",
                              description="loop (Phase 1 agent loop) or langgraph (Phase 3 graph)")
    review: bool = Field(False, description="langgraph only: pause admitted patches for human approval")
    checkpoint_db: Optional[str] = Field(None, description="langgraph SQLite checkpoint file (default: <out_root>)")

    @classmethod
    def from_env(cls, **overrides) -> "RepairConfig":
        provider = _env("LLM_PROVIDER", DEFAULT_PROVIDER).lower()
        model = _env("LLM_MODEL", DEFAULT_MODELS.get(provider, DEFAULT_MODEL))
        free_flag = _env("LLM_FREE_TIER", "true").lower() not in ("0", "false", "no")
        # Google AI Studio's free tier and OpenRouter ':free' models are not billed.
        free_gemini = free_flag and (provider == "gemini" or (provider == "openrouter" and model.endswith(":free")))

        def _price(name: str) -> Optional[float]:
            raw = os.environ.get(name, "").strip()
            if raw:
                try:
                    return float(raw)
                except ValueError:
                    return None
            return 0.0 if free_gemini else None  # Google AI Studio free tier is not billed

        values = dict(
            provider=provider,
            model=model,
            min_seconds_between_calls=_env_float("LLM_MIN_SECONDS_BETWEEN_CALLS", MIN_INTERVAL.get(provider, 0.0)),
            temperature=_env_float("LLM_TEMPERATURE", -1.0) if _env("LLM_TEMPERATURE", "") else None,
            seed=_env_int("LLM_SEED", 0) if _env("LLM_SEED", "") else None,
            input_price_per_mtok=_price("LLM_INPUT_PRICE_PER_MTOK"),
            output_price_per_mtok=_price("LLM_OUTPUT_PRICE_PER_MTOK"),
            retrieval=_retrieval_env(),
            max_attempts=_env_int("MAX_ATTEMPTS", 3),
            max_tool_calls=_env_int("MAX_TOOL_CALLS", 50),
            max_agent_tokens=_env_int("MAX_AGENT_TOKENS", 400_000),
            max_run_seconds=_env_int("MAX_RUN_TIME", 1800),
            max_cost_per_task=_env_float("MAX_COST_PER_TASK", 1.25),
            max_evaluation_cost=_env_float("MAX_EVALUATION_COST", 15.0),
            effort=_env("LLM_EFFORT", "medium"),
            orchestrator=_env("ORCHESTRATOR", "loop").lower(),
            review=_env("HUMAN_REVIEW", "false").lower() in ("1", "true", "yes"),
            checkpoint_db=_env("GRAPH_CHECKPOINT_DB", "") or None,
            server_fallbacks=_env("LLM_SERVER_FALLBACKS", "true").lower() not in ("0", "false", "no"),
        )
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

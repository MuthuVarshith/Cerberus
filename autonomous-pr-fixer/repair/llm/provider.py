"""
Provider-independent model interface.

The agent talks to a `ChatModel`: a system prompt, a list of neutral `Message`s and the
tool specifications go in; text and native tool calls come out, with token usage. Each
provider adapter converts to and from its own wire format. Credentials are read by the
provider SDK from the environment and never pass through this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

# USD per million tokens (input, output). Unknown models get no cost estimate rather
# than a guessed one; the token budget still applies to them.
PRICES_PER_MTOK: Dict[str, tuple] = {
    "claude-sonnet-5-5": (2.0, 10.0),
}


class LLMError(RuntimeError):
    """The model call failed (network, auth, rate limit, malformed response)."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    """One conversation turn. `assistant` turns may carry tool calls; `user` turns may carry tool results."""
    role: str  # "user" | "assistant"
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_results: List[ToolResult] = field(default_factory=list)
    raw: Any = None  # provider-native content of an assistant turn, replayed unchanged (e.g. thinking blocks)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ModelResponse:
    text: str
    tool_calls: List[ToolCall]
    usage: Usage
    stop_reason: str = ""
    model: str = ""
    latency_seconds: float = 0.0
    raw: Any = None


class ChatModel(Protocol):
    model: str

    def complete(self, system: str, messages: List[Message], tools: List[ToolSpec],
                 max_tokens: int) -> ModelResponse: ...


def estimate_cost(model: str, usage: Usage) -> Optional[float]:
    """USD cost of one call, or None when the model has no known price."""
    name = model.split("/")[-1]
    price = PRICES_PER_MTOK.get(name)
    if price is None:
        return None
    return round(usage.input_tokens * price[0] / 1e6 + usage.output_tokens * price[1] / 1e6, 6)


def create_model(provider: str, model: str, effort: Optional[str] = None, server_fallbacks: bool = True,
                 min_interval: float = 0.0, temperature: Optional[float] = None,
                 seed: Optional[int] = None) -> ChatModel:
    provider = provider.lower()
    if provider == "anthropic":
        from repair.llm.anthropic import AnthropicChatModel
        return AnthropicChatModel(model, effort=effort, server_fallbacks=server_fallbacks)
    if provider == "gemini":
        import os
        from repair.llm.openai_compat import GEMINI_BASE_URL, OpenAICompatChatModel
        key_env = "GEMINI_API_KEY" if os.environ.get("GEMINI_API_KEY") else "GOOGLE_API_KEY"
        return OpenAICompatChatModel(model, GEMINI_BASE_URL, key_env, reasoning_effort=effort, min_interval=min_interval,
                                     temperature=temperature, seed=seed)
    if provider == "openrouter":
        from repair.llm.openai_compat import OPENROUTER_BASE_URL, OpenAICompatChatModel
        return OpenAICompatChatModel(model, OPENROUTER_BASE_URL, "OPENROUTER_API_KEY", min_interval=min_interval,
                                     temperature=temperature, seed=seed)
    if provider == "openai-compatible":
        # Any OpenAI-style chat-completions endpoint (e.g. Groq, Cerebras, Together, Mistral, DeepSeek):
        # LLM_BASE_URL is the API root (".../v1"), the key is read from LLM_API_KEY.
        import os
        from repair.llm.openai_compat import OpenAICompatChatModel
        base_url = os.environ.get("LLM_BASE_URL", "").strip()
        if not base_url.startswith("https://"):
            raise LLMError("LLM_PROVIDER=openai-compatible needs LLM_BASE_URL (https://...) to be set.")
        return OpenAICompatChatModel(model, base_url, "LLM_API_KEY", min_interval=min_interval,
                                     temperature=temperature, seed=seed)
    raise LLMError(f"Unsupported LLM_PROVIDER '{provider}'. "
                   "Supported: anthropic, gemini, openrouter, openai-compatible.")

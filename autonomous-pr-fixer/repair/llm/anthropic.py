"""
Anthropic Messages API adapter with native tool use. Credentials are resolved by the SDK
(ANTHROPIC_API_KEY or an `ant auth login` profile); this module never reads or logs them.

Model notes for the default `claude-sonnet-5-5`:
- sampling parameters (temperature) are rejected, so none are sent; depth is set with
  `output_config.effort`;
- thinking is on and its blocks are bound to the conversation, so every assistant turn is
  replayed exactly as returned (`Message.raw`), never rebuilt from text and tool calls;
- a `refusal` stop reason is reported as an LLMError; server-side fallbacks
  (`fallbacks: "default"`, beta header below) are enabled unless disabled in config.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from repair.llm.provider import LLMError, Message, ModelResponse, ToolCall, ToolSpec, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicChatModel:
    def __init__(self, model: str, client: Any = None, effort: Optional[str] = "medium", server_fallbacks: bool = True):
        self.model = model
        self.effort = effort
        self.server_fallbacks = server_fallbacks
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency declared in requirements
                raise LLMError("The 'anthropic' package is not installed.") from exc
            try:
                client = anthropic.Anthropic(max_retries=2)
            except Exception as exc:  # no credential source configured
                raise LLMError(f"Anthropic client could not be created: {type(exc).__name__}") from None
        self._client = client

    @staticmethod
    def _to_wire(messages: List[Message]) -> List[Dict[str, Any]]:
        wire: List[Dict[str, Any]] = []
        for m in messages:
            if m.role == "assistant" and m.raw is not None:
                wire.append({"role": "assistant", "content": m.raw})
                continue
            blocks: List[Dict[str, Any]] = []
            for r in m.tool_results:
                blocks.append({"type": "tool_result", "tool_use_id": r.tool_call_id,
                               "content": r.content, "is_error": r.is_error})
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            for c in m.tool_calls:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
            wire.append({"role": m.role, "content": blocks})
        return wire

    def complete(self, system: str, messages: List[Message], tools: List[ToolSpec], max_tokens: int) -> ModelResponse:
        extra_body: Dict[str, Any] = {}
        extra_headers: Dict[str, str] = {}
        if self.effort:
            extra_body["output_config"] = {"effort": self.effort}
        if self.server_fallbacks:
            extra_body["fallbacks"] = "default"
            extra_headers["anthropic-beta"] = FALLBACK_BETA
        started = time.monotonic()
        try:
            resp = self._client.messages.create(
                model=self.model,
                system=system,
                messages=self._to_wire(messages),
                tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
                tool_choice={"type": "auto"},
                max_tokens=max_tokens,
                extra_body=extra_body or None,
                extra_headers=extra_headers or None,
            )
        except Exception as exc:  # the SDK raises many types; the agent needs one
            raise LLMError(f"{type(exc).__name__}: {str(exc)[:300]}") from None

        stop = str(getattr(resp, "stop_reason", "") or "")
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMError(f"Model refused the request (category: {getattr(details, 'category', None)})")
        text_parts: List[str] = []
        calls: List[ToolCall] = []
        raw: List[Dict[str, Any]] = []
        for block in getattr(resp, "content", []) or []:
            raw.append(block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block))
            kind = getattr(block, "type", "")
            if kind == "text":
                text_parts.append(block.text)
            elif kind == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
        usage = getattr(resp, "usage", None)
        return ModelResponse(
            text="\n".join(text_parts),
            tool_calls=calls,
            usage=Usage(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            ),
            stop_reason=stop,
            model=str(getattr(resp, "model", self.model) or self.model),
            latency_seconds=time.monotonic() - started,
            raw=raw,
        )

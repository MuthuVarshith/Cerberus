"""
OpenAI-compatible chat-completions adapter (native function calling over plain HTTP).

Used for Google AI Studio's Gemini endpoint and any other OpenAI-compatible provider.
The API key is read from the environment and only ever placed in the Authorization
header; it is never logged, stored or included in an error message.

Free tiers are rate-limited, so the adapter spaces requests (`min_interval`) and retries
HTTP 429/5xx with backoff, honouring Retry-After when the server sends it.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from repair.llm.provider import LLMError, Message, ModelResponse, ToolCall, ToolSpec, Usage

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_DROP_KEYS = {"title", "default", "examples", "$schema"}


def sanitize_schema(schema: Any) -> Any:
    """Reduce a Pydantic JSON schema to the subset function-calling APIs accept.

    Optional[X] (anyOf X|null) becomes X; titles/defaults are dropped; $defs are inlined.
    """
    defs = schema.get("$defs", {}) if isinstance(schema, dict) else {}

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs.get(node["$ref"].split("/")[-1], {}))
        if "anyOf" in node:
            options = [o for o in node["anyOf"] if o.get("type") != "null"]
            if len(options) == 1:
                merged = {**{k: v for k, v in node.items() if k != "anyOf"}, **options[0]}
                return walk(merged)
        return {k: walk(v) for k, v in node.items() if k not in _DROP_KEYS and k != "$defs"}

    return walk(schema)


def _quota_ids(text: str) -> List[str]:
    return re.findall(r'"quotaId":\s*"([^"]+)"', text)


def _daily_quota_exhausted(text: str) -> bool:
    if any("PerDay" in q for q in _quota_ids(text)):          # Google AI Studio
        return True
    return "free-models-per-day" in text                      # OpenRouter free-model daily cap


def _quota_summary(text: str) -> str:
    ids = _quota_ids(text)
    values = re.findall(r'"quotaValue":\s*"([^"]+)"', text)
    parts = [f"{q}={v}" for q, v in zip(ids, values)] or ids
    return ", ".join(parts) if parts else text[:300]


class OpenAICompatChatModel:
    def __init__(self, model: str, base_url: str, api_key_env: str, client: Optional[httpx.Client] = None,
                 reasoning_effort: Optional[str] = None, min_interval: float = 0.0, max_retries: Optional[int] = None,
                 timeout: float = 180.0, api_key: Optional[str] = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        key = api_key or os.environ.get(api_key_env, "")
        if not key:
            raise LLMError(f"{api_key_env} is not set.")
        self._headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self._client = client or httpx.Client(timeout=timeout)
        self.reasoning_effort = reasoning_effort
        self.min_interval = min_interval
        self.max_retries = max_retries if max_retries is not None else int(os.environ.get("LLM_MAX_RETRIES", "6") or 6)
        self._last_call = 0.0
        self._sleep = time.sleep

    @staticmethod
    def _to_wire(system: str, messages: List[Message]) -> List[Dict[str, Any]]:
        wire: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "assistant":
                if m.raw is not None:
                    wire.append(m.raw)  # replayed exactly as returned (keeps provider signatures)
                    continue
                msg: Dict[str, Any] = {"role": "assistant", "content": m.text or None}
                if m.tool_calls:
                    msg["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                         for c in m.tool_calls]
                wire.append(msg)
                continue
            for r in m.tool_results:
                wire.append({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content})
            if m.text:
                wire.append({"role": "user", "content": m.text})
        return wire

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        delay = 5.0
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = self._client.post(url, headers=self._headers, json=body)
            except httpx.HTTPError as exc:
                if attempt == self.max_retries:
                    raise LLMError(f"network error: {type(exc).__name__}") from None
                self._sleep(delay)
                delay = min(delay * 2, 90)
                continue
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    raise LLMError("provider returned a non-JSON response") from None
                err = data.get("error") if isinstance(data, dict) and not data.get("choices") else None
                if not err:
                    return data
                # OpenRouter reports upstream failures inside a 200 body; treat them like the HTTP status.
                code = int(err.get("code") or 502) if isinstance(err, dict) else 502
                text = json.dumps(err)[:300]
                if code == 429 and _daily_quota_exhausted(text):
                    raise LLMError(f"HTTP 429 QUOTA_EXHAUSTED: {text}")
                if code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    self._sleep(delay)
                    delay = min(delay * 2, 90)
                    continue
                raise LLMError(f"HTTP {code}: {text}")
            if resp.status_code == 429 and _daily_quota_exhausted(resp.text):
                # A per-day quota will not recover within any sensible retry window.
                raise LLMError(f"HTTP 429 QUOTA_EXHAUSTED: {_quota_summary(resp.text)}")
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("retry-after")
                hinted = None
                if retry_after:
                    try:
                        hinted = float(retry_after)
                    except ValueError:
                        hinted = None
                if hinted is None:
                    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', resp.text)
                    hinted = float(m.group(1)) if m else None
                self._sleep(min(hinted if hinted is not None else delay, 120))
                delay = min(delay * 2, 90)
                continue
            # Never echo request headers; the body of an error response contains no credentials.
            detail = _quota_summary(resp.text) if resp.status_code == 429 else resp.text[:300]
            raise LLMError(f"HTTP {resp.status_code}: {detail}")
        raise LLMError("retries exhausted")

    def complete(self, system: str, messages: List[Message], tools: List[ToolSpec], max_tokens: int) -> ModelResponse:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": self._to_wire(system, messages),
            "tools": [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                         "parameters": sanitize_schema(t.input_schema)}} for t in tools],
            "tool_choice": "auto",
            "max_tokens": max_tokens,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        started = time.monotonic()
        data = self._post(body)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"no choices in response: {json.dumps(data)[:300]}")
        msg = choices[0].get("message") or {}
        calls: List[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except ValueError:
                args = {"__invalid_json__": str(raw_args)[:500]}
            if not tc.get("id"):
                tc["id"] = f"call_{uuid.uuid4().hex[:12]}"
            calls.append(ToolCall(id=tc["id"], name=fn.get("name", ""), arguments=args))
        usage = data.get("usage") or {}
        raw = {k: v for k, v in msg.items() if v is not None}
        raw["role"] = "assistant"
        return ModelResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            usage=Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
            stop_reason=str(choices[0].get("finish_reason") or ""),
            model=str(data.get("model") or self.model),
            latency_seconds=time.monotonic() - started,
            raw=raw,
        )

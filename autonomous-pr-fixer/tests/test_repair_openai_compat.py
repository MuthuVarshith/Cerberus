"""OpenAI-compatible (Gemini) adapter with a mocked HTTP transport: no network, no key, no cost."""
from __future__ import annotations

import json

import httpx
import pytest

from repair.config import RepairConfig
from repair.llm.openai_compat import GEMINI_BASE_URL, OpenAICompatChatModel, sanitize_schema
from repair.llm.provider import LLMError, Message, ToolResult, create_model
from repair.tools import build_registry

KEY = "AIzaFAKEKEY-not-real-0123456789"


def _ok(tool_calls=None, content=None, extra=None):
    msg = {"role": "assistant", "content": content, "tool_calls": tool_calls}
    if extra:
        msg.update(extra)
    return httpx.Response(200, json={"model": "gemini-2.5-flash", "choices": [{"message": msg, "finish_reason": "tool_calls"}],
                                     "usage": {"prompt_tokens": 120, "completion_tokens": 30}})


def _model(handler):
    m = OpenAICompatChatModel("gemini-2.5-flash", GEMINI_BASE_URL, "UNUSED", api_key=KEY,
                              client=httpx.Client(transport=httpx.MockTransport(handler)), reasoning_effort="medium")
    m._sleep = lambda s: None
    return m


def test_tool_schemas_are_sanitized_for_function_calling():
    for spec in build_registry().specs():
        text = json.dumps(sanitize_schema(spec.input_schema))
        assert '"anyOf"' not in text and '"title"' not in text and '"default"' not in text and "$ref" not in text
    read = next(s for s in build_registry().specs() if s.name == "read_file")
    assert sanitize_schema(read.input_schema)["properties"]["end_line"]["type"] == "integer"


def test_request_shape_tool_parsing_and_raw_replay():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        assert req.headers["authorization"] == f"Bearer {KEY}"
        return _ok([{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}],
                   extra={"extra_content": {"google": {"thought_signature": "sig=="}}})

    m = _model(handler)
    specs = build_registry().specs()
    out = m.complete("sys", [Message("user", "fix it")], specs, 4000)
    assert out.tool_calls[0].name == "read_file" and out.tool_calls[0].arguments == {"path": "a.py"}
    assert out.usage.input_tokens == 120 and out.usage.output_tokens == 30
    body = seen[0]
    assert body["messages"][0] == {"role": "system", "content": "sys"} and body["tool_choice"] == "auto"
    assert body["reasoning_effort"] == "medium" and "temperature" not in body
    history = [Message("user", "fix it"), Message("assistant", tool_calls=out.tool_calls, raw=out.raw),
               Message("user", tool_results=[ToolResult("c1", '{"content": "x"}')])]
    m.complete("sys", history, specs, 4000)
    replayed = seen[1]["messages"]
    assert replayed[2]["extra_content"] == {"google": {"thought_signature": "sig=="}}   # unchanged
    assert replayed[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"content": "x"}'}


def test_rate_limit_is_retried_with_server_delay():
    calls, slept = [], []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, text='{"error": {"details": [{"retryDelay": "7s"}]}}')
        return _ok(content="done")

    m = _model(handler)
    m._sleep = slept.append
    assert m.complete("s", [Message("user", "x")], [], 100).text == "done"
    assert len(calls) == 3 and slept[:2] == [7.0, 7.0]


def test_errors_never_contain_the_key():
    m = _model(lambda req: httpx.Response(400, text='{"error": "bad request"}'))
    with pytest.raises(LLMError) as exc:
        m.complete("s", [Message("user", "x")], [], 100)
    assert KEY not in str(exc.value) and "400" in str(exc.value)


def test_missing_key_is_an_llm_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMError, match="not set"):
        create_model("gemini", "gemini-2.5-flash")


def test_gemini_config_defaults(monkeypatch):
    for k in ("LLM_MODEL", "LLM_INPUT_PRICE_PER_MTOK", "LLM_OUTPUT_PRICE_PER_MTOK", "LLM_FREE_TIER",
              "LLM_MIN_SECONDS_BETWEEN_CALLS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    cfg = RepairConfig.from_env()
    assert cfg.model == "gemini-3.8-flash" and cfg.min_seconds_between_calls == 6.0
    assert cfg.input_price_per_mtok == 0.0 and cfg.output_price_per_mtok == 0.0
    monkeypatch.setenv("LLM_FREE_TIER", "false")
    assert RepairConfig.from_env().input_price_per_mtok is None

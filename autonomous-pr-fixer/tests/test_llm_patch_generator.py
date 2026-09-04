"""
Tests for LLM-backed patch generation.

The model call itself is injected, so these run offline and deterministically.
What is exercised for real is everything around it: prompt assembly, diff
extraction from a chatty reply, token accounting, provider-error handling, and
the generator driving a real `PatchAgent` loop against a real sandbox.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.llm_patch_generator import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_OPENAI_MODEL,
    GenerationUsage,
    LLMPatchGenerator,
    PatchGenerationError,
    ScriptedPatchGenerator,
    has_api_key,
    resolve_model,
)
from agents.patch_agent import PatchAgent
from harness.docker_sandbox import Sandbox

GOOD_DIFF = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)


class _FakeResponse:
    """Minimal stand-in for an OpenAI-shaped completion response."""

    class _Message:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _FakeResponse._Message(content)

    class _Usage:
        def __init__(self, prompt, completion):
            self.prompt_tokens = prompt
            self.completion_tokens = completion

    def __init__(self, content, prompt_tokens=120, completion_tokens=40):
        self.choices = [self._Choice(content)]
        self.usage = self._Usage(prompt_tokens, completion_tokens)


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    """Never let a developer's real key be picked up by these tests."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CERBERUS_MODEL", raising=False)


@pytest.fixture
def sandbox_with_bug():
    with Sandbox() as sb:
        sb.exec("git init && git config user.name 'Bot' && git config user.email 'b@t.co'")
        sb.write_file("calc.py", "def add(a, b):\n    return a - b\n")
        sb.write_file(
            "test_reproduce.py",
            "from calc import add\ndef test_repro(): assert add(2, 3) == 5\n",
        )
        sb.exec("git add -A && git commit -m 'initial'")
        yield sb


def test_missing_api_key_is_an_explicit_error(sandbox_with_bug):
    """Refuse to construct rather than silently producing empty patches."""
    with pytest.raises(PatchGenerationError, match="ANTHROPIC_API_KEY"):
        LLMPatchGenerator(sandbox_with_bug, ["calc.py"], "t", "b")


def test_resolve_model_prefers_explicit_then_env_then_key(monkeypatch):
    assert resolve_model("anthropic/claude-opus-5") == "anthropic/claude-opus-5"
    monkeypatch.setenv("CERBERUS_MODEL", "anthropic/claude-haiku-4-5-20251001")
    assert resolve_model() == "anthropic/claude-haiku-4-5-20251001"
    monkeypatch.delenv("CERBERUS_MODEL")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    assert resolve_model() == DEFAULT_OPENAI_MODEL
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    assert resolve_model() == DEFAULT_ANTHROPIC_MODEL


def test_has_api_key_reflects_environment(monkeypatch):
    assert has_api_key() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    assert has_api_key() is True


def test_prompt_contains_boundary_source_and_feedback(sandbox_with_bug):
    gen = LLMPatchGenerator(
        sandbox_with_bug, ["calc.py"], "add subtracts", "add(2,3) == -1",
        completion_fn=lambda **kw: _FakeResponse(GOOD_DIFF),
    )

    first = gen.build_prompt(1, "unused on the first attempt")
    assert "add subtracts" in first
    assert "return a - b" in first          # real file content, read from the sandbox
    assert "- calc.py" in first
    assert "Previous attempt failed" not in first

    retry = gen.build_prompt(2, "AssertionError: assert -1 == 5")
    assert "Previous attempt failed (attempt 1)" in retry
    assert "AssertionError: assert -1 == 5" in retry


def test_oversized_file_is_truncated_with_a_marker(sandbox_with_bug):
    sandbox_with_bug.write_file("calc.py", "def add(a, b):\n" + "    # pad\n" * 4000)
    gen = LLMPatchGenerator(
        sandbox_with_bug, ["calc.py"], "t", "b", max_file_chars=200,
        completion_fn=lambda **kw: _FakeResponse(GOOD_DIFF),
    )
    prompt = gen.build_prompt(1, "")
    assert "truncated at 200 characters" in prompt


def test_unreadable_candidate_file_is_skipped_not_fatal(sandbox_with_bug):
    gen = LLMPatchGenerator(
        sandbox_with_bug, ["calc.py", "does_not_exist.py"], "t", "b",
        completion_fn=lambda **kw: _FakeResponse(GOOD_DIFF),
    )
    prompt = gen.build_prompt(1, "")
    assert "return a - b" in prompt


def test_diff_is_extracted_from_a_chatty_reply(sandbox_with_bug):
    """Models wrap diffs in prose and fences; only the diff may reach git apply."""
    chatty = f"Sure! Here is the fix:\n\n```diff\n{GOOD_DIFF}```\n\nLet me know if that helps."
    gen = LLMPatchGenerator(
        sandbox_with_bug, ["calc.py"], "t", "b",
        completion_fn=lambda **kw: _FakeResponse(chatty),
    )
    diff = gen(1, "")
    assert diff.startswith("--- a/calc.py")
    assert "Sure!" not in diff
    assert "Let me know" not in diff


def test_usage_is_accumulated_across_attempts(sandbox_with_bug):
    gen = LLMPatchGenerator(
        sandbox_with_bug, ["calc.py"], "t", "b",
        completion_fn=lambda **kw: _FakeResponse(GOOD_DIFF, 100, 25),
    )
    gen(1, "")
    gen(2, "failed")
    assert gen.usage.calls == 2
    assert gen.usage.prompt_tokens == 200
    assert gen.usage.completion_tokens == 50
    assert gen.usage.total_tokens == 250


def test_provider_error_yields_an_empty_diff_not_a_crash(sandbox_with_bug):
    """One bad API call must be a failed attempt, not a dead pipeline."""
    def _boom(**kwargs):
        raise RuntimeError("429 rate limited")

    gen = LLMPatchGenerator(
        sandbox_with_bug, ["calc.py"], "t", "b", completion_fn=_boom,
    )
    assert gen(1, "") == ""
    assert "429 rate limited" in gen.transcript[0]


def test_model_id_is_passed_through_to_the_provider(sandbox_with_bug):
    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return _FakeResponse(GOOD_DIFF)

    gen = LLMPatchGenerator(
        sandbox_with_bug, ["calc.py"], "t", "b",
        model="anthropic/claude-opus-5", completion_fn=_capture,
    )
    gen(1, "")
    assert seen["model"] == "anthropic/claude-opus-5"
    assert seen["temperature"] == 0.0
    assert seen["messages"][0]["role"] == "system"
    assert "unified diff and nothing else" in seen["messages"][0]["content"]


def test_generator_drives_the_real_patch_loop_to_green(sandbox_with_bug):
    """End-to-end through PatchAgent: the loop applies the diff and the RED test passes."""
    gen = LLMPatchGenerator(
        sandbox_with_bug, ["calc.py"], "add subtracts", "add(2,3) == -1",
        completion_fn=lambda **kw: _FakeResponse(GOOD_DIFF),
    )
    result = PatchAgent(sandbox_with_bug, max_attempts=3).run_patch_loop(["calc.py"], gen)

    assert result.reached_green is True
    assert result.total_attempts == 1
    assert result.is_empty_diff is False
    assert gen.usage.total_tokens > 0


def test_loop_retries_with_feedback_and_recovers(sandbox_with_bug):
    """A first bad patch must produce structured feedback the next attempt can see."""
    wrong = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
        "-    return a - b\n+    return a * b\n"
    )
    prompts = []

    def _two_stage(**kwargs):
        prompts.append(kwargs["messages"][1]["content"])
        return _FakeResponse(wrong if len(prompts) == 1 else GOOD_DIFF)

    gen = LLMPatchGenerator(
        sandbox_with_bug, ["calc.py"], "add subtracts", "add(2,3) == -1",
        completion_fn=_two_stage,
    )
    result = PatchAgent(sandbox_with_bug, max_attempts=3).run_patch_loop(["calc.py"], gen)

    assert result.reached_green is True
    assert result.total_attempts == 2
    # The retry prompt carried the verifier's own failure output.
    assert "Previous attempt failed" in prompts[1]
    assert "AssertionError" in prompts[1] or "assert" in prompts[1]


def test_scripted_generator_walks_its_list_then_repeats():
    gen = ScriptedPatchGenerator(["one", "two"])
    assert gen(1, "") == "one"
    assert gen(2, "") == "two"
    # Repeating the last diff makes run_patch_loop abort on the duplicate rather
    # than spin through the remaining budget.
    assert gen(3, "") == "two"
    assert gen.calls == 3


def test_scripted_generator_requires_at_least_one_diff():
    with pytest.raises(PatchGenerationError):
        ScriptedPatchGenerator([])


def test_usage_add_is_cumulative():
    usage = GenerationUsage()
    usage.add(10, 5)
    usage.add(1, 2)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, usage.calls) == (
        11, 7, 18, 2,
    )

"""
Tests for LLM-backed reproduction test synthesis.

Exercises prompt assembly, test code extraction, token accounting,
RED-gate verification with injected completions, and recovery from bad initial tests.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.llm_reproduction_synthesizer import (
    LLMReproductionSynthesizer,
    ReproductionSynthesisError,
)
from harness.docker_sandbox import Sandbox


class _FakeResponse:
    """Minimal stand-in for an LLM completion response."""

    class _Message:
        def __init__(self, content: str):
            self.content = content

    class _Choice:
        def __init__(self, content: str):
            self.message = _FakeResponse._Message(content)

    class _Usage:
        def __init__(self, prompt: int = 150, completion: int = 50):
            self.prompt_tokens = prompt
            self.completion_tokens = completion

    def __init__(self, content: str, prompt_tokens: int = 150, completion_tokens: int = 50):
        self.choices = [self._Choice(content)]
        self.usage = self._Usage(prompt_tokens, completion_tokens)


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    """Ensure no ambient API keys leak into these tests."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CERBERUS_MODEL", raising=False)


@pytest.fixture
def sandbox_with_code():
    with Sandbox() as sb:
        sb.exec("git init && git config user.name \"Bot\" && git config user.email \"b@t.co\"")
        sb.write_file("calc.py", "def add(a, b):\n    return a - b\n")
        sb.exec("git add -A && git commit -m \"initial\"")
        yield sb


def test_missing_api_key_raises(sandbox_with_code):
    """Refuse to construct without credentials when no completion_fn is provided."""
    with pytest.raises(ReproductionSynthesisError, match="No ANTHROPIC_API_KEY or OPENAI_API_KEY"):
        LLMReproductionSynthesizer(
            sandbox_with_code,
            candidate_files=["calc.py"],
            issue_title="Wrong add",
            issue_body="add(2, 3) gives -1",
        )


def test_prompt_contains_issue_and_sources(sandbox_with_code):
    synth = LLMReproductionSynthesizer(
        sandbox_with_code,
        candidate_files=["calc.py"],
        issue_title="Bug in add()",
        issue_body="Calling add(2, 3) returns -1 instead of 5",
        completion_fn=lambda **kw: _FakeResponse("code"),
    )

    first = synth.build_prompt(1, "")
    assert "Bug in add()" in first
    assert "add(2, 3) returns -1" in first
    assert "calc.py" in first
    assert "return a - b" in first
    assert "Previous attempt failed" not in first

    retry = synth.build_prompt(2, "AssertionError: test passed unexpectedly")
    assert "Previous attempt failed" in retry
    assert "AssertionError" in retry


def test_extract_test_code_fenced_and_raw():
    fenced_py = "Here is the test:\n```python\nimport pytest\ndef test_it(): assert 1 == 2\n```\nEnjoy!"
    assert LLMReproductionSynthesizer.extract_test_code(fenced_py) == "import pytest\ndef test_it(): assert 1 == 2"

    fenced_generic = "```\ndef test_generic(): assert False\n```"
    assert LLMReproductionSynthesizer.extract_test_code(fenced_generic) == "def test_generic(): assert False"

    raw = "def test_raw(): assert False"
    assert LLMReproductionSynthesizer.extract_test_code(raw) == "def test_raw(): assert False"


def test_synthesize_and_verify_passes_red_gate(sandbox_with_code):
    """A valid failing test passes the RED gate and accumulates tokens."""
    failing_test = (
        "```python\n"
        "from calc import add\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "```"
    )
    synth = LLMReproductionSynthesizer(
        sandbox_with_code,
        candidate_files=["calc.py"],
        issue_title="Add subtracts",
        issue_body="add(2,3) gives -1",
        completion_fn=lambda **kw: _FakeResponse(failing_test),
    )

    result = synth.synthesize_and_verify(max_attempts=2)
    assert result.reproduced is True
    assert result.returncode != 0
    assert "assert add(2, 3) == 5" in result.test_code
    assert synth.usage.total_tokens == 200
    assert synth.usage.calls == 1


def test_synthesize_and_verify_recovers_from_syntax_error(sandbox_with_code):
    """If attempt 1 produces broken syntax, attempt 2 sees the trace and recovers."""
    broken = "```python\ndef test_broken(:\n    assert True\n```"
    valid_failing = (
        "```python\n"
        "from calc import add\n"
        "def test_add():\n"
        "    assert add(10, 5) == 15\n"
        "```"
    )

    prompts = []

    def _two_stage(**kwargs):
        prompts.append(kwargs["messages"][1]["content"])
        return _FakeResponse(broken if len(prompts) == 1 else valid_failing)

    synth = LLMReproductionSynthesizer(
        sandbox_with_code,
        candidate_files=["calc.py"],
        issue_title="Add broken",
        issue_body="add should add numbers",
        completion_fn=_two_stage,
    )

    result = synth.synthesize_and_verify(max_attempts=3)
    assert result.reproduced is True
    assert len(prompts) == 2
    assert "SyntaxError" in prompts[1]
    assert synth.usage.calls == 2
    assert synth.usage.total_tokens == 400


def test_synthesize_and_verify_rejects_passing_test(sandbox_with_code):
    """A test that passes on unpatched code does NOT prove a bug; RED gate must reject."""
    passing_test = (
        "```python\n"
        "from calc import add\n"
        "def test_passing():\n"
        "    assert add(2, 3) == -1\n"
        "```"
    )
    synth = LLMReproductionSynthesizer(
        sandbox_with_code,
        candidate_files=["calc.py"],
        issue_title="Fake issue",
        issue_body="Everything works",
        completion_fn=lambda **kw: _FakeResponse(passing_test),
    )

    result = synth.synthesize_and_verify(max_attempts=2)
    assert result.reproduced is False
    assert result.refusal_code == "RED_NOT_FAILING"

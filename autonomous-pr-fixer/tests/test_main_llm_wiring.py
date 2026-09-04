"""
Tests that the model is genuinely in the loop when asked for, and never otherwise.

`main.py` previously wrote the fix itself, so no model could reach the patch
stage at all. These pin the three cases that matter: off by default, an explicit
request without credentials degrades loudly instead of pretending, and an
enabled run drives a real `LLMPatchGenerator` whose token count is reported as
the measurement it now is.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main
from agents.llm_patch_generator import LLMPatchGenerator

ISSUE_KWARGS = dict(
    issue_number=101,
    issue_title="Divide by zero in rate_calculator",
    issue_body="calculate_rate(10, 0) throws ZeroDivisionError",
)


class _FakeResponse:
    class _Message:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _FakeResponse._Message(content)

    class _Usage:
        def __init__(self):
            self.prompt_tokens = 900
            self.completion_tokens = 60

    def __init__(self, content):
        self.choices = [self._Choice(content)]
        self.usage = self._Usage()


def _guard_fix(path: str) -> str:
    """A diff that adds the zero check `main.py`'s reproduction test demands."""
    return (
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,3 +1,5 @@\n"
        " def calculate_rate(amount: float, total: float) -> float:\n"
        '     """Calculate the rate as amount / total."""\n'
        "+    if total == 0:\n"
        "+        return 0.0\n"
        "     return amount / total\n"
    )


@pytest.fixture(autouse=True)
def _no_ambient_llm_config(monkeypatch):
    monkeypatch.delenv("CERBERUS_USE_LLM", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CERBERUS_MODEL", raising=False)


@pytest.fixture
def recorded_publishes(monkeypatch):
    calls: list[dict] = []

    def _fake_publish(self, **kwargs):
        calls.append(kwargs)
        return {"status": "dry_run_success", "pr_url": None, "display_url": "recorded"}

    monkeypatch.setattr(main.PRPublisher, "publish_pr", _fake_publish)
    return calls


@pytest.fixture
def constructed(monkeypatch):
    """Replace the generator class with a factory that records and injects.

    The injected `completion_fn` is the only way to exercise the real generator
    here: no provider credential exists in this environment.
    """
    made: list[LLMPatchGenerator] = []

    def _factory(**kwargs):
        path = kwargs["candidate_files"][0]
        gen = LLMPatchGenerator(
            **kwargs, completion_fn=lambda **kw: _FakeResponse(_guard_fix(path))
        )
        made.append(gen)
        return gen

    monkeypatch.setattr(main, "LLMPatchGenerator", _factory)
    return made


def test_llm_is_off_by_default(constructed, recorded_publishes, tmp_path):
    """The offline scripted repair stays the default, key present or not."""
    assert main.run_pipeline(repo_dir=str(tmp_path), mode="local", **ISSUE_KWARGS) is True
    assert constructed == []


def test_request_without_credentials_falls_back_instead_of_failing(
    constructed, recorded_publishes, tmp_path, capsys
):
    admitted = main.run_pipeline(
        repo_dir=str(tmp_path), mode="local", use_llm=True, **ISSUE_KWARGS
    )
    out = capsys.readouterr().out

    assert admitted is True
    assert constructed == []            # never constructed without a key
    assert "no ANTHROPIC_API_KEY" in out
    assert "Falling back" in out


def test_enabled_run_drives_the_generator_and_reports_real_tokens(
    constructed, recorded_publishes, monkeypatch, tmp_path
):
    monkeypatch.setattr(main, "has_api_key", lambda: True)

    admitted = main.run_pipeline(
        repo_dir=str(tmp_path), mode="local", use_llm=True, **ISSUE_KWARGS
    )

    assert admitted is True
    assert len(constructed) == 1
    gen = constructed[0]
    # The model produced the patch that passed the RED test, so tokens were spent.
    assert gen.usage.calls == 1
    assert gen.usage.total_tokens == 960
    # ...and that count reaches the evidence report as a measurement, replacing
    # the "not measured" label the scripted path is honest enough to print.
    body = recorded_publishes[0]["pr_body"]
    assert "960 tokens" in body
    assert "not measured" not in body


def test_env_var_enables_the_llm_path_too(constructed, recorded_publishes, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "has_api_key", lambda: True)
    monkeypatch.setenv("CERBERUS_USE_LLM", "1")

    assert main.run_pipeline(repo_dir=str(tmp_path), mode="local", **ISSUE_KWARGS) is True
    assert len(constructed) == 1


def test_explicit_false_beats_the_env_var(constructed, recorded_publishes, monkeypatch, tmp_path):
    """An explicit argument wins, so a caller can force the deterministic path."""
    monkeypatch.setattr(main, "has_api_key", lambda: True)
    monkeypatch.setenv("CERBERUS_USE_LLM", "1")

    assert (
        main.run_pipeline(repo_dir=str(tmp_path), mode="local", use_llm=False, **ISSUE_KWARGS)
        is True
    )
    assert constructed == []

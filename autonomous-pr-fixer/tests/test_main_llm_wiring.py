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


def test_real_repo_without_llm_does_not_fall_back_to_demo_fix(
    constructed, recorded_publishes, tmp_path, capsys
):
    (tmp_path / "service.py").write_text("def identity(value):\n    return value\n", encoding="utf-8")

    admitted = main.run_pipeline(
        repo_dir=str(tmp_path),
        mode="local",
        use_llm=True,
        issue_number=202,
        issue_title="identity should reject None",
        issue_body="identity(None) should raise ValueError at service.py:1",
    )
    out = capsys.readouterr().out

    assert admitted is False
    assert constructed == []
    assert "Real repositories require LLM patching" in out
    assert "rate_calculator" not in out


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


def test_llm_repro_is_off_by_default(tmp_path, monkeypatch):
    """Reproduction test synthesis is scripted by default."""
    repro_constructed = []

    def _fake_synth(**kwargs):
        repro_constructed.append(kwargs)
        raise AssertionError("Should not be called")

    monkeypatch.setattr(main, "LLMReproductionSynthesizer", _fake_synth)
    assert main.run_pipeline(repo_dir=str(tmp_path), mode="local", **ISSUE_KWARGS) is True
    assert repro_constructed == []


def test_llm_repro_drives_synthesizer_when_enabled(tmp_path, monkeypatch, recorded_publishes):
    """When enabled, LLMReproductionSynthesizer generates test_reproduce.py."""
    monkeypatch.setattr(main, "has_api_key", lambda: True)
    repro_calls = []

    class _MockSynth:
        def __init__(self, **kwargs):
            repro_calls.append(kwargs)
            self.model = "mock-model"
            self.usage = type("Usage", (), {"total_tokens": 150})()

        def synthesize_and_verify(self, max_attempts=3):
            # Write a valid failing reproduction test directly
            test_code = "from rate_calculator import calculate_rate\ndef test_fail(): assert calculate_rate(10, 0) == 0.0\n"
            repro_agent = main.ReproductionAgent(repro_calls[-1]["sandbox"])
            return repro_agent.run_reproduction_gate("Title", "Body", test_code)

    monkeypatch.setattr(main, "LLMReproductionSynthesizer", _MockSynth)

    assert (
        main.run_pipeline(
            repo_dir=str(tmp_path), mode="local", use_llm_repro=True, **ISSUE_KWARGS
        )
        is True
    )
    assert len(repro_calls) == 1



def test_real_repo_with_mocked_llm_succeeds(
    constructed, recorded_publishes, tmp_path, monkeypatch, capsys
):
    """Proves the real path works without relying on the demo or live LLM."""
    (tmp_path / "rate_calculator.py").write_text(
        "def calculate_rate(amount: float, total: float) -> float:\n"
        '    """Calculate the rate as amount / total."""\n'
        "    return amount / total\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from rate_calculator import calculate_rate\n"
        "def test_ok(): assert calculate_rate(10, 2) == 5.0\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    import subprocess
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(tmp_path), check=True)


    monkeypatch.setattr(main, "has_api_key", lambda: True)

    class _MockSynth:
        def __init__(self, **kwargs):
            self.sandbox = kwargs["sandbox"]
            self.model = "mock-model"
            self.usage = type("Usage", (), {"total_tokens": 150})()

        def synthesize_and_verify(self, max_attempts=3):
            test_code = (
                "from rate_calculator import calculate_rate\n"
                "def test_fail(): assert calculate_rate(10, 0) == 0.0\n"
            )
            repro_agent = main.ReproductionAgent(self.sandbox)
            return repro_agent.run_reproduction_gate("Title", "Body", test_code)

    monkeypatch.setattr(main, "LLMReproductionSynthesizer", _MockSynth)

    admitted = main.run_pipeline(
        repo_dir=str(tmp_path),
        mode="local",
        use_llm=True,
        use_llm_repro=True,
        issue_number=1,
        issue_title="Divide by zero",
        issue_body="calculate_rate(10, 0) throws ZeroDivisionError at rate_calculator.py:1",
    )

    out = capsys.readouterr().out
    assert admitted is True
    assert len(constructed) == 1
    assert "Model-generated patches, boundary: rate_calculator.py" in out
    assert "Generated and verified test_reproduce.py via model" in out

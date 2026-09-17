"""
Tests that a model is in the loop only when asked for, and never silently replaced.

Pinned cases: model paths are off by default; a request without credentials is
refused rather than quietly substituted by a scripted repair; an enabled run
drives a real `LLMPatchGenerator` whose token count reaches the evidence report.
"""
from __future__ import annotations

import pytest

import main
from agents.llm_patch_generator import LLMPatchGenerator
from examples.demo import prepare_rate_calculator_demo


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


@pytest.fixture
def scenario(tmp_path):
    return prepare_rate_calculator_demo(str(tmp_path / "fixture"))


@pytest.fixture
def recorded_publishes(monkeypatch):
    calls: list[dict] = []

    def _fake_publish(self, **kwargs):
        calls.append(kwargs)
        return {"status": "dry_run_success", "pr_url": None, "display_url": "recorded"}

    monkeypatch.setattr(main.PRPublisher, "publish_pr", _fake_publish)
    return calls


@pytest.fixture
def constructed(monkeypatch, scenario):
    """Replace the generator class with a factory that injects a fake transport.

    The injected `completion_fn` is the only way to exercise the real generator
    here: no provider credential exists in this environment.
    """
    made: list[LLMPatchGenerator] = []

    def _factory(**kwargs):
        gen = LLMPatchGenerator(**kwargs, completion_fn=lambda **kw: _FakeResponse(scenario.patch_diff))
        made.append(gen)
        return gen

    monkeypatch.setattr(main, "LLMPatchGenerator", _factory)
    return made


def _run(scenario, **kwargs):
    base = dict(
        repo_dir=scenario.repo_dir,
        issue_number=scenario.issue_number,
        issue_title=scenario.issue_title,
        issue_body=scenario.issue_body,
        mode="local",
        repro_test_code=scenario.repro_test_code,
    )
    base.update(kwargs)
    return main.run_pipeline(**base)


def test_llm_is_off_by_default(constructed, recorded_publishes, scenario, capsys):
    """Without a patch source and without opting in, nothing generates a patch."""
    assert _run(scenario) is False
    assert constructed == []
    assert "No patch source is configured" in capsys.readouterr().out


def test_request_without_credentials_is_refused_not_substituted(constructed, recorded_publishes, scenario, capsys):
    admitted = _run(scenario, use_llm=True)
    out = capsys.readouterr().out

    assert admitted is False
    assert constructed == []
    assert "no ANTHROPIC_API_KEY" in out
    assert recorded_publishes == []


def test_enabled_run_drives_the_generator_and_reports_real_tokens(
    constructed, recorded_publishes, monkeypatch, scenario
):
    monkeypatch.setattr(main, "has_api_key", lambda: True)

    assert _run(scenario, use_llm=True) is True
    assert len(constructed) == 1
    gen = constructed[0]
    assert gen.usage.calls == 1
    assert gen.usage.total_tokens == 960
    body = recorded_publishes[0]["pr_body"]
    assert "960 tokens" in body
    assert "not measured" not in body


def test_env_var_enables_the_llm_path_too(constructed, recorded_publishes, monkeypatch, scenario):
    monkeypatch.setattr(main, "has_api_key", lambda: True)
    monkeypatch.setenv("CERBERUS_USE_LLM", "1")

    assert _run(scenario) is True
    assert len(constructed) == 1


def test_explicit_false_beats_the_env_var(constructed, recorded_publishes, monkeypatch, scenario):
    monkeypatch.setattr(main, "has_api_key", lambda: True)
    monkeypatch.setenv("CERBERUS_USE_LLM", "1")

    assert _run(scenario, use_llm=False) is False
    assert constructed == []


def test_llm_repro_is_off_by_default(monkeypatch, scenario):
    def _fake_synth(**kwargs):
        raise AssertionError("Should not be called")

    monkeypatch.setattr(main, "LLMReproductionSynthesizer", _fake_synth)
    assert _run(scenario, repro_test_code=None) is False


def test_llm_repro_requires_a_file_reference(monkeypatch, scenario, capsys):
    monkeypatch.setattr(main, "has_api_key", lambda: True)
    admitted = _run(
        scenario,
        repro_test_code=None,
        use_llm_repro=True,
        issue_title="Something is wrong",
        issue_body="it crashes sometimes",
    )
    assert admitted is False
    assert "needs a concrete source file" in capsys.readouterr().out


def test_llm_repro_and_patch_drive_both_models(constructed, recorded_publishes, monkeypatch, scenario, capsys):
    monkeypatch.setattr(main, "has_api_key", lambda: True)
    synth_calls = []

    class _MockSynth:
        def __init__(self, **kwargs):
            synth_calls.append(kwargs)
            self.sandbox = kwargs["sandbox"]
            self.model = "mock-model"
            self.usage = type("Usage", (), {"total_tokens": 150})()

        def synthesize_and_verify(self, max_attempts=3):
            repro_agent = main.ReproductionAgent(self.sandbox)
            return repro_agent.run_reproduction_gate("Title", "Body", scenario.repro_test_code)

    monkeypatch.setattr(main, "LLMReproductionSynthesizer", _MockSynth)

    admitted = _run(scenario, repro_test_code=None, use_llm=True, use_llm_repro=True)
    out = capsys.readouterr().out
    assert admitted is True
    assert len(synth_calls) == 1
    assert synth_calls[0]["candidate_files"] == ["rate_calculator.py"]
    assert len(constructed) == 1
    assert "Model-generated patches, boundary: rate_calculator.py" in out
    assert "1110 tokens" in recorded_publishes[0]["pr_body"]

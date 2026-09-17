import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _isolated_run_artifacts(tmp_path, monkeypatch):
    """Keep run.json artifacts out of the repository's artifacts/ directory."""
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("RUN_ARTIFACTS_DIR", str(artifacts))
    return artifacts


@pytest.fixture(autouse=True)
def _no_ambient_llm_config(monkeypatch):
    """A developer's shell must not switch tests onto a paid model path."""
    for key in (
        "CERBERUS_USE_LLM",
        "CERBERUS_USE_LLM_REPRO",
        "CERBERUS_MODEL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _explicit_test_sandbox(monkeypatch):
    """Tests run repository fixtures written by the tests themselves.

    Docker is the default and fails closed when unavailable; the suite opts in
    to host-unsafe execution explicitly so it can run on machines without
    Docker. Tests of the Docker path set CERBERUS_SANDBOX themselves.
    """
    monkeypatch.setenv("CERBERUS_SANDBOX", "host-unsafe")

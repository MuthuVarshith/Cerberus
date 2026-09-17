"""Repository setup detection.

Reads repository metadata from the workspace (no commands are executed here)
and proposes dependency installation commands and a test command. The test
command is always pytest-based, because the gates require JUnit XML results.

Installation commands use pip only, because the sandbox image provides pip and
pytest and nothing else. Poetry and uv projects are installed through their
PEP 517 build backend with `pip install -e .`.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

from harness.docker_sandbox import Sandbox

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the project requires 3.11+
    tomllib = None

_TEST_EXTRAS = ("test", "tests", "testing", "dev")
_EXTRA_REQUIREMENTS = ("requirements-dev.txt", "requirements-test.txt", "requirements_test.txt", "dev-requirements.txt", "test-requirements.txt")


@dataclass
class RepoSetupPlan:
    install_commands: List[str] = field(default_factory=list)
    test_command: str = ""
    package_manager: str = "unknown"
    test_framework: str = "unknown"
    reason: str = ""


class RepoSetupAgent:
    """Detects install and test commands for a checked-out Python repository."""

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox

    def _pyproject_extras(self) -> List[str]:
        if tomllib is None:
            return []
        try:
            with open(os.path.join(self.sandbox.workspace_dir, "pyproject.toml"), "rb") as f:
                data = tomllib.load(f)
        except (OSError, ValueError):
            return []
        extras = (data.get("project") or {}).get("optional-dependencies") or {}
        return [name for name in _TEST_EXTRAS if name in extras]

    def detect(self) -> RepoSetupPlan:
        ws = self.sandbox.workspace_dir
        files = set(os.listdir(ws))
        py = self.sandbox.python_cmd
        install: List[str] = []
        reasons: List[str] = []
        manager = "none"

        if "pyproject.toml" in files or "setup.py" in files:
            extras = self._pyproject_extras() if "pyproject.toml" in files else []
            target = f".[{','.join(extras)}]" if extras else "."
            install.append(f"{py} -m pip install -e \"{target}\"")
            manager = "pip"
            reasons.append("pyproject.toml/setup.py" + (f" with extras {extras}" if extras else ""))
        if "requirements.txt" in files:
            install.append(f"{py} -m pip install -r requirements.txt")
            manager = "pip"
            reasons.append("requirements.txt")
        for extra in _EXTRA_REQUIREMENTS:
            if extra in files:
                install.append(f"{py} -m pip install -r {extra}")
                manager = "pip"
                reasons.append(extra)

        has_tests = (
            os.path.isdir(os.path.join(ws, "tests"))
            or os.path.isdir(os.path.join(ws, "test"))
            or any(name.startswith("test_") and name.endswith(".py") for name in files)
            or bool({"pytest.ini", "tox.ini", "setup.cfg", "conftest.py"} & files)
            or "pyproject.toml" in files
        )
        if not has_tests:
            return RepoSetupPlan(
                install_commands=install,
                package_manager=manager,
                reason="No Python test configuration detected.",
            )
        return RepoSetupPlan(
            install_commands=install,
            test_command=f"{py} -m pytest -q",
            package_manager=manager,
            test_framework="pytest",
            reason="Detected " + (", ".join(reasons) if reasons else "tests without dependency manifests") + ".",
        )

"""Repository setup detection for real repair runs.

The repair harness should make an honest attempt to use the target repository's
own dependency and test conventions instead of silently substituting a demo
test suite.  This module keeps that detection small and explicit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from harness.docker_sandbox import Sandbox


@dataclass
class RepoSetupPlan:

    install_commands: List[str] = field(default_factory=list)
    test_command: str = ""
    package_manager: str = "unknown"
    test_framework: str = "unknown"
    reason: str = ""
    baseline_failures: List[str] = field(default_factory=list)  # tests that already fail before any patch
    initial_commit: Optional[str] = None  # git commit hash at start


class RepoSetupAgent:
    """Detects install and regression commands for a checked-out repository."""

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox

    def detect(self) -> RepoSetupPlan:
        files = set(os.listdir(self.sandbox.workspace_dir))
        py = self.sandbox.python_cmd

        def _choose_test_cmd() -> str:
            """Select test command based on config files.
            Preference order: tox.ini > noxfile.py > pytest.ini > default pytest.
            """
            if "tox.ini" in files:
                return "tox"
            if "noxfile.py" in files:
                return "nox -s tests"
            if "pytest.ini" in files:
                return f"{py} -m pytest -c pytest.ini -q"
            return f"{py} -m pytest -q"

        plan: RepoSetupPlan
        if "pyproject.toml" in files:
            pyproject = self._read_optional("pyproject.toml")
            if "tool.poetry" in pyproject and "poetry.lock" in files:
                plan = RepoSetupPlan(
                    install_commands=["poetry install --no-interaction"],
                    test_command="poetry run pytest -q",
                    package_manager="poetry",
                    test_framework="pytest",
                    reason="Detected pyproject.toml with Poetry metadata.",
                )
            elif "tool.uv" in pyproject or "uv.lock" in files:
                plan = RepoSetupPlan(
                    install_commands=["uv sync"],
                    test_command="uv run pytest -q",
                    package_manager="uv",
                    test_framework="pytest",
                    reason="Detected uv project metadata.",
                )
            else:
                plan = RepoSetupPlan(
                    install_commands=[f"{py} -m pip install -e ."],
                    test_command=_choose_test_cmd(),
                    package_manager="pip",
                    test_framework="pytest",
                    reason="Detected pyproject.toml.",
                )
        elif "requirements.txt" in files:
            plan = RepoSetupPlan(
                install_commands=[f"{py} -m pip install -r requirements.txt"],
                test_command=_choose_test_cmd(),
                package_manager="pip",
                test_framework="pytest",
                reason="Detected requirements.txt.",
            )
        elif "Pipfile" in files:
            plan = RepoSetupPlan(
                install_commands=["pipenv install --dev"],
                test_command="pipenv run pytest -q",
                package_manager="pipenv",
                test_framework="pytest",
                reason="Detected Pipfile.",
            )
        elif os.path.isdir(os.path.join(self.sandbox.workspace_dir, "tests")):
            plan = RepoSetupPlan(
                install_commands=[],
                test_command=_choose_test_cmd(),
                package_manager="none",
                test_framework="pytest",
                reason="Detected tests/ directory.",
            )
        else:
            plan = RepoSetupPlan(
                install_commands=[],
                test_command="",
                package_manager="unknown",
                test_framework="unknown",
                reason="No supported Python dependency or test configuration detected.",
            )

        # Capture initial commit hash if git repo exists
        git_dir = os.path.join(self.sandbox.workspace_dir, ".git")
        if os.path.isdir(git_dir):
            commit_res = self.sandbox.exec("git rev-parse HEAD", timeout=30)
            if commit_res.exit_code == 0:
                plan.initial_commit = commit_res.stdout.strip()

        # Run baseline tests, record failures but do not abort
        if plan.test_command:
            test_res = self.sandbox.exec(plan.test_command, timeout=240)
            if test_res.exit_code != 0:
                failures = []
                for line in test_res.stdout.splitlines():
                    if "::" in line and ("FAIL" in line or "ERROR" in line):
                        failures.append(line.strip())
                plan.baseline_failures = failures

        return plan

    def install(self, plan: RepoSetupPlan, timeout: Optional[int] = None) -> bool:
        """Execute all install commands.
        If any command fails, log stdout/stderr and abort early, returning False.
        """
        for cmd in plan.install_commands:
            res = self.sandbox.exec(cmd, timeout=timeout or 120)
            if res.exit_code != 0:
                # Structured error logging for debugging
                print(f"[REPO_SETUP] Install command failed: {cmd}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
                return False
        return True

    def _read_optional(self, rel_path: str) -> str:
        try:
            return self.sandbox.read_file(rel_path)
        except Exception:
            return ""

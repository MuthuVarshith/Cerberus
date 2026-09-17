"""
Per-repository configuration: `.cerberus.yml` at the repository root.

The file comes from the repository under verification, so it is parsed with
`yaml.safe_load` and validated strictly: unknown keys, wrong types and
out-of-range values are errors, not silently ignored. Its commands run only
inside the sandbox, like any other repository code. When the file is absent,
auto-detection supplies the setup and test commands.

Example:

    version: 1
    setup:
      - python -m pip install -e ".[test]"
    test:
      command: python -m pytest -q        # pytest commands get a JUnit report appended
      # or: command: tox -e py311 -- --junitxml={junit_xml}
      exclude:                            # JUnit test IDs (fnmatch patterns) ignored by the regression gate
        - "tests.test_network::*"
    scope:
      allowed_paths: ["src/**"]           # glob patterns; default is the localized file
      max_files: 3
      max_lines: 200
      allow_test_modifications: false     # changing existing test files is refused by default
    budgets:
      patch_attempts: 5
      red_runs: 3
      green_runs: 3
      command_timeout_seconds: 600
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CONFIG_FILENAME = ".cerberus.yml"
MAX_CONFIG_BYTES = 64 * 1024


class RepoConfigError(ValueError):
    """The repository's .cerberus.yml is present but invalid."""


@dataclass
class ScopeConfig:
    allowed_paths: List[str] = field(default_factory=list)
    max_files: int = 3
    max_lines: int = 200
    allow_test_modifications: bool = False


@dataclass
class BudgetConfig:
    patch_attempts: int = 5
    red_runs: int = 3
    green_runs: int = 3
    command_timeout_seconds: int = 600


@dataclass
class RepoConfig:
    present: bool = False
    setup: Optional[List[str]] = None
    test_command: Optional[str] = None
    test_report: Optional[str] = None
    test_exclude: List[str] = field(default_factory=list)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "present": self.present,
            "setup": self.setup,
            "test_command": self.test_command,
            "test_report": self.test_report,
            "test_exclude": self.test_exclude,
            "scope": vars(self.scope),
            "budgets": vars(self.budgets),
        }


_TOP_KEYS = {"version", "setup", "test", "scope", "budgets"}
_TEST_KEYS = {"command", "report", "exclude"}
_SCOPE_KEYS = {"allowed_paths", "max_files", "max_lines", "allow_test_modifications"}
_BUDGET_LIMITS = {
    "patch_attempts": (1, 20),
    "red_runs": (1, 10),
    "green_runs": (1, 10),
    "command_timeout_seconds": (10, 7200),
}


def _require_mapping(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RepoConfigError(f"{where} must be a mapping.")
    return value


def _reject_unknown(mapping: Dict[str, Any], allowed: set, where: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise RepoConfigError(f"Unknown key(s) in {where}: {', '.join(map(str, unknown))}.")


def _string_list(value: Any, where: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise RepoConfigError(f"{where} must be a list of non-empty strings.")
    return [v.strip() for v in value]


def _int_in_range(value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (low <= value <= high):
        raise RepoConfigError(f"{where} must be an integer between {low} and {high}.")
    return value


def _safe_relative_path(value: str, where: str) -> str:
    norm = value.replace("\\", "/")
    if norm.startswith("/") or ".." in norm.split("/") or ":" in norm:
        raise RepoConfigError(f"{where} must be a relative path inside the repository.")
    return norm


def parse_repo_config(text: str) -> RepoConfig:
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RepoConfigError(f"{CONFIG_FILENAME} is not valid YAML: {exc}") from exc
    data = _require_mapping(data, CONFIG_FILENAME)
    _reject_unknown(data, _TOP_KEYS, CONFIG_FILENAME)
    if data.get("version", 1) != 1:
        raise RepoConfigError(f"{CONFIG_FILENAME}: only version 1 is supported.")

    cfg = RepoConfig(present=True)
    if "setup" in data:
        cfg.setup = _string_list(data["setup"], "setup")

    test = _require_mapping(data.get("test"), "test")
    _reject_unknown(test, _TEST_KEYS, "test")
    if "command" in test:
        if not isinstance(test["command"], str) or not test["command"].strip():
            raise RepoConfigError("test.command must be a non-empty string.")
        cfg.test_command = test["command"].strip()
    if "report" in test:
        if not isinstance(test["report"], str):
            raise RepoConfigError("test.report must be a string.")
        cfg.test_report = _safe_relative_path(test["report"], "test.report")
    cfg.test_exclude = _string_list(test.get("exclude"), "test.exclude")

    scope = _require_mapping(data.get("scope"), "scope")
    _reject_unknown(scope, _SCOPE_KEYS, "scope")
    cfg.scope.allowed_paths = [_safe_relative_path(p, "scope.allowed_paths") for p in _string_list(scope.get("allowed_paths"), "scope.allowed_paths")]
    if "max_files" in scope:
        cfg.scope.max_files = _int_in_range(scope["max_files"], "scope.max_files", 1, 100)
    if "max_lines" in scope:
        cfg.scope.max_lines = _int_in_range(scope["max_lines"], "scope.max_lines", 1, 10000)
    if "allow_test_modifications" in scope:
        if not isinstance(scope["allow_test_modifications"], bool):
            raise RepoConfigError("scope.allow_test_modifications must be true or false.")
        cfg.scope.allow_test_modifications = scope["allow_test_modifications"]

    budgets = _require_mapping(data.get("budgets"), "budgets")
    _reject_unknown(budgets, set(_BUDGET_LIMITS), "budgets")
    for key, (low, high) in _BUDGET_LIMITS.items():
        if key in budgets:
            setattr(cfg.budgets, key, _int_in_range(budgets[key], f"budgets.{key}", low, high))
    return cfg


def load_repo_config(workspace_dir: str) -> RepoConfig:
    """Load `.cerberus.yml` from a workspace. Absent file -> defaults with present=False."""
    path = os.path.join(workspace_dir, CONFIG_FILENAME)
    if not os.path.isfile(path):
        return RepoConfig()
    if os.path.getsize(path) > MAX_CONFIG_BYTES:
        raise RepoConfigError(f"{CONFIG_FILENAME} is larger than {MAX_CONFIG_BYTES} bytes.")
    with open(path, "r", encoding="utf-8") as f:
        return parse_repo_config(f.read())

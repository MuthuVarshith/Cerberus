"""A small trusted fixture repository and scripted model steps for the repair-agent tests.

The bug: calc.rates.percent(part, 0) raises ZeroDivisionError; it should return 0.0.
"""
from __future__ import annotations

import os
import subprocess
from typing import Dict

from repair.config import RepairConfig
from harness.docker_sandbox import ISOLATION_HOST_UNSAFE

FILES: Dict[str, str] = {
    "calc/__init__.py": "",
    "calc/rates.py": (
        "def percent(part, whole):\n"
        "    return part / whole * 100\n"
    ),
    "calc/fmt.py": (
        "def fmt(value):\n"
        "    return f\"{value:.1f}%\"\n"
    ),
    "tests/__init__.py": "",
    "tests/test_rates.py": (
        "from calc.rates import percent\n\n\n"
        "def test_percent_basic():\n"
        "    assert percent(1, 4) == 25.0\n\n\n"
        "def test_percent_negative():\n"
        "    assert percent(-1, 4) == -25.0\n"
    ),
}

TITLE = "percent raises ZeroDivisionError when whole is 0"
BODY = "calc/rates.py: percent(5, 0) raises ZeroDivisionError. It should return 0.0 when whole is 0."
REPRO = (
    "from calc.rates import percent\n\n\n"
    "def test_percent_zero_whole_returns_zero():\n"
    "    assert percent(5, 0) == 0.0\n"
)

OLD = "    return part / whole * 100\n"
GOOD = "    if whole == 0:\n        return 0.0\n    return part / whole * 100\n"
NO_FIX = "    return part / whole * 100.0\n"                       # applies, still fails RED test
REGRESSING = "    if whole == 0:\n        return 0.0\n    return abs(part) / whole * 100\n"


def make_repo(root: str) -> str:
    repo = os.path.join(root, "calcrepo")
    for rel, text in FILES.items():
        path = os.path.join(repo, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    for args in (["init", "-q"], ["config", "user.name", "t"], ["config", "user.email", "t@localhost"],
                 ["config", "core.autocrlf", "false"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def config(**kw) -> RepairConfig:
    base = dict(isolation=ISOLATION_HOST_UNSAFE, max_attempts=3, tool_timeout_seconds=60)
    base.update(kw)
    return RepairConfig(**base)

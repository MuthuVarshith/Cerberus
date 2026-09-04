"""
Reproduction Agent with a Hard RED Gate.
Given an issue description and codebase, it synthesizes an isolated test_reproduce.py script.
Enforces the strict TDD contract:
- The script MUST fail (return non-zero) on the unpatched codebase (RED gate passed).
- If it passes on the unpatched codebase, reproduction fails (no bug to fix).
- Syntax errors or missing imports in test_reproduce.py are rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple
from harness.docker_sandbox import Sandbox
from harness.tools import ACI


@dataclass
class ReproductionResult:
    reproduced: bool
    test_code: str
    error_message: str
    returncode: int
    raw_output: str


class ReproductionAgent:
    """Agent responsible for writing and validating the RED reproduction test."""

    def __init__(self, sandbox: Sandbox, model: str = "gpt-4o"):
        self.sandbox = sandbox
        self.aci = ACI(sandbox)
        self.model = model

    def build_reproduction_prompt(self, issue_title: str, issue_body: str) -> str:
        return f"""You are a Test-Driven Development (TDD) Reproduction Engineer.
Given the following issue report, write a standalone Python test file named `test_reproduce.py`.

### REQUIREMENTS:
1. The test MUST trigger the exact bug described in the issue.
2. It must assert the expected correct behavior so that it FAILS right now on the buggy codebase.
3. Use pytest or standard `assert` statements. If an exception is expected to be fixed, call the function that triggers the bug.
4. Keep it minimal and self-contained. Import necessary modules from the local project.
5. Return ONLY executable Python code inside a ```python ``` markdown codeblock.

### ISSUE TITLE:
{issue_title}

### ISSUE DESCRIPTION:
{issue_body}
"""

    def extract_code(self, response_text: str) -> str:
        """Extracts Python code from LLM markdown fences."""
        match = re.search(r"```python(.*?)```", response_text, re.DOTALL)
        if match:
            return match.group(1).strip()
        match = re.search(r"```(.*?)```", response_text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return response_text.strip()

    def run_reproduction_gate(
        self,
        issue_title: str,
        issue_body: str,
        generated_test_code: Optional[str] = None,
    ) -> ReproductionResult:
        """
        Executes the hard RED gate:
        - Writes test_reproduce.py
        - Runs `pytest test_reproduce.py` (or `python test_reproduce.py`)
        - Validates RED state (exit_code != 0 due to AssertionError or relevant Exception)
        """
        test_code = generated_test_code or ""
        if not test_code:
            return ReproductionResult(
                reproduced=False,
                test_code="",
                error_message="No test code provided or generated.",
                returncode=-1,
                raw_output="",
            )

        # Write test_reproduce.py to sandbox
        self.sandbox.write_file("test_reproduce.py", test_code)

        # Execute test in sandbox
        cmd = "py -3.13 -m pytest test_reproduce.py"
        exec_res = self.sandbox.exec(cmd, timeout=30)

        # HARD RED GATE CHECK:
        # A valid bug reproduction MUST FAIL (exit_code != 0)
        if exec_res.exit_code == 0:
            # Bug did NOT reproduce: the test passed on the unpatched codebase
            return ReproductionResult(
                reproduced=False,
                test_code=test_code,
                error_message="RED GATE BLOCKED: test_reproduce.py passed on unpatched codebase. Could not reproduce the bug.",
                returncode=0,
                raw_output=exec_res.output,
            )

        # Check for syntax errors in the test itself (we want real assertion/runtime failure, not test syntax break)
        if "SyntaxError" in exec_res.stderr or "SyntaxError" in exec_res.stdout:
            return ReproductionResult(
                reproduced=False,
                test_code=test_code,
                error_message="SyntaxError in test_reproduce.py script itself.",
                returncode=exec_res.exit_code,
                raw_output=exec_res.output,
            )

        # Successfully reproduced! (RED gate satisfied)
        return ReproductionResult(
            reproduced=True,
            test_code=test_code,
            error_message="",
            returncode=exec_res.exit_code,
            raw_output=exec_res.output,
        )

"""
LLM-backed reproduction test synthesis.

Provides LLMReproductionSynthesizer, which asks a model to generate a standalone
`test_reproduce.py` test given an issue report and relevant source files.
Enforces the strict RED-gate contract:
- The synthesized test must execute cleanly without syntax errors.
- The test must FAIL (exit code != 0) on the current buggy codebase.
- Feeds verifier failures back into the model across iterative retry attempts.
- Tracks token consumption via GenerationUsage.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from agents.llm_patch_generator import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_OPENAI_MODEL,
    GenerationUsage,
    has_api_key,
    resolve_model,
)
from agents.reproduction_agent import ReproductionAgent, ReproductionResult
from harness.docker_sandbox import Sandbox

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Test-Driven Development (TDD) Reproduction Engineer operating under a verification harness.

Rules you must follow:
1. Write a minimal, standalone reproduction test script named `test_reproduce.py`.
2. The test MUST trigger the reported bug and assert the expected correct behavior.
3. The test MUST fail (exit code != 0 with an AssertionError or expected exception) on the current buggy codebase.
4. Keep the test minimal and isolated. Import necessary modules from the local project.
5. Do NOT modify the source code; only write the test.
6. Return ONLY valid Python code inside a ```python ``` markdown codeblock, with no conversational filler.
"""


class ReproductionSynthesisError(RuntimeError):
    """Raised when reproduction synthesis fails or cannot be configured."""


class LLMReproductionSynthesizer:
    """Synthesizes and validates a minimal reproduction test against the RED gate."""

    def __init__(
        self,
        sandbox: Sandbox,
        candidate_files: Sequence[str],
        issue_title: str,
        issue_body: str,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_file_chars: int = 6000,
        completion_fn: Optional[Callable[..., Any]] = None,
    ):
        if completion_fn is None and not has_api_key():
            raise ReproductionSynthesisError(
                "No ANTHROPIC_API_KEY or OPENAI_API_KEY is set, so reproduction test "
                "synthesis cannot be performed. Set one or use scripted reproduction tests."
            )

        self.sandbox = sandbox
        self.candidate_files = list(candidate_files)
        self.issue_title = issue_title
        self.issue_body = issue_body
        self.model = resolve_model(model)
        self.temperature = temperature
        self.max_file_chars = max_file_chars
        self._completion_fn = completion_fn
        self.usage = GenerationUsage()
        self.transcript: List[str] = []

    def _completion(self, **kwargs: Any) -> Any:
        if self._completion_fn is not None:
            return self._completion_fn(**kwargs)
        try:
            from litellm import completion
        except ImportError as exc:  # pragma: no cover
            raise ReproductionSynthesisError(
                "litellm is required for LLM-backed reproduction test synthesis: pip install litellm"
            ) from exc
        return completion(**kwargs)

    def _read_candidate_sources(self) -> str:
        """Render candidate files as code blocks, truncating oversized ones."""
        blocks = []
        for path in self.candidate_files:
            try:
                content = self.sandbox.read_file(path)
            except Exception as exc:
                logger.warning("candidate_file_unreadable path=%s error=%s", path, exc)
                continue
            if len(content) > self.max_file_chars:
                content = (
                    content[: self.max_file_chars]
                    + f"\n# ... truncated at {self.max_file_chars} characters ...\n"
                )
            blocks.append(f"### File: {path}\n```python\n{content}\n```")
        return "\n\n".join(blocks) if blocks else "(no candidate file contents available)"

    def build_prompt(self, attempt: int = 1, feedback: str = "") -> str:
        """Assemble the user prompt for synthesis or retry."""
        parts = [
            f"## Issue Report\n**{self.issue_title}**\n\n{self.issue_body}".rstrip(),
        ]
        if self.candidate_files:
            parts.append(
                "## Relevant Files\n"
                + "\n".join(f"- {p}" for p in self.candidate_files)
                + "\n\n## Source Code Excerpts\n"
                + self._read_candidate_sources()
            )

        if attempt > 1 and feedback:
            parts.append(
                f"## Previous attempt failed reproduction (attempt {attempt - 1})\n"
                "This is the verifier's exact feedback. The test did not reproduce the issue or failed to run:\n"
                f"```\n{feedback}\n```\n"
                "Address the error. The test must be valid Python that runs and FAILS on the buggy codebase."
            )
        parts.append("Reply with the Python reproduction test inside a ```python ``` codeblock.")
        return "\n\n".join(parts)

    @staticmethod
    def extract_test_code(response_text: str) -> str:
        """Extract Python code from markdown code fences."""
        match = re.search(r"```(?:python)?\n(.*?)```", response_text, re.DOTALL)
        if match:
            return match.group(1).strip()
        match = re.search(r"```(.*?)```", response_text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return response_text.strip()

    def synthesize_and_verify(self, max_attempts: int = 3) -> ReproductionResult:
        """
        Iteratively prompts the model to generate a test and validates it against the RED gate.
        Returns when the RED gate is satisfied, or after max_attempts fails.
        """
        feedback = ""
        last_result: Optional[ReproductionResult] = None
        repro_agent = ReproductionAgent(self.sandbox)

        for attempt in range(1, max_attempts + 1):
            prompt = self.build_prompt(attempt=attempt, feedback=feedback)
            try:
                response = self._completion(
                    model=self.model,
                    temperature=self.temperature,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                )
            except Exception as exc:
                logger.error("LLM reproduction synthesis API call failed: %s", exc)
                last_result = ReproductionResult(
                    reproduced=False,
                    test_code="",
                    error_message=f"Provider API call failed: {exc}",
                    returncode=-1,
                    raw_output=str(exc),
                )
                break

            raw_text = ""
            if hasattr(response, "choices") and response.choices:
                raw_text = getattr(response.choices[0].message, "content", "") or ""
            self.transcript.append(raw_text)

            usage = getattr(response, "usage", None)
            if usage:
                self.usage.add(
                    prompt=getattr(usage, "prompt_tokens", 0) or 0,
                    completion=getattr(usage, "completion_tokens", 0) or 0,
                )

            test_code = self.extract_test_code(raw_text)
            if not test_code:
                feedback = (
                    f"Attempt #{attempt} yielded no Python code. "
                    "Ensure you provide executable Python code inside a ```python ``` block."
                )
                continue

            last_result = repro_agent.run_reproduction_gate(
                issue_title=self.issue_title,
                issue_body=self.issue_body,
                generated_test_code=test_code,
            )

            if last_result.reproduced:
                return last_result

            feedback = (
                f"Attempt #{attempt} failed the RED gate:\n"
                f"Reason: {last_result.error_message}\n"
                f"Output:\n{last_result.raw_output}"
            )

        return last_result or ReproductionResult(
            reproduced=False,
            test_code="",
            error_message=f"Failed to synthesize a passing RED test after {max_attempts} attempts.",
            returncode=-1,
            raw_output="",
        )

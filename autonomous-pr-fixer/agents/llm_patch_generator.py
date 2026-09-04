"""
LLM-backed patch generation.

`PatchAgent.run_patch_loop` takes a `patch_generator_fn(attempt, feedback) -> diff`.
This module supplies real implementations of that callable, which is the piece the
harness was missing: every gate was wired up, but nothing generated candidate
patches outside of test fixtures.

Two implementations are provided:

* `LLMPatchGenerator` — asks a model for a minimal unified diff, feeding the
  previous attempt's structured failure back in on each retry.
* `ScriptedPatchGenerator` — returns diffs from a fixed list, so the loop and the
  gates can be exercised deterministically and without an API key.

Both are plain callables, so `PatchAgent` needs no knowledge of either.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from harness.diff_utils import DiffUtils
from harness.docker_sandbox import Sandbox

logger = logging.getLogger(__name__)

#: litellm-style model identifier. Anthropic is the default; an OpenAI key alone
#: selects an OpenAI model instead (see `resolve_model`).
DEFAULT_ANTHROPIC_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "openai/gpt-4o"

SYSTEM_PROMPT = """You are a software repair agent operating under a verification harness.

Rules you must follow:
1. Reply with a unified diff and nothing else. No prose, no explanation.
2. The diff must apply cleanly with `git apply`. Use correct `--- a/<path>` and
   `+++ b/<path>` headers and accurate @@ hunk line numbers.
3. Change only the files you are explicitly given. Touching any other file causes
   the patch to be rejected by the blast-radius gate.
4. Make the smallest change that fixes the reported bug. Do not reformat, rename,
   add comments, or refactor surrounding code.
5. Do not modify tests. The reproduction test is the specification; a patch that
   edits it is rejected."""


class PatchGenerationError(RuntimeError):
    """Raised when a patch generator cannot be used at all (e.g. no API key)."""


@dataclass
class GenerationUsage:
    """Token accounting across a whole patch loop.

    Exists so `RunRecord.total_tokens` can be a measurement rather than a
    stipulated constant; `tokens_measured` should be set only when this is used.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def add(self, prompt: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += prompt + completion
        self.calls += 1


def resolve_model(explicit: Optional[str] = None) -> str:
    """Pick a model id: explicit argument, then CERBERUS_MODEL, then whichever key exists."""
    if explicit:
        return explicit
    from_env = os.environ.get("CERBERUS_MODEL", "").strip()
    if from_env:
        return from_env
    if os.environ.get("ANTHROPIC_API_KEY"):
        return DEFAULT_ANTHROPIC_MODEL
    if os.environ.get("OPENAI_API_KEY"):
        return DEFAULT_OPENAI_MODEL
    return DEFAULT_ANTHROPIC_MODEL


def has_api_key() -> bool:
    """Whether any provider credential is present, so a caller can pick a generator."""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))


class ScriptedPatchGenerator:
    """Yields pre-written diffs in order; the last one repeats once exhausted.

    Used by the evaluation harness and tests to drive the loop deterministically.
    Repeating the final diff is intentional: `run_patch_loop` detects a duplicate
    candidate and aborts, so an exhausted script ends the loop instead of hanging.
    """

    def __init__(self, diffs: Sequence[str]):
        if not diffs:
            raise PatchGenerationError("ScriptedPatchGenerator requires at least one diff.")
        self.diffs = list(diffs)
        self.calls = 0

    def __call__(self, attempt: int, feedback: str) -> str:
        self.calls += 1
        index = min(attempt, len(self.diffs)) - 1
        return self.diffs[max(index, 0)]


class LLMPatchGenerator:
    """Asks a model for a minimal unified diff, retrying with structured feedback."""

    def __init__(
        self,
        sandbox: Sandbox,
        candidate_files: List[str],
        issue_title: str,
        issue_body: str,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_file_chars: int = 6000,
        completion_fn: Optional[Callable[..., Any]] = None,
    ):
        """
        Args:
            candidate_files: the authorized repair boundary from localization. The
                model is shown these and only these; anything else it edits is
                rejected downstream by the blast-radius gate.
            completion_fn: injection point for tests. Defaults to litellm.completion,
                imported lazily because importing litellm is slow enough to matter
                at test-collection time.
        """
        if completion_fn is None and not has_api_key():
            raise PatchGenerationError(
                "No ANTHROPIC_API_KEY or OPENAI_API_KEY is set, so no patch can be "
                "generated. Set one, or use ScriptedPatchGenerator for offline runs."
            )
        self.sandbox = sandbox
        self.candidate_files = candidate_files
        self.issue_title = issue_title
        self.issue_body = issue_body
        self.model = resolve_model(model)
        self.temperature = temperature
        self.max_file_chars = max_file_chars
        self._completion_fn = completion_fn
        self.usage = GenerationUsage()
        #: Raw model replies, in order, for the run artifact.
        self.transcript: List[str] = []

    def _completion(self, **kwargs: Any) -> Any:
        if self._completion_fn is not None:
            return self._completion_fn(**kwargs)
        try:
            from litellm import completion  # imported here: slow, and optional at rest
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise PatchGenerationError(
                "litellm is required for LLM-backed patch generation: pip install litellm"
            ) from exc
        return completion(**kwargs)

    def _read_candidate_sources(self) -> str:
        """Render the authorized files as fenced blocks, truncating very large ones."""
        blocks = []
        for path in self.candidate_files:
            try:
                content = self.sandbox.read_file(path)
            except Exception as exc:
                logger.warning("candidate_file_unreadable path=%s error=%s", path, exc)
                continue
            if len(content) > self.max_file_chars:
                # Truncation is announced so the model does not invent hunk line
                # numbers for a region it was never shown.
                content = (
                    content[: self.max_file_chars]
                    + f"\n# ... truncated at {self.max_file_chars} characters ...\n"
                )
            blocks.append(f"### File: {path}\n```python\n{content}\n```")
        return "\n\n".join(blocks) if blocks else "(no candidate file contents available)"

    def build_prompt(self, attempt: int, feedback: str) -> str:
        """Assemble the user message for one attempt."""
        parts = [
            f"## Issue\n**{self.issue_title}**\n\n{self.issue_body}".rstrip(),
            "## Files you may modify\n"
            + "\n".join(f"- {p}" for p in self.candidate_files)
            + "\n\nModifying anything else will be rejected.",
            "## Current source\n" + self._read_candidate_sources(),
        ]
        if attempt > 1:
            parts.append(
                f"## Previous attempt failed (attempt {attempt - 1})\n"
                "This is verifier output, not a suggestion. Address the actual failure:\n"
                f"```\n{feedback}\n```"
            )
        parts.append("Reply with the unified diff only.")
        return "\n\n".join(parts)

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Pull the assistant message out of an OpenAI-shaped response."""
        try:
            choice = response.choices[0]
        except (AttributeError, IndexError, TypeError):
            choice = None
        if choice is None:
            return ""
        message = getattr(choice, "message", None)
        if message is None and isinstance(choice, dict):
            message = choice.get("message")
        if message is None:
            return ""
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        return content or ""

    def _record_usage(self, response: Any) -> None:
        """Accumulate token counts when the provider reported them."""
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
        if usage is None:
            return

        def _field(name: str) -> int:
            value = getattr(usage, name, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(name)
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        self.usage.add(_field("prompt_tokens"), _field("completion_tokens"))

    def __call__(self, attempt: int, feedback: str) -> str:
        """Generate one candidate diff. Signature matches `patch_generator_fn`."""
        prompt = self.build_prompt(attempt, feedback)
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
            # A provider error is a failed attempt, not a crashed pipeline: returning
            # an empty diff lets the loop record the failure and move on.
            logger.error("patch_generation_failed attempt=%s error=%s", attempt, exc)
            self.transcript.append(f"<error: {exc}>")
            return ""

        raw = self._extract_text(response)
        self.transcript.append(raw)
        self._record_usage(response)
        return DiffUtils.extract_diff_from_markdown(raw)

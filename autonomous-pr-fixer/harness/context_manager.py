"""
Context Manager & Token Budgeting for the Harness.
Follows mini-swe-agent context pruning principles: truncates large file views,
limits history growth, and formats error traces concisely for the next agent turn.
"""
from __future__ import annotations

from typing import List, Dict, Any, Optional


class ContextManager:
    """Manages conversational history, token budgets, and error feedback."""

    def __init__(self, max_tokens: int = 32000, model: str = "gpt-4o"):
        self.max_tokens = max_tokens
        self.model = model
        self.messages: List[Dict[str, str]] = []

    def add_message(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def get_messages(self) -> List[Dict[str, str]]:
        return self.messages

    def format_error_feedback(self, attempt: int, stderr: str, stdout: str) -> str:
        """
        Extracts and formats pytest / python traceback concisely to prevent
        context bloat while giving the patch agent exact line numbers and assert failures.
        """
        raw = stderr if stderr.strip() else stdout
        lines = raw.strip().splitlines()
        
        # Keep last 40 lines which usually contains the exact AssertionError / Traceback
        relevant = lines[-40:] if len(lines) > 40 else lines
        condensed_error = "\n".join(relevant)

        return (
            f"### Patch Attempt #{attempt} FAILED\n"
            f"Execution output / traceback:\n```\n{condensed_error}\n```\n"
            f"Analyze the exact AssertionError or exception above. "
            f"Produce a corrected unified diff that specifically fixes this issue."
        )

    def prune_context(self, max_messages: int = 15) -> None:
        """Keeps the system prompt and the most recent N conversational turns."""
        if len(self.messages) <= max_messages:
            return
        # Keep system message if first
        first_is_sys = len(self.messages) > 0 and self.messages[0]["role"] == "system"
        if first_is_sys:
            self.messages = [self.messages[0]] + self.messages[-(max_messages - 1):]
        else:
            self.messages = self.messages[-max_messages:]

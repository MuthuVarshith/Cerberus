"""
Agent-Computer Interface (ACI) Tools.
Provides lightweight, token-efficient inspection, editing, and execution tools
following the mini-swe-agent / ACI principles (line-range viewing, search, edit).
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple
from harness.docker_sandbox import ExecResult, Sandbox


class ACI:
    """Agent-Computer Interface wrapper over the Sandbox."""

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox

    def view_file(
        self, rel_path: str, start_line: int = 1, end_line: Optional[int] = None
    ) -> str:
        """
        Views lines of a file with line numbers.
        Truncates large views to prevent LLM context exhaustion.
        """
        try:
            content = self.sandbox.read_file(rel_path)
        except FileNotFoundError:
            return f"Error: File '{rel_path}' not found."

        lines = content.splitlines()
        total_lines = len(lines)
        if total_lines == 0:
            return f"[{rel_path} is empty]"

        start = max(1, start_line)
        end = min(total_lines, end_line if end_line is not None else total_lines)

        if start > total_lines:
            return f"Error: start_line {start} is beyond file length ({total_lines})."

        output = [f"--- {rel_path} (lines {start}-{end} of {total_lines}) ---"]
        for idx in range(start - 1, end):
            output.append(f"{idx + 1:4d} | {lines[idx]}")

        return "\n".join(output)

    def edit_lines(
        self,
        rel_path: str,
        start_line: int,
        end_line: int,
        replacement_text: str,
    ) -> str:
        """
        Replaces lines [start_line, end_line] inclusive with replacement_text.
        Line numbers are 1-indexed.
        """
        try:
            content = self.sandbox.read_file(rel_path)
        except FileNotFoundError:
            return f"Error: File '{rel_path}' not found."

        lines = content.splitlines()
        total_lines = len(lines)

        if start_line < 1 or start_line > total_lines + 1:
            return f"Error: start_line {start_line} is invalid for file with {total_lines} lines."
        if end_line < start_line - 1 or end_line > total_lines:
            return f"Error: end_line {end_line} is invalid (start_line={start_line}, total={total_lines})."

        new_replacement_lines = (
            replacement_text.splitlines() if replacement_text else []
        )
        prefix = lines[: start_line - 1]
        suffix = lines[end_line:]
        modified_lines = prefix + new_replacement_lines + suffix

        new_content = "\n".join(modified_lines)
        if content.endswith("\n") or not content:
            new_content += "\n"

        self.sandbox.write_file(rel_path, new_content)
        return (
            f"Successfully updated {rel_path} (lines {start_line}-{end_line} replaced with "
            f"{len(new_replacement_lines)} lines, total lines now {len(modified_lines)})."
        )

    def run_cmd(self, command: str, timeout: Optional[int] = None) -> ExecResult:
        """Runs a bash/shell command inside the sandbox."""
        return self.sandbox.exec(command, timeout=timeout)

    def grep(
        self,
        query: str,
        path: str = ".",
        case_sensitive: bool = False,
        max_results: int = 50,
    ) -> str:
        """
        Searches files in the sandbox workspace for occurrences of query.
        Uses ripgrep if installed, else fallback to standard Python walk.
        """
        # Try ripgrep or git grep in sandbox
        cmd = f"git grep -n {' ' if case_sensitive else '-i '} -- \"{query}\" {path}"
        res = self.sandbox.exec(cmd)
        if res.exit_code == 0 and res.stdout.strip():
            matches = res.stdout.strip().splitlines()
            if len(matches) > max_results:
                return "\n".join(matches[:max_results]) + f"\n... [{len(matches) - max_results} more results omitted]"
            return "\n".join(matches)

        # Fallback pure-Python directory scan inside sandbox workspace
        results = []
        base = os.path.join(self.sandbox.workspace_dir, path)
        pattern = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)

        for root, dirs, files in os.walk(base):
            # Ignore hidden or build dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules", ".git")]
            for file in files:
                if file.endswith((".pyc", ".pyo", ".so", ".bin", ".lock")):
                    continue
                file_full = os.path.join(root, file)
                rel_f = os.path.relpath(file_full, self.sandbox.workspace_dir)
                try:
                    with open(file_full, "r", encoding="utf-8", errors="ignore") as f:
                        for idx, line in enumerate(f, 1):
                            if pattern.search(line):
                                results.append(f"{rel_f}:{idx}:{line.rstrip()}")
                                if len(results) >= max_results:
                                    break
                except Exception:
                    continue
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

        if not results:
            return f"No matches found for '{query}' in '{path}'."
        return "\n".join(results)

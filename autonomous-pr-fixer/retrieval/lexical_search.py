"""
Lexical search layer: literal keyword matching across repository files.

Implemented in pure Python on purpose. It reads files from the workspace but
never executes git or any other program there.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class SearchMatch:
    file_path: str
    line_number: int
    line_content: str
    score: float = 1.0


class LexicalSearch:
    """Performs lexical search across workspace files."""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = os.path.abspath(workspace_dir)

    def search(
        self,
        query: str,
        case_sensitive: bool = False,
        file_extension: str = ".py",
        max_matches: int = 50,
    ) -> List[SearchMatch]:
        """Searches repository files for exact or regex query matches."""
        results: List[SearchMatch] = []
        if not query or not query.strip():
            return results

        # Pure-Python scan. No git: after repository code has run in the sandbox,
        # the workspace's .git directory is untrusted and must not drive host git.
        flags_re = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(re.escape(query), flags_re)

        for root, dirs, files in os.walk(self.workspace_dir):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d not in ("__pycache__", "venv", "env", "node_modules", ".git")
            ]
            for file in files:
                if not file.endswith(file_extension):
                    continue
                full_path = os.path.join(root, file)
                if os.path.islink(full_path):
                    continue  # never follow a workspace symlink from the host
                rel_path = os.path.relpath(full_path, self.workspace_dir).replace("\\", "/")

                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        for idx, line in enumerate(f, 1):
                            if pattern.search(line):
                                results.append(
                                    SearchMatch(
                                        file_path=rel_path,
                                        line_number=idx,
                                        line_content=line.strip(),
                                    )
                                )
                                if len(results) >= max_matches:
                                    return results
                except Exception:
                    continue

        return results

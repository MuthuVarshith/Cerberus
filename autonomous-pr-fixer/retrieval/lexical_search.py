"""
Lexical Search Layer.
Provides fast keyword, regex, and symbol matching across repository files
using ripgrep (where available) with robust pure-Python regex fallback.
"""
from __future__ import annotations

import os
import re
import subprocess
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

        # Try git grep first
        flags = "-n" if case_sensitive else "-n -i"
        cmd = f"git grep {flags} -- \"{query}\""
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                for line in proc.stdout.strip().splitlines():
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        fpath = parts[0].replace("\\", "/")
                        if not fpath.endswith(file_extension):
                            continue
                        try:
                            lineno = int(parts[1])
                        except ValueError:
                            lineno = 1
                        results.append(
                            SearchMatch(
                                file_path=fpath,
                                line_number=lineno,
                                line_content=parts[2].strip(),
                            )
                        )
                        if len(results) >= max_matches:
                            return results
                if results:
                    return results
        except Exception:
            pass

        # Python fallback directory scan
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

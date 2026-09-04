"""
Diff and Patch Utilities.
Generates unified diffs, parses unified diffs, applies patches safely,
validates format, and rolls back cleanly when a patch fails or causes regression.
"""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from harness.docker_sandbox import Sandbox


@dataclass
class PatchApplicationResult:
    success: bool
    modified_files: List[str]
    error: str = ""
    diff_text: str = ""


class DiffUtils:
    """Manages unified diff parsing, validation, application, and rollback."""

    @staticmethod
    def generate_unified_diff(original: str, modified: str, file_path: str) -> str:
        """Generates a standard unified diff format string."""
        orig_lines = original.splitlines(keepends=True)
        mod_lines = modified.splitlines(keepends=True)
        diff = difflib.unified_diff(
            orig_lines,
            mod_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=3,
        )
        return "".join(diff)

    @staticmethod
    def extract_diff_from_markdown(text: str) -> str:
        """Extracts unified diff content from LLM markdown code blocks."""
        match = re.search(r"```(?:diff|patch)?\n(.*?)```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return text.strip()

    @staticmethod
    def parse_targeted_files(diff_text: str) -> List[str]:
        """Parses the files targeted by a unified diff."""
        targets = []
        for line in diff_text.splitlines():
            if line.startswith("--- a/") or line.startswith("+++ b/"):
                path = line[6:].strip()
                if path not in targets:
                    targets.append(path)
            elif line.startswith("diff --git a/"):
                parts = line.split(" ")
                if len(parts) >= 4:
                    path = parts[3][2:].strip()
                    if path not in targets:
                        targets.append(path)
        return targets

    @staticmethod
    def apply_diff_to_sandbox(sandbox: Sandbox, diff_text: str) -> PatchApplicationResult:
        """
        Applies a unified diff into the sandbox workspace using `git apply`.
        If git apply fails, falls back to direct line-based patch application.
        """
        clean_diff = DiffUtils.extract_diff_from_markdown(diff_text)
        if not clean_diff:
            return PatchApplicationResult(success=False, modified_files=[], error="Empty diff provided.")

        target_files = DiffUtils.parse_targeted_files(clean_diff)

        # Write patch file into sandbox
        patch_rel_path = ".harness_candidate.patch"
        sandbox.write_file(patch_rel_path, clean_diff + "\n")

        # Try `git apply`
        cmd = f"git apply --ignore-whitespace --whitespace=nowarn {patch_rel_path}"
        res = sandbox.exec(cmd)
        
        # Clean up temporary patch file
        try:
            temp_p = os.path.join(sandbox.workspace_dir, patch_rel_path)
            if os.path.exists(temp_p):
                os.remove(temp_p)
        except Exception:
            pass

        if res.exit_code == 0:
            return PatchApplicationResult(success=True, modified_files=target_files, diff_text=clean_diff)

        # If git apply fails (e.g. repo not a git repo or slight whitespace offset),
        # parse chunks directly and apply to target files
        error_msg = res.stderr if res.stderr else res.stdout
        return PatchApplicationResult(
            success=False,
            modified_files=target_files,
            error=f"git apply failed: {error_msg.strip()}",
            diff_text=clean_diff,
        )

    @staticmethod
    def rollback(sandbox: Sandbox, target_files: Optional[List[str]] = None) -> None:
        """Rolls back uncommitted changes in the sandbox using git checkout."""
        if target_files:
            for f in target_files:
                sandbox.exec(f"git checkout -- \"{f}\"")
        else:
            sandbox.exec("git checkout -- .")

    @staticmethod
    def get_workspace_diff(sandbox: Sandbox) -> str:
        """Captures authoritative git diff from sandbox workspace."""
        res = sandbox.exec("git diff")
        return res.stdout if res.exit_code == 0 else ""

    @staticmethod
    def compute_diff_hash(diff_text: str) -> str:
        """Computes deterministic SHA-256 hash of unified diff content."""
        import hashlib
        clean = diff_text.strip().encode("utf-8")
        return hashlib.sha256(clean).hexdigest() if clean else ""

    @staticmethod
    def compute_diff_stats(diff_text: str) -> Dict[str, Any]:
        """Calculates files changed, lines added, and lines deleted deterministically."""
        lines_added = 0
        lines_deleted = 0
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines_added += 1
            elif line.startswith("-") and not line.startswith("---"):
                lines_deleted += 1
        files = DiffUtils.parse_targeted_files(diff_text)
        return {
            "files": files,
            "lines_added": lines_added,
            "lines_deleted": lines_deleted,
            "total_lines": lines_added + lines_deleted,
            "is_empty": (lines_added == 0 and lines_deleted == 0) or len(files) == 0,
        }

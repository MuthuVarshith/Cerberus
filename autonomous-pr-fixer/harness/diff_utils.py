"""
Diff and patch utilities.

All git operations on a workspace run through `Sandbox.exec`, never through host
git: once repository code has run, the workspace's `.git` directory is
untrusted (a rewritten config could make host git execute commands).

Every comparison is against the recorded base commit SHA, not `HEAD`, so code
that moves `HEAD` or commits inside the sandbox cannot hide a change from the
gates. Untracked new files are included via intent-to-add, and filenames are
passed through a pathspec file rather than interpolated into shell commands.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from harness.docker_sandbox import HARNESS_DIR, Sandbox
from harness.repo_config import CONFIG_FILENAME


@dataclass
class PatchApplicationResult:
    success: bool
    modified_files: List[str]
    error: str = ""
    diff_text: str = ""


@dataclass
class WorkspaceChanges:
    """What the working tree changed relative to the base commit."""
    diff_text: str
    files: List[str] = field(default_factory=list)
    new_files: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)
    lines_added: int = 0
    lines_deleted: int = 0
    per_file_numstat: Dict[str, Tuple[int, int]] = field(default_factory=dict)

    @property
    def total_lines(self) -> int:
        return self.lines_added + self.lines_deleted

    @property
    def is_empty(self) -> bool:
        return not self.files


_UNSAFE_PATH_RE = re.compile(r"(^|/)\.\.(/|$)")


def _diff_base(sandbox: Sandbox) -> str:
    return getattr(sandbox, "base_commit", None) or "HEAD"


def _git_diff_flags() -> str:
    # Repository config must not change what a diff looks like.
    return "--no-color --no-ext-diff --no-renames --no-textconv"


class DiffUtils:
    """Unified diff parsing, application, rollback and workspace change measurement."""

    @staticmethod
    def generate_unified_diff(original: str, modified: str, file_path: str) -> str:
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=3,
        )
        return "".join(diff)

    @staticmethod
    def extract_diff_from_markdown(text: str) -> str:
        """Extracts unified diff content from LLM markdown code blocks.

        Only surrounding line breaks are removed. A hunk's last context line is a
        single space when the source line is empty; stripping all whitespace
        deletes it and git rejects the hunk as corrupt.
        """
        match = re.search(r"```(?:diff|patch)?\n(.*?)```", text, re.DOTALL)
        return (match.group(1) if match else text).strip("\r\n")

    @staticmethod
    def parse_targeted_files(diff_text: str) -> List[str]:
        """Parses the files targeted by a unified diff."""
        targets: List[str] = []
        for line in diff_text.splitlines():
            path = None
            if line.startswith("--- a/") or line.startswith("+++ b/"):
                path = line[6:].strip()
            elif line.startswith("diff --git a/"):
                parts = line.split(" ")
                if len(parts) >= 4:
                    path = parts[3][2:].strip()
            if path and path not in targets:
                targets.append(path)
        return targets

    @staticmethod
    def forbidden_targets(diff_text: str) -> List[str]:
        """Paths a candidate patch may never touch: harness files, git internals, the
        repository's verification policy (.cerberus.yml), and paths escaping the tree."""
        bad = []
        for line in diff_text.splitlines():
            if not (line.startswith("--- ") or line.startswith("+++ ") or line.startswith("diff --git ")):
                continue
            for token in line.split()[1:]:
                path = token[2:] if token.startswith(("a/", "b/")) else token
                if path == "/dev/null":
                    continue
                norm = path.replace("\\", "/")
                if (
                    norm.startswith("/")
                    or _UNSAFE_PATH_RE.search(norm)
                    or norm == HARNESS_DIR or norm.startswith(f"{HARNESS_DIR}/")
                    or norm == ".git" or norm.startswith(".git/")
                    or norm == CONFIG_FILENAME
                ):
                    if path not in bad:
                        bad.append(path)
        return bad

    @staticmethod
    def apply_diff_to_sandbox(sandbox: Sandbox, diff_text: str) -> PatchApplicationResult:
        """Applies a unified diff to the workspace with `git apply` inside the sandbox."""
        clean_diff = DiffUtils.extract_diff_from_markdown(diff_text)
        if not clean_diff:
            return PatchApplicationResult(success=False, modified_files=[], error="Empty diff provided.")
        forbidden = DiffUtils.forbidden_targets(clean_diff)
        if forbidden:
            return PatchApplicationResult(
                success=False,
                modified_files=[],
                error=f"Patch targets forbidden paths: {', '.join(forbidden)}",
                diff_text=clean_diff,
            )

        target_files = DiffUtils.parse_targeted_files(clean_diff)
        sandbox.ensure_harness_dir()
        patch_rel_path = f"{HARNESS_DIR}/candidate.patch"
        sandbox.write_file(patch_rel_path, clean_diff + "\n")
        res = sandbox.exec(f"git apply --ignore-whitespace --whitespace=nowarn {patch_rel_path}")
        if res.exit_code == 0:
            return PatchApplicationResult(success=True, modified_files=target_files, diff_text=clean_diff)
        error_msg = res.stderr if res.stderr else res.stdout
        return PatchApplicationResult(
            success=False,
            modified_files=target_files,
            error=f"git apply failed: {error_msg.strip()}",
            diff_text=clean_diff,
        )

    @staticmethod
    def rollback(sandbox: Sandbox, target_files: Optional[List[str]] = None) -> None:
        """Return the disposable workspace to the base commit.

        Tracked changes are reset and untracked files removed; ignored files,
        including `.cerberus/` and installed build artefacts, are kept. This runs
        only on Cerberus's own workspace clone, never on a user's checkout.
        """
        base = _diff_base(sandbox)
        sandbox.exec(f"git reset -q --hard {base}")
        sandbox.exec(f"git clean -fdq -e /{HARNESS_DIR}/")

    @staticmethod
    def _untracked_files(sandbox: Sandbox) -> List[str]:
        res = sandbox.exec(f"git ls-files --others --exclude-standard -x /{HARNESS_DIR}/ -z")
        return [p for p in res.stdout.split("\0") if p]

    @staticmethod
    def workspace_changes(sandbox: Sandbox) -> WorkspaceChanges:
        """Measure every change against the base commit, including new files."""
        base = _diff_base(sandbox)
        untracked = DiffUtils._untracked_files(sandbox)
        pathspec = f"{HARNESS_DIR}/untracked.pathspec"
        if untracked:
            sandbox.ensure_harness_dir()
            sandbox.write_file(pathspec, "\0".join(untracked) + "\0")
            sandbox.exec(f"git add --intent-to-add --pathspec-from-file={pathspec} --pathspec-file-nul")
        try:
            flags = _git_diff_flags()
            diff = sandbox.exec(f"git diff {flags} {base}")
            status = sandbox.exec(f"git diff {flags} --name-status -z {base}")
            numstat = sandbox.exec(f"git diff {flags} --numstat -z {base}")
        finally:
            if untracked:
                sandbox.exec(f"git reset -q --pathspec-from-file={pathspec} --pathspec-file-nul")

        changes = WorkspaceChanges(diff_text=diff.stdout if diff.exit_code == 0 else "")
        tokens = [t for t in status.stdout.split("\0")]
        i = 0
        while i + 1 < len(tokens):
            code, path = tokens[i], tokens[i + 1]
            i += 2
            if not code:
                continue
            path = path.replace("\\", "/")
            changes.files.append(path)
            if path in untracked or code.startswith("A"):
                changes.new_files.append(path)
            elif code.startswith("D"):
                changes.deleted_files.append(path)
        for entry in numstat.stdout.split("\0"):
            parts = entry.split("\t")
            if len(parts) < 3:
                continue
            path = parts[2].replace("\\", "/")
            try:
                added, deleted = int(parts[0]), int(parts[1])
            except ValueError:
                added, deleted = 0, 0  # binary file
            changes.per_file_numstat[path] = (added, deleted)
            changes.lines_added += added
            changes.lines_deleted += deleted
        return changes

    @staticmethod
    def get_workspace_diff(sandbox: Sandbox) -> str:
        return DiffUtils.workspace_changes(sandbox).diff_text

    @staticmethod
    def compute_diff_hash(diff_text: str) -> str:
        """Computes deterministic SHA-256 hash of unified diff content."""
        clean = diff_text.strip().encode("utf-8")
        return hashlib.sha256(clean).hexdigest() if clean else ""

    @staticmethod
    def compute_diff_stats(diff_text: str) -> Dict[str, object]:
        """Calculates files changed, lines added, and lines deleted from diff text."""
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

    @staticmethod
    def changed_line_ranges(diff_text: str) -> Dict[str, Dict[str, List[Tuple[int, int]]]]:
        """Per file, the old-side and new-side line ranges touched by each hunk."""
        ranges: Dict[str, Dict[str, List[Tuple[int, int]]]] = {}
        old_path: Optional[str] = None
        new_path: Optional[str] = None
        hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
        for line in diff_text.splitlines():
            if line.startswith("--- "):
                old_path = None if line[4:].strip() == "/dev/null" else line[4:].strip()[2:]
            elif line.startswith("+++ "):
                new_path = None if line[4:].strip() == "/dev/null" else line[4:].strip()[2:]
            else:
                m = hunk_re.match(line)
                if not m:
                    continue
                old_start, old_len = int(m.group(1)), int(m.group(2) or 1)
                new_start, new_len = int(m.group(3)), int(m.group(4) or 1)
                key = new_path or old_path
                if key is None:
                    continue
                entry = ranges.setdefault(key, {"old": [], "new": []})
                if old_path and old_len:
                    entry["old"].append((old_start, old_start + old_len - 1))
                if new_path and new_len:
                    entry["new"].append((new_start, new_start + new_len - 1))
        return ranges

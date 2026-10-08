"""Read a repository's Python sources at a commit as text, without a checkout and without running anything.

`materialize` writes the commit's `.py` files to a directory with `git archive` (for methods that
expect files on disk, such as the Phase 1 localizer). `read_sources` returns them in memory via
`git cat-file --batch`. Both treat the repository as untrusted data: no hooks, filters, imports or
setup code run.
"""
from __future__ import annotations

import io
import os
import subprocess
import tarfile

from harness.scope_gate import is_test_path

MAX_FILE_BYTES = 1024 * 1024  # skip generated or vendored giants

# Never let repository-provided git configuration (attributes, filters) apply during export.
_GIT_SAFE = ["git", "-c", "core.autocrlf=false", "-c", "core.attributesFile=/dev/null"]


def _git(mirror: str, *args: str, input_bytes: bytes | None = None, timeout: int = 600) -> bytes:
    res = subprocess.run([*_GIT_SAFE, "--git-dir", mirror, *args], input=input_bytes,
                         capture_output=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {res.stderr.decode('utf-8', 'replace')[:300]}")
    return res.stdout


def python_files(mirror: str, commit: str, include_tests: bool = False) -> list[str]:
    out = _git(mirror, "ls-tree", "-r", "-z", "--name-only", commit).decode("utf-8", "replace")
    paths = [p for p in out.split("\0") if p.endswith(".py")]
    return [p for p in paths if include_tests or not is_test_path(p)]


def read_sources(mirror: str, commit: str, paths: list[str]) -> dict[str, str]:
    """Return {path: text} for the given paths at `commit` (binary or oversized files skipped)."""
    if not paths:
        return {}
    request = "".join(f"{commit}:{p}\n" for p in paths).encode("utf-8")
    raw = _git(mirror, "cat-file", "--batch", input_bytes=request)
    sources: dict[str, str] = {}
    pos = 0
    for path in paths:
        header_end = raw.index(b"\n", pos)
        header = raw[pos:header_end].split()
        pos = header_end + 1
        if len(header) < 3 or header[1] != b"blob":
            continue  # "missing" or not a blob
        size = int(header[2])
        data = raw[pos:pos + size]
        pos += size + 1  # trailing newline after each object
        if size > MAX_FILE_BYTES or b"\0" in data[:8192]:
            continue
        sources[path] = data.decode("utf-8", errors="replace")
    return sources


def materialize(mirror: str, commit: str, dest: str, include_tests: bool = False) -> list[str]:
    """Write the commit's .py files (non-test by default) under `dest`. Returns the written paths."""
    wanted = set(python_files(mirror, commit, include_tests=include_tests))
    tar_bytes = _git(mirror, "archive", "--format=tar", commit)
    written: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        for member in tar.getmembers():
            if not member.isfile() or member.name not in wanted or member.size > MAX_FILE_BYTES:
                continue  # symlinks, devices and everything else are skipped, never followed
            norm = member.name.replace("\\", "/")
            if norm.startswith("/") or ".." in norm.split("/"):
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            target = os.path.join(dest, *norm.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as out:
                out.write(fh.read())
            written.append(norm)
    return written

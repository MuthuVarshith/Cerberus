"""
Issue commands and the reproduction-test input contract (pure functions, no I/O).

Commands (first line of an issue comment, case-insensitive):
  /cerberus fix                      start a repair run for this issue
  /cerberus approve [RUN_ID]         open a draft PR for an ADMITTED run (latest run if omitted)
  /cerberus reject  [RUN_ID] [why]   record a human rejection of an ADMITTED run

Input contract: the issue body must contain a fenced ```python block with at least
one `def test_...` function. Cerberus holds every candidate patch to that test.

Anything taken from a repository or an issue and echoed back to GitHub goes through
`fence` (code block) or `inline_code`, so it renders as text, not as markdown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

FIX_LABEL = "cerberus-fix"
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
ISSUE_COMMANDS = frozenset({"fix", "approve", "reject"})
MAX_REPRO_TEST_CHARS = 50_000

_COMMAND_RE = re.compile(r"^/cerberus\s+([a-z]+)\b[ \t]*(.*)$", re.IGNORECASE)
_PY_BLOCK_RE = re.compile(r"^```[ \t]*(?:python3?|py)[ \t]*\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE | re.IGNORECASE)
_TEST_FUNC_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+test_\w*[ \t]*\(", re.MULTILINE)
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

REPRO_FORMAT_HELP = """Cerberus needs a **reproduction test** in the issue body before it can attempt a fix. \
Every candidate patch is held to this test: it must fail on the current default branch and pass with the patch, \
and the rest of the test suite must keep passing.

Edit the issue description and add one fenced Python block containing at least one `def test_...` function, \
for example:

````markdown
```python
from mypackage.intervals import merge

def test_merge_keeps_contained_interval():
    assert merge([(1, 10), (2, 3)]) == [(1, 10)]
```
````

Rules:
- The block must start with ```` ```python ```` (or ```` ```py ````) on its own line and end with ```` ``` ````.
- It must contain at least one function whose name starts with `test_`; it runs under pytest from the repository root.
- Import the code under test the way the repository's own tests do.
- If there are several Python blocks, Cerberus uses the first one that contains a `def test_` function.

Then comment `/cerberus fix` (or re-apply the `cerberus-fix` label)."""


@dataclass
class IssueCommand:
    name: str                 # fix | approve | reject | verify | repair | <other>
    run_id: Optional[str] = None
    note: str = ""


def parse_command(body: str) -> Optional[IssueCommand]:
    """Parse the first line of a comment as a /cerberus command, or return None."""
    lines = (body or "").replace("\r\n", "\n").strip().split("\n")
    match = _COMMAND_RE.match(lines[0].strip()) if lines and lines[0] else None
    if not match:
        return None
    name = match.group(1).lower()
    rest = match.group(2).strip()
    run_id: Optional[str] = None
    if rest:
        first, _, remainder = rest.partition(" ")
        if _RUN_ID_RE.match(first) and (first.startswith("app_") or first.startswith("api_")):
            run_id, rest = first, remainder.strip()
    note = " ".join([rest, *(ln.strip() for ln in lines[1:])]).strip()[:500]
    return IssueCommand(name=name, run_id=run_id, note=note)


def extract_reproduction_test(body: str) -> Optional[str]:
    """The first ```python block containing a `def test_` function, or None."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    for match in _PY_BLOCK_RE.finditer(text):
        code = match.group(1)
        if _TEST_FUNC_RE.search(code):
            return code if code.endswith("\n") else code + "\n"
    return None


def reproduction_test_problem(body: str) -> Optional[str]:
    """Why the issue body does not satisfy the input contract, or None if it does."""
    code = extract_reproduction_test(body)
    if code is None:
        return "The issue body has no fenced ```python block with a `def test_` function."
    if len(code) > MAX_REPRO_TEST_CHARS:
        return f"The reproduction test is longer than {MAX_REPRO_TEST_CHARS} characters."
    return None


def fence(text: str, lang: str = "", limit: int = 0) -> str:
    """Render untrusted text as a fenced code block that its own content cannot close."""
    text = (text or "").replace("\r\n", "\n")
    if limit and len(text) > limit:
        text = text[:limit] + "\n... (truncated by Cerberus)"
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    marker = "`" * max(3, longest + 1)
    return f"{marker}{lang}\n{text.rstrip()}\n{marker}"


def inline_code(text: str, limit: int = 200) -> str:
    """Single-line inline code; backticks and newlines are removed so the span cannot be escaped."""
    cleaned = re.sub(r"[`\r\n]+", " ", str(text or "")).strip()[:limit]
    return f"`{cleaned}`" if cleaned else "`-`"


def bullet_list(items: List[str], limit: int = 30) -> str:
    """Untrusted items (paths, test ids) as one fenced block, one per line."""
    if not items:
        return "_none_"
    shown = list(items[:limit])
    if len(items) > limit:
        shown.append(f"... and {len(items) - limit} more")
    return fence("\n".join(str(i) for i in shown))

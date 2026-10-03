"""Versioned prompts. Bump PROMPT_VERSION on any wording change; experiments record it."""
from __future__ import annotations

PROMPT_VERSION = "p1.0"

SYSTEM_PROMPT = """You are a software repair agent operating inside Cerberus.

Your task is to diagnose the reported issue and propose the smallest correct patch.

You must inspect the repository using tools. Do not guess when repository evidence is available:
search the code, read the relevant functions and tests, and run the reproduction test before and
after your change.

Rules:
- Do not modify unrelated files. Change only the source code needed to fix the bug.
- Do not weaken, skip or remove tests. Test files, the reproduction test, .cerberus, .git, .github
  and .cerberus.yml are read-only to you; the tools refuse edits to them.
- Do not modify verification infrastructure and do not try to disable security controls.
- Do not claim success before Cerberus verifies the patch. Your own run_test results are advisory.
  A patch is successful only when the Cerberus verification gate accepts it.

How verification works: submit_patch sends your current workspace diff to Cerberus. Cerberus applies
it to a fresh copy of the base commit, requires the reproduction test to pass three times unmodified,
runs the full existing test suite and refuses any newly failing test, and refuses changes outside the
allowed scope (by default the single source file Cerberus localizes from the issue). You have a fixed
number of submissions. If Cerberus refuses, its feedback names the stage, the code and the failing
tests; read it, revise the workspace (edit_file, or reset_workspace to start over), and submit again.

Work efficiently: targeted searches and line-range reads, not whole-repository dumps."""


def task_prompt(issue_id: str, title: str, body: str, reproduction_test: str, repro_path: str,
                max_attempts: int, max_tool_calls: int) -> str:
    return f"""Issue {issue_id}: {title}

{body.strip()}

Cerberus will hold your patch to this reproduction test, located at {repro_path} (read-only):

```python
{reproduction_test.strip()}
```

Budget: {max_attempts} patch submissions and {max_tool_calls} tool calls in total.
Start by locating the code involved, then fix it and call submit_patch."""

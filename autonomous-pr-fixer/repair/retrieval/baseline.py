"""The Phase 1 baseline ranker: the gate's existing lexical + AST localizer, unchanged.

Production calls `LocalizationAgent.localize(..., max_candidates=3)`; for the benchmark the full
candidate list is requested so Recall@5/10 can be measured. Files the localizer does not mention
are simply absent from its ranking (they count as misses). Ties keep the localizer's own order.
"""
from __future__ import annotations

from dataclasses import dataclass

from agents.localization_agent import LocalizationAgent
from agents.triage_agent import TriageAgent
from harness.py_interpreter import HOST_PYTHON


@dataclass
class _ReadOnlyWorkspace:
    """What triage and localization read: a directory of source files. Nothing is executed."""
    workspace_dir: str
    python_cmd: str = HOST_PYTHON


def split_issue(problem_statement: str) -> tuple[str, str]:
    lines = problem_statement.strip().splitlines()
    title = lines[0].strip() if lines else ""
    return title, "\n".join(lines[1:])


def phase1_ranking(workspace_dir: str, problem_statement: str, limit: int = 1000) -> list[str]:
    ws = _ReadOnlyWorkspace(workspace_dir)
    title, body = split_issue(problem_statement)
    triage = TriageAgent(ws).triage_issue(0, title, body)  # type: ignore[arg-type]
    result = LocalizationAgent(ws).localize(  # type: ignore[arg-type]
        issue_title=title,
        issue_body=body,
        error_signatures=triage.error_signatures,
        referenced_files=triage.referenced_files,
        max_candidates=limit,
    )
    return list(dict.fromkeys(c.file for c in result.candidates))

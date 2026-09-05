"""
Triage Agent with Pydantic Schema Validation.
Parses issues, extracts error signatures and stack traces, detects project environment,
prepares working branch, and triggers repository indexing.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from harness.docker_sandbox import Sandbox
from retrieval.indexer import RepositoryIndexer


class TriageReport(BaseModel):
    issue_summary: str
    language: str = "Python"
    test_framework: str = "pytest"
    package_manager: str = "pip"
    test_command: str = "pytest -q"
    error_signatures: List[str] = Field(default_factory=list)
    working_branch: str = "main"
    referenced_files: List[str] = Field(default_factory=list)
    stack_traces: List[str] = Field(default_factory=list)
    index_stats: Dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


class TriageAgent:
    """Performs issue triage, environment analysis, and repository preparation."""

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self.indexer = RepositoryIndexer(sandbox.workspace_dir)

    def triage_issue(
        self,
        issue_number: int,
        issue_title: str,
        issue_body: str,
    ) -> TriageReport:
        combined = f"{issue_title}\n{issue_body}"

        # 1. Extract error signatures
        error_matches = re.findall(r"\b([A-Z][a-zA-Z0-9]*(?:Error|Exception))\b", combined)
        error_signatures = list(dict.fromkeys(error_matches))

        # 2. Extract referenced files (.py and modules)
        file_matches = re.findall(r"[\w\./\-]+\.py\b", combined)
        # Also check for module tokens matching existing workspace files
        tokens = re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", combined)
        for t in tokens:
            candidate = f"{t}.py"
            if os.path.exists(os.path.join(self.sandbox.workspace_dir, candidate)):
                if candidate not in file_matches:
                    file_matches.append(candidate)

        referenced_files = list(dict.fromkeys(file_matches))

        # 3. Extract stack traces
        stack_traces = re.findall(
            r"Traceback \(most recent call last\):.*?(?:\w+Error|\w+Exception):.*",
            combined,
            re.DOTALL,
        )

        # 4. Detect package manager and test framework
        pm = self._detect_package_manager()
        tf, test_cmd = self._detect_test_framework()

        # 5. Create clean working branch
        branch_name = f"fix/issue-{issue_number}"
        self.sandbox.exec(f"git checkout -b {branch_name}")

        # 6. Trigger repository indexing
        index_stats = self.indexer.build_index()

        summary = f"Issue #{issue_number}: {issue_title}. "
        if error_signatures:
            summary += f"Identified error signatures: {', '.join(error_signatures)}. "
        summary += f"Targeting {tf} in {self.sandbox.workspace_dir}."

        # Strict Pydantic validation
        return TriageReport(
            issue_summary=summary,
            language="Python",
            test_framework=tf,
            package_manager=pm,
            test_command=test_cmd,
            error_signatures=error_signatures,
            working_branch=branch_name,
            referenced_files=referenced_files,
            stack_traces=stack_traces,
            index_stats=index_stats,
        )

    def _detect_package_manager(self) -> str:
        try:
            files = os.listdir(self.sandbox.workspace_dir)
        except Exception:
            return "pip"
        if "poetry.lock" in files or "pyproject.toml" in files:
            return "poetry"
        if "Pipfile" in files:
            return "pipenv"
        return "pip"

    def _detect_test_framework(self) -> tuple[str, str]:
        return "pytest", f"{self.sandbox.python_cmd} -m pytest -q"

"""
Localization Agent with Authorized Repair Boundary.
Combines code-aware retrieval, AST symbol indexing, ripgrep lexical matching,
and stack trace resolution to output ranked candidates in standard JSON format.
Generates an explicit authorized repair boundary passed to Patch Agent and Admission Controller.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional
from harness.docker_sandbox import Sandbox
from retrieval.retriever import CodeAwareRetriever


@dataclass
class CandidateLocation:
    file: str
    symbol: str
    score: float
    line_start: Optional[int] = None
    line_end: Optional[int] = None


@dataclass
class LocalizationResult:
    candidates: List[CandidateLocation]
    top_1_file: Optional[str] = None
    top_3_files: List[str] = field(default_factory=list)
    authorized_files: List[str] = field(default_factory=list)
    authorized_symbols: List[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def repair_boundary(self) -> Dict[str, List[str]]:
        """Explicit repair boundary for safety checks."""
        return {
            "authorized_files": self.authorized_files,
            "authorized_symbols": self.authorized_symbols,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidates": [asdict(c) for c in self.candidates],
            "top_1_file": self.top_1_file,
            "top_3_files": self.top_3_files,
            "repair_boundary": self.repair_boundary,
            "confidence": self.confidence,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class LocalizationAgent:
    """Ranks and shortlists candidate files and symbols for repair."""

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self.retriever = CodeAwareRetriever(sandbox.workspace_dir)

    def localize(
        self,
        issue_title: str,
        issue_body: str,
        error_signatures: Optional[List[str]] = None,
        referenced_files: Optional[List[str]] = None,
        max_candidates: int = 3,
    ) -> LocalizationResult:
        context = self.retriever.retrieve(
            issue_title=issue_title,
            issue_body=issue_body,
            error_signatures=error_signatures,
            referenced_files=referenced_files,
        )

        candidates: List[CandidateLocation] = []
        file_scores: Dict[str, float] = {}
        authorized_symbols: List[str] = []

        # 1. Map matched symbols
        for sym in context.relevant_symbols:
            clean_file = sym.file_path.replace("\\", "/")
            if "test" in clean_file.lower():
                continue
            base_score = 0.85
            if referenced_files and any(rf in clean_file for rf in referenced_files):
                base_score = 0.95

            file_scores[clean_file] = max(file_scores.get(clean_file, 0.0), base_score)
            authorized_symbols.append(sym.qualified_name)
            candidates.append(
                CandidateLocation(
                    file=clean_file,
                    symbol=sym.qualified_name,
                    score=round(base_score, 2),
                    line_start=sym.line_start,
                    line_end=sym.line_end,
                )
            )

        # 2. Add files that had lexical or reference hits
        for f in context.relevant_files:
            clean_f = f.replace("\\", "/")
            if "test" in clean_f.lower():
                continue
            if clean_f not in file_scores:
                score = 0.70
                file_scores[clean_f] = score
                candidates.append(
                    CandidateLocation(
                        file=clean_f,
                        symbol="<module>",
                        score=round(score, 2),
                    )
                )

        # Sort candidates descending by score
        candidates.sort(key=lambda c: c.score, reverse=True)
        shortlist = candidates[:max_candidates]

        top_files = list(dict.fromkeys([c.file for c in shortlist]))
        top_1 = top_files[0] if top_files else None
        top_3 = top_files[:3]
        confidence = shortlist[0].score if shortlist else 0.0

        return LocalizationResult(
            candidates=shortlist,
            top_1_file=top_1,
            top_3_files=top_3,
            authorized_files=top_files,
            authorized_symbols=list(dict.fromkeys(authorized_symbols)),
            confidence=confidence,
        )

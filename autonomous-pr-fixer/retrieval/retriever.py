"""
Code-Aware Hybrid Retrieval Layer.
Combines lexical keyword/stack-trace matching with structural AST indices
to retrieve relevant source files, target functions/classes, and related tests.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from retrieval.ast_index import SymbolNode
from retrieval.indexer import RepositoryIndexer
from retrieval.lexical_search import LexicalSearch


@dataclass
class RetrievedContext:
    relevant_files: List[str]
    relevant_symbols: List[SymbolNode]
    related_tests: List[str]
    evidence_snippets: List[str] = field(default_factory=list)


class CodeAwareRetriever:
    """Performs hybrid code-aware retrieval for software repair tasks."""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.indexer = RepositoryIndexer(self.workspace_dir)
        self.lexical = LexicalSearch(self.workspace_dir)

    def retrieve(
        self,
        issue_title: str,
        issue_body: str,
        error_signatures: Optional[List[str]] = None,
        referenced_files: Optional[List[str]] = None,
    ) -> RetrievedContext:
        """Retrieves targeted context by joining lexical hits with AST relations."""
        if not self.indexer.is_indexed:
            self.indexer.build_index()

        error_sigs = error_signatures or []
        ref_files = referenced_files or []

        matched_files: Dict[str, float] = {}
        matched_symbols: List[SymbolNode] = []
        related_tests: Set[str] = set()
        snippets: List[str] = []

        # 1. Check explicitly referenced files from stack traces or issue text
        for rf in ref_files:
            clean_rf = rf.replace("\\", "/")
            for indexed_path in self.indexer.ast_indexer.file_indexes.keys():
                if indexed_path.endswith(clean_rf) or os.path.basename(indexed_path) == os.path.basename(clean_rf):
                    matched_files[indexed_path] = matched_files.get(indexed_path, 0.0) + 10.0
                    snippets.append(f"Referenced directly in issue/trace: {indexed_path}")

        # 2. Check error signatures and keywords in symbol lookup
        search_terms = list(error_sigs)
        for word in (issue_title + " " + issue_body).split():
            clean = "".join(ch for ch in word if ch.isalnum() or ch == "_")
            if len(clean) >= 4 and clean not in ("this", "that", "with", "from", "when", "error"):
                search_terms.append(clean)

        for term in search_terms[:15]:
            # Symbol definitions
            symbols = self.indexer.get_symbol(term)
            for sym in symbols:
                if not self.indexer.ast_indexer.file_indexes.get(sym.file_path, None):
                    continue
                is_test = self.indexer.ast_indexer.file_indexes[sym.file_path].is_test
                if not is_test:
                    matched_files[sym.file_path] = matched_files.get(sym.file_path, 0.0) + 5.0
                    matched_symbols.append(sym)
                    snippets.append(f"Symbol match: {sym.qualified_name} in {sym.file_path}:{sym.line_start}")

            # Lexical hits
            matches = self.lexical.search(term, max_matches=5)
            for m in matches:
                is_test = "test" in m.file_path.lower()
                weight = 1.0 if not is_test else 0.5
                matched_files[m.file_path] = matched_files.get(m.file_path, 0.0) + weight

        # 3. Pull mapped tests for matched source files
        for src_f in matched_files.keys():
            tests = self.indexer.get_tests_for_source(src_f)
            related_tests.update(tests)

        # Sort files by relevance score
        ranked_files = sorted(matched_files.items(), key=lambda x: x[1], reverse=True)
        top_files = [f[0] for f in ranked_files]

        # Deduplicate symbols
        seen_syms = set()
        unique_symbols = []
        for s in matched_symbols:
            key = (s.file_path, s.qualified_name)
            if key not in seen_syms:
                seen_syms.add(key)
                unique_symbols.append(s)

        return RetrievedContext(
            relevant_files=top_files,
            relevant_symbols=unique_symbols,
            related_tests=list(related_tests),
            evidence_snippets=snippets[:10],
        )

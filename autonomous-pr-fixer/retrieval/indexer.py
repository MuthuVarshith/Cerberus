"""
Repository Indexing Layer.
Coordinates AST structural indexing, manages cache/persistence, and exposes
unified querying interfaces for files, symbols, and test mappings.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any, Dict, List, Optional
from retrieval.ast_index import ASTIndexer, FileIndex, SymbolNode


class RepositoryIndexer:
    """Coordinates structural indexing of the entire codebase."""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.ast_indexer = ASTIndexer(self.workspace_dir)
        self.is_indexed = False

    def build_index(self) -> Dict[str, Any]:
        """Indexes the repository and returns summary statistics."""
        file_indexes = self.ast_indexer.index_repository()
        self.is_indexed = True

        total_files = len(file_indexes)
        total_symbols = sum(len(f.symbols) for f in file_indexes.values())
        test_files = sum(1 for f in file_indexes.values() if f.is_test)

        return {
            "total_files": total_files,
            "total_symbols": total_symbols,
            "test_files": test_files,
            "source_files": total_files - test_files,
            "workspace_dir": self.workspace_dir,
        }

    def get_symbol(self, symbol_name: str) -> List[SymbolNode]:
        """Returns all definitions matching symbol_name (case-insensitive)."""
        if not self.is_indexed:
            self.build_index()
        return self.ast_indexer.symbol_lookup.get(symbol_name.lower(), [])

    def get_file_symbols(self, file_path: str) -> List[SymbolNode]:
        """Returns all symbols defined in file_path."""
        if not self.is_indexed:
            self.build_index()
        clean = file_path.replace("\\", "/")
        f_idx = self.ast_indexer.file_indexes.get(clean)
        return f_idx.symbols if f_idx else []

    def get_tests_for_source(self, source_file: str) -> List[str]:
        """Finds test files mapped to a given source file."""
        if not self.is_indexed:
            self.build_index()
        clean = source_file.replace("\\", "/")
        tests: List[str] = []
        for t_file, sources in self.ast_indexer.test_to_source_map.items():
            if clean in sources:
                tests.append(t_file)
        return tests

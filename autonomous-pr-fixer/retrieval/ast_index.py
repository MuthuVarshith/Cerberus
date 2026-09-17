"""
AST and Structural Indexing Engine.
Extracts functions, classes, methods, parameters, line spans, imports,
module relationships, and test-to-source links using Tree-sitter with Python AST fallback.
Represents symbols with stable identifiers: file_path::Parent.name
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

# Attempt tree-sitter import
try:
    from tree_sitter import Language, Parser
    import tree_sitter_python
    PY_LANGUAGE = Language(tree_sitter_python.language())
    TS_AVAILABLE = True
except Exception:
    TS_AVAILABLE = False


@dataclass
class SymbolNode:
    name: str
    kind: str  # "function", "class", "method"
    file_path: str
    line_start: int
    line_end: int
    parent: Optional[str] = None
    parameters: List[str] = field(default_factory=list)
    docstring: Optional[str] = None
    references: List[str] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        if self.parent:
            return f"{self.parent}.{self.name}"
        return self.name

    @property
    def stable_id(self) -> str:
        """Stable identifier: src/parser.py::Parser.parse"""
        return f"{self.file_path}::{self.qualified_name}"


@dataclass
class FileIndex:
    file_path: str
    is_test: bool
    imports: List[str] = field(default_factory=list)
    symbols: List[SymbolNode] = field(default_factory=list)
    tested_modules: List[str] = field(default_factory=list)


class ASTIndexer:
    """Builds and queries structural AST indexes for Python repositories."""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.file_indexes: Dict[str, FileIndex] = {}
        self.symbol_lookup: Dict[str, List[SymbolNode]] = {}
        self.test_to_source_map: Dict[str, List[str]] = {}
        self.parser = Parser(PY_LANGUAGE) if TS_AVAILABLE else None

    def index_repository(self) -> Dict[str, FileIndex]:
        self.file_indexes.clear()
        self.symbol_lookup.clear()
        self.test_to_source_map.clear()

        for root, dirs, files in os.walk(self.workspace_dir):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d not in ("__pycache__", "venv", "env", "node_modules", ".git")
            ]
            for file in files:
                if not file.endswith(".py"):
                    continue
                full_path = os.path.join(root, file)
                if os.path.islink(full_path):
                    continue  # never follow a workspace symlink from the host
                rel_path = os.path.relpath(full_path, self.workspace_dir).replace("\\", "/")
                self._index_file(full_path, rel_path)

        self._link_tests_to_source()
        return self.file_indexes

    def _index_file(self, full_path: str, rel_path: str) -> None:
        is_test = (
            "test" in rel_path.lower()
            or os.path.basename(rel_path).startswith("test_")
            or os.path.basename(rel_path).endswith("_test.py")
        )

        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            tree = ast.parse(content, filename=rel_path)
        except Exception:
            return

        imports: List[str] = []
        symbols: List[SymbolNode] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)

        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                params = [arg.arg for arg in node.args.args]
                end_lineno = getattr(node, "end_lineno", node.lineno)
                sym = SymbolNode(
                    name=node.name,
                    kind="function",
                    file_path=rel_path,
                    line_start=node.lineno,
                    line_end=end_lineno,
                    parameters=params,
                    docstring=ast.get_docstring(node),
                )
                symbols.append(sym)
                self.symbol_lookup.setdefault(node.name.lower(), []).append(sym)
                self.symbol_lookup.setdefault(sym.stable_id.lower(), []).append(sym)

            elif isinstance(node, ast.ClassDef):
                end_lineno = getattr(node, "end_lineno", node.lineno)
                cls_sym = SymbolNode(
                    name=node.name,
                    kind="class",
                    file_path=rel_path,
                    line_start=node.lineno,
                    line_end=end_lineno,
                    docstring=ast.get_docstring(node),
                )
                symbols.append(cls_sym)
                self.symbol_lookup.setdefault(node.name.lower(), []).append(cls_sym)
                self.symbol_lookup.setdefault(cls_sym.stable_id.lower(), []).append(cls_sym)

                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        params = [arg.arg for arg in item.args.args]
                        method_end = getattr(item, "end_lineno", item.lineno)
                        method_sym = SymbolNode(
                            name=item.name,
                            kind="method",
                            file_path=rel_path,
                            line_start=item.lineno,
                            line_end=method_end,
                            parent=node.name,
                            parameters=params,
                            docstring=ast.get_docstring(item),
                        )
                        symbols.append(method_sym)
                        self.symbol_lookup.setdefault(item.name.lower(), []).append(method_sym)
                        self.symbol_lookup.setdefault(
                            f"{node.name.lower()}.{item.name.lower()}", []
                        ).append(method_sym)
                        self.symbol_lookup.setdefault(method_sym.stable_id.lower(), []).append(method_sym)

        self.file_indexes[rel_path] = FileIndex(
            file_path=rel_path,
            is_test=is_test,
            imports=imports,
            symbols=symbols,
        )

    def _link_tests_to_source(self) -> None:
        for rel_path, f_index in self.file_indexes.items():
            if not f_index.is_test:
                continue

            base = os.path.basename(rel_path)
            target_candidates = []
            if base.startswith("test_"):
                target_candidates.append(base[5:])
            if base.endswith("_test.py"):
                target_candidates.append(base[:-8] + ".py")

            tested_modules = []
            for candidate in target_candidates:
                for src_path, src_index in self.file_indexes.items():
                    if not src_index.is_test and os.path.basename(src_path) == candidate:
                        tested_modules.append(src_path)

            for imp in f_index.imports:
                for src_path, src_index in self.file_indexes.items():
                    if not src_index.is_test:
                        mod_name = os.path.splitext(os.path.basename(src_path))[0]
                        if imp == mod_name or imp.endswith(f".{mod_name}"):
                            tested_modules.append(src_path)

            f_index.tested_modules = list(dict.fromkeys(tested_modules))
            self.test_to_source_map[rel_path] = f_index.tested_modules

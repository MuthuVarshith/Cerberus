"""search_code, find_symbol, find_references: lexical and AST lookups reusing retrieval/ (no vector search)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from retrieval.lexical_search import LexicalSearch
from repair.tools.base import INVALID_ARGUMENT, Tool, ToolContext, ToolError
from repair.tools.files import read_lines

SEARCHABLE_EXTENSIONS = (".py", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".json")


def _enclosing_symbol(ctx: ToolContext, path: str, line: int) -> Optional[str]:
    if not path.endswith(".py"):
        return None
    fi = ctx.index().file_indexes.get(path)
    if not fi:
        return None
    best = None
    for s in fi.symbols:
        if s.line_start <= line <= s.line_end and (best is None or s.line_start >= best.line_start):
            best = s
    return best.qualified_name if best else None


def _context(ctx: ToolContext, path: str, line: int, radius: int) -> str:
    try:
        lines = read_lines(ctx, path)
    except ToolError:
        return ""
    lo, hi = max(1, line - radius), min(len(lines), line + radius)
    return "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(lo, hi + 1))


class SearchCodeInput(BaseModel):
    query: str = Field(min_length=2, max_length=200, description="Literal text: identifier, error message, keyword")
    file_extension: str = Field(".py", description=f"One of {', '.join(SEARCHABLE_EXTENSIONS)}")
    max_results: int = Field(20, ge=1, le=50)
    context_lines: int = Field(2, ge=0, le=5)


class SearchCode(Tool):
    name = "search_code"
    description = ("Case-insensitive literal search over repository files. Returns file, line, matching text, "
                   "a few lines of context and the enclosing function/class when known.")
    Input = SearchCodeInput

    def run(self, ctx: ToolContext, args: SearchCodeInput) -> Dict[str, Any]:
        if args.file_extension not in SEARCHABLE_EXTENSIONS:
            raise ToolError(INVALID_ARGUMENT, f"file_extension must be one of {SEARCHABLE_EXTENSIONS}")
        matches = LexicalSearch(ctx.sandbox.workspace_dir).search(
            args.query, file_extension=args.file_extension, max_matches=args.max_results,
        )
        return {
            "query": args.query,
            "matches": [
                {
                    "file": m.file_path,
                    "line": m.line_number,
                    "text": m.line_content[:300],
                    "symbol": _enclosing_symbol(ctx, m.file_path, m.line_number),
                    "context": _context(ctx, m.file_path, m.line_number, args.context_lines) if args.context_lines else "",
                }
                for m in matches
            ],
            "truncated": len(matches) >= args.max_results,
        }


class FindSymbolInput(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="Function, class, method ('Class.method') or module ('pkg.mod') name")
    kind: Optional[str] = Field(None, description="Optional filter: function | class | method | module")


class FindSymbol(Tool):
    name = "find_symbol"
    description = "Locate definitions of a Python function, class, method or module; returns file, line range and source."
    Input = FindSymbolInput

    def run(self, ctx: ToolContext, args: FindSymbolInput) -> Dict[str, Any]:
        kind = (args.kind or "").lower() or None
        if kind not in (None, "function", "class", "method", "module"):
            raise ToolError(INVALID_ARGUMENT, "kind must be function, class, method or module")
        idx = ctx.index()
        results: List[Dict[str, Any]] = []
        if kind in (None, "module"):
            want = args.name.replace(".", "/")
            for path in sorted(idx.file_indexes):
                if path[:-3] == want or path[:-3].endswith("/" + want) or path == want + "/__init__.py":
                    defined = [s.qualified_name for s in idx.file_indexes[path].symbols][:40]
                    results.append({"symbol": args.name, "kind": "module", "file": path, "start_line": 1,
                                    "end_line": None, "context": "defines: " + ", ".join(defined)})
        if kind != "module":
            for fi in idx.file_indexes.values():
                for s in fi.symbols:
                    if args.name not in (s.name, s.qualified_name):
                        continue
                    if kind and s.kind != kind:
                        continue
                    span = min(s.line_end, s.line_start + 59)
                    try:
                        lines = read_lines(ctx, s.file_path)
                        src = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(s.line_start, span + 1))
                    except ToolError:
                        src = ""
                    results.append({"symbol": s.qualified_name, "kind": s.kind, "file": s.file_path,
                                    "start_line": s.line_start, "end_line": s.line_end, "context": src})
                    ctx.note(ctx.files_read, s.file_path)
        return {"name": args.name, "definitions": results[:20]}


class FindReferencesInput(BaseModel):
    name: str = Field(min_length=2, max_length=200, description="Identifier to find, matched as a whole word")
    max_results: int = Field(30, ge=1, le=60)


class FindReferences(Tool):
    name = "find_references"
    description = "Whole-word lexical search for an identifier across Python files (definitions are flagged)."
    Input = FindReferencesInput

    def run(self, ctx: ToolContext, args: FindReferencesInput) -> Dict[str, Any]:
        bare = args.name.split(".")[-1]
        word = re.compile(rf"\b{re.escape(bare)}\b")
        definition = re.compile(rf"^\s*(async\s+def|def|class)\s+{re.escape(bare)}\b")
        raw = LexicalSearch(ctx.sandbox.workspace_dir).search(bare, case_sensitive=True, max_matches=args.max_results * 4)
        refs = [
            {"file": m.file_path, "line": m.line_number, "text": m.line_content[:300],
             "is_definition": bool(definition.match(m.line_content))}
            for m in raw if word.search(m.line_content)
        ][: args.max_results]
        return {"name": args.name, "references": refs, "files": sorted({r["file"] for r in refs})}

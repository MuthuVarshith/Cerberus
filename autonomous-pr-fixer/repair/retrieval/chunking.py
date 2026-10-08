"""Code-aware chunking of Python source with Tree-sitter.

Each file becomes chunks of semantic units:

- ``function``: a top-level function (decorators included);
- ``class``: a class header, docstring and class-level statements up to its first method;
- ``method``: each method, named ``Class.method`` (nested classes are qualified the same way);
- ``module``: the remaining top-level code (imports, constants, ``if __name__`` blocks).

Definitions longer than ``MAX_CHUNK_LINES`` are split into overlapping line windows that keep the
symbol name. Files Tree-sitter cannot parse into any definition (or that fail to decode) fall back
to plain line windows (``kind="window"``). Parsing reads text only; nothing is imported or run.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

MAX_CHUNK_LINES = 150
WINDOW_LINES = 60
WINDOW_OVERLAP = 10

_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    path: str
    symbol: str          # qualified name, "<module>" or "<window>"
    kind: str            # function | class | method | module | window
    start_line: int      # 1-based, inclusive
    end_line: int        # 1-based, inclusive
    text: str
    docstring: str = ""
    imports: tuple[str, ...] = field(default=())

    @property
    def chunk_id(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}:{self.symbol}"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", "replace")).hexdigest()


@lru_cache(maxsize=1)
def _parser() -> Any:
    import tree_sitter_python
    from tree_sitter import Language, Parser
    return Parser(Language(tree_sitter_python.language()))


def module_imports(source: str) -> tuple[str, ...]:
    names: list[str] = []
    for frm, imp in _IMPORT_RE.findall(source):
        if frm:
            names.append(frm)
        else:
            names += [n.strip().split(" as ")[0] for n in imp.split(",") if n.strip()]
    return tuple(dict.fromkeys(names))


def _windows(path: str, lines: list[str], start: int, end: int, symbol: str, kind: str,
             docstring: str, imports: tuple[str, ...], size: int = WINDOW_LINES,
             overlap: int = WINDOW_OVERLAP) -> Iterator[Chunk]:
    """Line windows over lines[start-1:end] (1-based, inclusive)."""
    step = max(1, size - overlap)
    s = start
    while s <= end:
        e = min(end, s + size - 1)
        yield Chunk(path, symbol, kind, s, e, "\n".join(lines[s - 1:e]), docstring, imports)
        if e == end:
            break
        s += step


def _docstring(body: Any, src: bytes) -> str:
    if body is None:
        return ""
    for child in body.children:
        if child.type == "expression_statement" and child.children and child.children[0].type == "string":
            raw = src[child.start_byte:child.end_byte].decode("utf-8", "replace")
            return raw.strip("\"' \n\r\t")[:500]
        if child.type not in ("comment",):
            return ""
    return ""


def _unwrap(node: Any) -> Any:
    """decorated_definition -> its function/class; anything else unchanged."""
    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        return inner if inner is not None else node
    return node


def _name(node: Any, src: bytes) -> str:
    n = node.child_by_field_name("name")
    return src[n.start_byte:n.end_byte].decode("utf-8", "replace") if n is not None else "?"


def _emit(path: str, lines: list[str], start: int, end: int, symbol: str, kind: str,
          docstring: str, imports: tuple[str, ...]) -> Iterator[Chunk]:
    if end - start + 1 > MAX_CHUNK_LINES:
        yield from _windows(path, lines, start, end, symbol, kind, docstring, imports,
                            size=MAX_CHUNK_LINES, overlap=WINDOW_OVERLAP * 2)
    else:
        yield Chunk(path, symbol, kind, start, end, "\n".join(lines[start - 1:end]), docstring, imports)


def _class_chunks(path: str, node: Any, outer: Any, src: bytes, lines: list[str], prefix: str,
                  imports: tuple[str, ...]) -> Iterator[Chunk]:
    name = f"{prefix}{_name(node, src)}"
    body = node.child_by_field_name("body")
    doc = _docstring(body, src)
    start = outer.start_point[0] + 1
    members = [c for c in (body.children if body is not None else [])
               if _unwrap(c).type in ("function_definition", "class_definition")]
    header_end = (members[0].start_point[0] if members else outer.end_point[0] + 1)
    yield from _emit(path, lines, start, max(start, header_end), name, "class", doc, imports)
    for member in members:
        inner = _unwrap(member)
        if inner.type == "class_definition":
            yield from _class_chunks(path, inner, member, src, lines, f"{name}.", imports)
        else:
            yield from _emit(path, lines, member.start_point[0] + 1, member.end_point[0] + 1,
                             f"{name}.{_name(inner, src)}", "method",
                             _docstring(inner.child_by_field_name("body"), src), imports)


def chunk_source(path: str, source: str) -> list[Chunk]:
    lines = source.splitlines()
    if not lines:
        return []
    imports = module_imports(source)
    src = source.encode("utf-8", "replace")
    try:
        tree = _parser().parse(src)
    except Exception:  # defensive: Tree-sitter is error-tolerant, but never let one file stop indexing
        tree = None
    chunks: list[Chunk] = []
    covered: set[int] = set()
    if tree is not None:
        for top in tree.root_node.children:
            inner = _unwrap(top)
            start, end = top.start_point[0] + 1, top.end_point[0] + 1
            if inner.type == "function_definition":
                chunks += _emit(path, lines, start, end, _name(inner, src), "function",
                                _docstring(inner.child_by_field_name("body"), src), imports)
            elif inner.type == "class_definition":
                chunks += _class_chunks(path, inner, top, src, lines, "", imports)
            else:
                continue
            covered.update(range(start, end + 1))
    if tree is None or (not chunks and tree.root_node.has_error):
        return list(_windows(path, lines, 1, len(lines), "<window>", "window", "", imports))
    rest = [i for i in range(1, len(lines) + 1) if i not in covered and lines[i - 1].strip()]
    for block in _contiguous(rest):
        chunks += _windows(path, lines, block[0], block[-1], "<module>", "module", "", imports,
                           size=MAX_CHUNK_LINES, overlap=WINDOW_OVERLAP)
    return chunks


def _contiguous(nums: Iterable[int], gap: int = 3) -> Iterator[list[int]]:
    """Group sorted line numbers into blocks, bridging gaps of up to `gap` lines."""
    block: list[int] = []
    for n in nums:
        if block and n - block[-1] > gap:
            yield block
            block = []
        block.append(n)
    if block:
        yield block


def chunk_repository(sources: dict[str, str]) -> list[Chunk]:
    out: list[Chunk] = []
    for path in sorted(sources):
        out += chunk_source(path, sources[path])
    return out

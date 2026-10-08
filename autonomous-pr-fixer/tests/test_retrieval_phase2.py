"""Phase 2 retrieval: localization metrics, benchmark construction and text-only source export."""
from __future__ import annotations

import os
import subprocess

import pytest

from evaluation.localization.build_benchmark import gold_files, mentions
from repair.retrieval.metrics import aggregate, score_instance
from repair.retrieval.source import materialize, python_files, read_sources


def test_recall_all_found_and_mrr():
    s = score_instance("x", ["a.py", "b.py", "a.py", "c.py"], {"b.py", "c.py"})
    assert s.first_gold_rank == 2 and s.reciprocal_rank == 0.5
    assert s.recall == {1: 0.0, 5: 1.0, 10: 1.0}          # duplicate "a.py" is not double-counted
    assert s.all_found == {1: False, 5: True, 10: True}
    miss = score_instance("y", ["z.py"], {"b.py"})
    assert miss.first_gold_rank is None and miss.reciprocal_rank == 0.0
    agg = aggregate([s, miss])
    assert agg.n == 2 and agg.mrr == 0.25 and agg.recall[5] == 0.5 and agg.not_ranked == 1


def test_empty_gold_is_rejected():
    with pytest.raises(ValueError):
        score_instance("x", ["a.py"], set())


def test_gold_files_exclude_tests_and_non_python():
    patch = ("diff --git a/pkg/core.py b/pkg/core.py\n--- a/pkg/core.py\n+++ b/pkg/core.py\n"
             "diff --git a/tests/test_core.py b/tests/test_core.py\n"
             "diff --git a/docs/index.rst b/docs/index.rst\n")
    assert gold_files(patch) == ["pkg/core.py"]


def test_path_mentions():
    assert mentions("see django/db/models/query.py", "django/db/models/query.py")
    assert mentions("in django.db.models.query the", "django/db/models/query.py")
    assert mentions("bug in query.py today", "django/db/models/query.py")
    assert not mentions("nothing here", "django/db/models/query.py")
    assert not mentions("pkg/__init__.py mentioned", "other/__init__.py")


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def mirror(tmp_path):
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / "pkg" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (src / "pkg" / "blob.py").write_bytes(b"\x00\x01binary")
    (src / "tests" / "test_core.py").write_text("def test_f():\n    pass\n", encoding="utf-8")
    (src / "README.md").write_text("hi", encoding="utf-8")
    _git(str(src), "init", "-q")
    _git(str(src), "-c", "user.name=t", "-c", "user.email=t@x", "add", "-A")
    _git(str(src), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "base")
    bare = tmp_path / "m.git"
    _git(str(tmp_path), "clone", "-q", "--bare", str(src), str(bare))
    return str(bare), _git(str(src), "rev-parse", "HEAD")


def test_source_export_is_text_only_and_skips_tests(mirror, tmp_path):
    bare, commit = mirror
    assert python_files(bare, commit) == ["pkg/blob.py", "pkg/core.py"]
    srcs = read_sources(bare, commit, python_files(bare, commit))
    assert set(srcs) == {"pkg/core.py"}          # binary file skipped
    out = tmp_path / "ws"
    written = materialize(bare, commit, str(out))
    assert sorted(written) == ["pkg/blob.py", "pkg/core.py"]
    assert not os.path.exists(out / "tests" / "test_core.py") and not os.path.exists(out / "README.md")


# ------------------------------------------------------------------ chunking, BM25, aggregation

from repair.retrieval.aggregate import files_from_chunks  # noqa: E402
from repair.retrieval.bm25 import BM25Index, tokenize  # noqa: E402
from repair.retrieval.chunking import MAX_CHUNK_LINES, Chunk, chunk_source  # noqa: E402

SRC = '''import os
from django.db import models

LIMIT = 3


class Identifier(Base):
    """A SQL identifier."""
    kind = "name"

    def get_real_name(self):
        """Return the real name."""
        return self.tokens[-1]

    class Meta:
        def inner(self):
            return 1


@cached
def parse(sql):
    return sql.split()


if __name__ == "__main__":
    parse("x")
'''


def test_chunks_cover_functions_classes_methods_and_module_code():
    chunks = chunk_source("pkg/sql.py", SRC)
    by_symbol = {c.symbol: c for c in chunks}
    assert by_symbol["Identifier"].kind == "class" and by_symbol["Identifier"].docstring == "A SQL identifier."
    m = by_symbol["Identifier.get_real_name"]
    assert m.kind == "method" and m.start_line == 11 and m.end_line == 13 and m.docstring == "Return the real name."
    assert by_symbol["Identifier.Meta.inner"].kind == "method"
    p = by_symbol["parse"]
    assert p.kind == "function" and p.start_line == 20 and "@cached" in p.text
    modules = [c for c in chunks if c.kind == "module"]
    assert modules and any("LIMIT = 3" in c.text for c in modules) and any("__main__" in c.text for c in modules)
    assert all(c.imports == ("os", "django.db") for c in chunks)


def test_long_definitions_are_windowed_and_broken_files_fall_back():
    body = "\n".join(f"    x{i} = {i}" for i in range(MAX_CHUNK_LINES + 40))
    chunks = chunk_source("big.py", f"def big():\n{body}\n")
    assert len(chunks) >= 2 and all(c.symbol == "big" and c.end_line - c.start_line < MAX_CHUNK_LINES for c in chunks)
    broken = chunk_source("bad.py", "def (:\n  ))) this is not python\n" * 5)
    assert broken and all(c.kind == "window" for c in broken)
    assert chunk_source("empty.py", "") == []


def test_tokenize_splits_snake_and_camel_and_keeps_whole_identifiers():
    toks = tokenize("get_real_name HTTPResponseError self.return")
    assert {"get_real_name", "get", "real", "name", "httpresponseerror", "http", "response", "error"} <= set(toks)
    assert "self" not in toks and "return" not in toks


def test_bm25_prefers_the_relevant_chunk_and_is_deterministic():
    chunks = [Chunk("a/sql.py", "Identifier.get_real_name", "method", 1, 3, "def get_real_name(self): ..."),
              Chunk("a/other.py", "unrelated", "function", 1, 3, "def unrelated(): return 42"),
              Chunk("a/names.py", "alias", "function", 1, 3, "def alias(): name = real")]
    index = BM25Index(chunks)
    hits = index.search("get_real_name returns the wrong component")
    assert hits[0][0].path == "a/sql.py"
    assert index.search("get_real_name returns the wrong component") == hits
    assert index.search("zzzz qqqq") == []


def test_file_aggregation_max_and_sum_top3():
    c = lambda p: Chunk(p, "s", "function", 1, 1, "")  # noqa: E731
    scored = [(c("a.py"), 5.0), (c("b.py"), 4.0), (c("b.py"), 3.0), (c("b.py"), 2.0), (c("c.py"), 4.0)]
    assert files_from_chunks(scored, "max") == ["a.py", "b.py", "c.py"]
    assert files_from_chunks(scored, "sum_top3") == ["b.py", "a.py", "c.py"]
    with pytest.raises(ValueError):
        files_from_chunks(scored, "median")

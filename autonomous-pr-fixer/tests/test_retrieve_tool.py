"""retrieve_code tool and workspace retriever (BM25 path; no model downloads)."""
from __future__ import annotations

import os

import pytest

from harness.docker_sandbox import ISOLATION_HOST_UNSAFE, Sandbox
from repair.config import RepairConfig
from repair.llm.fake import FakeChatModel, call, respond
from repair.observability.langfuse import RecordingTracer
from repair.retrieval.service import WorkspaceRetriever
from repair.runner import RepairTask, run_repair
from repair.state import FinalStatus
from repair.tools import build_registry
from repair.tools.base import ToolContext
from repair.tools.retrieve import DATA_CLOSE, DATA_OPEN
from tests import repair_fixtures as fx


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_retriever_finds_code_and_reindexes_only_changed_files(tmp_path):
    ws = str(tmp_path / "ws")
    _write(ws, "pkg/rates.py", "def percent(part, whole):\n    return part / whole * 100\n")
    _write(ws, "pkg/fmt.py", "def fmt(value):\n    return f'{value:.1f}%'\n")
    r = WorkspaceRetriever(ws, commit=None)
    assert r.build()["rechunked"] == 2
    assert r.search("percent divides by whole", 3)[0][0].path == "pkg/rates.py"
    _write(ws, "pkg/fmt.py", "def fmt(value):\n    return f'{value:.2f} percent'\n")
    assert r.refresh() == {"files": 2, "rechunked": 1, "removed": 0}
    os.remove(os.path.join(ws, "pkg", "fmt.py"))
    assert r.refresh()["removed"] == 1


def test_index_is_cached_by_commit(tmp_path):
    ws = str(tmp_path / "ws")
    _write(ws, "a.py", "def alpha():\n    return 1\n")
    cache = str(tmp_path / "cache")
    WorkspaceRetriever(ws, commit="abc123", cache_dir=cache).build()
    assert os.path.exists(os.path.join(cache, "abc123.json"))
    again = WorkspaceRetriever(ws, commit="abc123", cache_dir=cache)
    assert again.refresh()["rechunked"] == 0          # loaded from the commit cache, nothing re-chunked


def test_symlinks_are_not_followed(tmp_path):
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    _write(str(outside), "secret.py", "API_TOKEN = 'do-not-index'\n")
    _write(str(ws), "a.py", "def alpha():\n    return 1\n")
    try:
        os.symlink(outside / "secret.py", ws / "link.py")
        os.symlink(outside, ws / "linkdir", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available to this user on this machine")
    r = WorkspaceRetriever(str(ws), commit=None)
    r.build()
    assert all("do-not-index" not in c.text for c, _ in r.search("API_TOKEN secret", 10))


def test_unknown_retriever_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        WorkspaceRetriever(str(tmp_path), commit=None, kind="magic")


@pytest.fixture
def sandbox(tmp_path):
    sb = Sandbox(base_dir=fx.make_repo(str(tmp_path)), isolation=ISOLATION_HOST_UNSAFE, timeout_sec=60)
    yield sb
    sb.destroy()


def test_tool_returns_delimited_snippets_and_is_off_by_default(sandbox):
    assert "retrieve_code" not in [s.name for s in build_registry().specs()]
    reg = build_registry(retrieval=True)
    ctx = ToolContext(sandbox=sandbox, retrieval_kind="bm25")
    out = reg.execute(ctx, "retrieve_code", {"query": "percent raises ZeroDivisionError when whole is 0", "k": 3})
    assert out.ok and out.payload["files"][0] == "calc/rates.py" and out.payload["retriever"] == "bm25"
    snippet = out.payload["results"][0]["snippet"]
    assert snippet.startswith(DATA_OPEN) and snippet.endswith(DATA_CLOSE) and "untrusted" in out.payload["note"]
    assert reg.execute(ctx, "retrieve_code", {"query": "x"}).error_code == "INVALID_ARGUMENT"


def test_retrieval_env_validation(monkeypatch):
    monkeypatch.setenv("RETRIEVAL", "bm25")
    assert RepairConfig.from_env().retrieval == "bm25"
    monkeypatch.setenv("RETRIEVAL", "none")
    assert RepairConfig.from_env().retrieval is None
    monkeypatch.setenv("RETRIEVAL", "magic")
    with pytest.raises(ValueError):
        RepairConfig.from_env()


def test_agent_uses_retrieve_code_then_the_real_gate_admits(tmp_path):
    task = RepairTask(repo_dir=fx.make_repo(str(tmp_path)), issue_id="#1", title=fx.TITLE, body=fx.BODY,
                      reproduction_test=fx.REPRO)
    model = FakeChatModel([
        respond(call("retrieve_code", query="percent ZeroDivisionError whole is 0", k=5)),
        respond(call("edit_file", path="calc/rates.py", old_text=fx.OLD, new_text=fx.GOOD)),
        respond(call("submit_patch", reasoning="guard zero whole")),
    ])
    tracer = RecordingTracer()
    state = run_repair(task, fx.config(retrieval="bm25"), model=model, tracer=tracer, run_id="r",
                       out_root=str(tmp_path / "out"))
    assert state.final_status == FinalStatus.ADMITTED
    assert state.tool_calls[0].tool == "retrieve_code" and state.tool_calls[0].ok
    retrievals = [e for e in tracer.events if e["kind"] == "retrieval"]
    assert retrievals and retrievals[0]["top_files"][0] == "calc/rates.py"

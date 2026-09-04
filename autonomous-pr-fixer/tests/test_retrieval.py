"""
Tests for Code-Aware Retrieval & AST Indexing Package.
Verifies:
1. AST indexer extracts classes, functions, methods, and line boundaries.
2. Test-to-source mapping connects test_parser.py to parser.py.
3. Hybrid retriever ranks relevant files and symbols based on issue keywords.
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.docker_sandbox import Sandbox
from retrieval.indexer import RepositoryIndexer
from retrieval.retriever import CodeAwareRetriever


def test_ast_indexer_extracts_symbols():
    with Sandbox() as sb:
        sb.write_file(
            "core/engine.py",
            "class Engine:\n"
            "    def start(self):\n"
            "        return True\n\n"
            "def initialize_cluster():\n"
            "    pass\n"
        )
        sb.write_file("tests/test_engine.py", "import core.engine\ndef test_eng(): pass\n")

        indexer = RepositoryIndexer(sb.workspace_dir)
        stats = indexer.build_index()

        assert stats["total_files"] == 2
        assert stats["total_symbols"] >= 3

        syms = indexer.get_symbol("start")
        assert len(syms) > 0
        assert syms[0].name == "start"
        assert syms[0].kind == "method"
        assert syms[0].parent == "Engine"

        # Check test-to-source mapping
        tests = indexer.get_tests_for_source("core/engine.py")
        assert len(tests) > 0
        assert "tests/test_engine.py" in tests[0]


def test_code_aware_retriever():
    with Sandbox() as sb:
        sb.write_file("auth/oauth.py", "def authenticate_user(token):\n    if not token: raise ValueError('empty')\n")
        sb.write_file("billing/invoice.py", "def create_invoice(): pass\n")

        retriever = CodeAwareRetriever(sb.workspace_dir)
        res = retriever.retrieve(
            issue_title="ValueError in authenticate_user",
            issue_body="Calling authenticate_user with None triggers an exception in oauth.py",
            error_signatures=["ValueError"],
            referenced_files=["oauth.py"],
        )

        assert len(res.relevant_files) > 0
        assert "auth/oauth.py" in res.relevant_files[0]
        assert any(s.name == "authenticate_user" for s in res.relevant_symbols)

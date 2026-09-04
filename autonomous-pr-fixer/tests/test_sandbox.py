"""
Tests for Phase 1: Sandbox & Tooling.
Verifies container/environment lifecycle, command execution with timeouts,
file writes, reads, line-range editing, and grep searches.
"""
import os
import sys
import pytest

# Ensure autonomous-pr-fixer is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.docker_sandbox import Sandbox
from harness.tools import ACI


def test_sandbox_lifecycle_and_exec():
    with Sandbox(timeout_sec=5) as sb:
        assert sb.is_alive
        res = sb.exec("echo 'Phase 1 Sandbox Ready'")
        assert res.exit_code == 0
        assert "Phase 1 Sandbox Ready" in res.output
    # Sandbox must be destroyed cleanly
    assert not sb.is_alive


def test_sandbox_timeout_safety():
    with Sandbox(timeout_sec=2) as sb:
        # Run a sleep longer than timeout
        res = sb.exec("py -3.13 -c \"import time; time.sleep(10)\"", timeout=2)
        assert res.timed_out or res.exit_code in (124, 1)


def test_aci_tools():
    with Sandbox() as sb:
        aci = ACI(sb)
        # 1. Write file
        sb.write_file("sample.py", "def add(a, b):\n    return a - b\n")
        
        # 2. View file
        view = aci.view_file("sample.py")
        assert "def add(a, b):" in view
        assert "return a - b" in view

        # 3. Edit line (fix subtraction bug to addition)
        edit_res = aci.edit_lines("sample.py", 2, 2, "    return a + b")
        assert "Successfully updated" in edit_res

        # 4. View updated file
        view_after = aci.view_file("sample.py")
        assert "return a + b" in view_after

        # 5. Grep tool
        grep_res = aci.grep("return a + b")
        assert "sample.py:2" in grep_res

        # 6. Run command
        run_res = aci.run_cmd("py -3.13 -c \"import sample; print(sample.add(2, 3))\"")
        assert run_res.exit_code == 0
        assert "5" in run_res.output.strip()


def test_sandbox_path_traversal_protection():
    with Sandbox() as sb:
        with pytest.raises(ValueError) as exc:
            sb.write_file("../../outside.txt", "exploit")
        assert "Path traversal detected" in str(exc.value)

        with pytest.raises(ValueError) as exc:
            sb.read_file("../../../etc/passwd")
        assert "Path traversal detected" in str(exc.value)


def test_sandbox_output_truncation():
    with Sandbox() as sb:
        # Generate output larger than 500KB
        res = sb.exec("py -3.13 -c \"print('A' * 600000)\"")
        assert len(res.stdout) <= 500500
        assert "truncated at 500KB" in res.stdout


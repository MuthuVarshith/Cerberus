"""
Tests for Phase 4: Patch Agent + Retry Loop.
Verifies on test scenarios that:
1. When attempt #1 fails, error feedback is captured and attempt #2 patches it -> reaches GREEN.
2. If all attempts fail, loop terminates safely at max_attempts without crashing.
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.docker_sandbox import Sandbox
from harness.diff_utils import DiffUtils
from agents.patch_agent import PatchAgent


def test_patch_loop_self_heals_to_green():
    with Sandbox() as sb:
        # Initialize git repo in sandbox workspace for clean git apply / rollback
        sb.exec("git init")
        sb.exec("git config user.name 'HarnessBot'")
        sb.exec("git config user.email 'bot@test.org'")

        # Write buggy code
        sb.write_file(
            "math_ops.py",
            "def divide(a, b):\n"
            "    # Bug: returns zero on valid division\n"
            "    return 0\n"
        )
        # Commit initial state
        sb.exec("git add math_ops.py && git commit -m 'Initial commit'")

        # Write failing reproduction test
        sb.write_file(
            "test_reproduce.py",
            "from math_ops import divide\n"
            "def test_divide():\n"
            "    assert divide(10, 2) == 5\n"
        )

        agent = PatchAgent(sb, max_attempts=3)

        # Mock patch generator simulating:
        # Attempt 1: Inadequate fix (returns 1 instead of 5 -> fails)
        # Attempt 2: Correct fix (returns a // b -> passes)
        def mock_generator(attempt: int, feedback: str) -> str:
            if attempt == 1:
                return (
                    "--- a/math_ops.py\n"
                    "+++ b/math_ops.py\n"
                    "@@ -2,2 +2,2 @@\n"
                    "-    # Bug: returns zero on valid division\n"
                    "-    return 0\n"
                    "+    # Inadequate fix\n"
                    "+    return 1\n"
                )
            else:
                return (
                    "--- a/math_ops.py\n"
                    "+++ b/math_ops.py\n"
                    "@@ -2,2 +2,2 @@\n"
                    "-    # Bug: returns zero on valid division\n"
                    "-    return 0\n"
                    "+    # Correct fix\n"
                    "+    return a // b\n"
                )

        result = agent.run_patch_loop(["math_ops.py"], mock_generator)

        # Must reach GREEN on attempt 2
        assert result.reached_green is True
        assert result.total_attempts == 2
        assert len(result.history) == 2
        assert result.history[0].reproduction_test_passed is False
        assert result.history[1].reproduction_test_passed is True
        assert "return a // b" in result.winning_diff


def test_patch_loop_aborts_on_duplicate_diff():
    """Agent producing the exact same diff repeatedly aborts early instead of thrashing."""
    with Sandbox() as sb:
        sb.exec("git init && git config user.name 'Bot' && git config user.email 'b@t.co'")
        sb.write_file("calc.py", "def f(): return 0\n")
        sb.exec("git add calc.py && git commit -m 'init'")
        sb.write_file("test_reproduce.py", "from calc import f\ndef test_f(): assert f() == 100\n")

        agent = PatchAgent(sb, max_attempts=5)

        # Generator returns the EXACT same bad diff on every attempt
        bad_diff = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-def f(): return 0\n+def f(): return 1\n"
        result = agent.run_patch_loop(["calc.py"], lambda attempt, fb: bad_diff)

        assert result.reached_green is False
        # Must abort after attempt 2 (since attempt 2 is a duplicate of attempt 1), NOT run all 5 attempts
        assert len(result.history) == 2
        assert result.history[1].structured_failure.status == "ABORT_DUPLICATE_DIFF"



@pytest.mark.parametrize("command", ["", "   "])
def test_empty_test_command_cannot_count_as_green(command):
    """A missing test command used to make GREEN pass automatically."""
    with Sandbox() as sb:
        agent = PatchAgent(sb, max_attempts=1)
        with pytest.raises(ValueError, match="non-empty test command"):
            agent.run_patch_loop(["calc.py"], lambda attempt, fb: "", test_command=command)

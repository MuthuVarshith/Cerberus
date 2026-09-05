"""
Tests for Phase 2: Reproduction Agent and Hard RED Gate.
Verifies:
1. When a reproduction test correctly catches a real bug -> returns RED (reproduced=True).
2. When a test passes on the buggy code -> halts with "Could not reproduce" (reproduced=False).
3. When a test has a SyntaxError -> rejected (reproduced=False).
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.docker_sandbox import Sandbox
from agents.reproduction_agent import ReproductionAgent


def test_red_gate_valid_bug_reproduced():
    with Sandbox() as sb:
        # Create a buggy module: integer division by zero or off-by-one bug
        sb.write_file(
            "calculator.py",
            "def calculate_discount(price, rate):\n"
            "    # Bug: adds discount instead of subtracting\n"
            "    return price + (price * rate)\n"
        )

        agent = ReproductionAgent(sb)
        test_script = (
            "import pytest\n"
            "from calculator import calculate_discount\n\n"
            "def test_discount():\n"
            "    # Expect $100 with 10% discount to be $90\n"
            "    result = calculate_discount(100, 0.10)\n"
            "    assert result == 90.0, f'Expected 90.0 but got {result}'\n"
        )

        res = agent.run_reproduction_gate(
            issue_title="calculate_discount increases price",
            issue_body="calculate_discount(100, 0.10) gives 110 instead of 90",
            generated_test_code=test_script,
        )

        # RED gate must pass: the test fails on buggy code!
        assert res.reproduced is True
        assert res.returncode != 0
        assert "Expected 90.0 but got 110.0" in res.raw_output


def test_red_gate_rejects_passing_test():
    with Sandbox() as sb:
        # Create working code
        sb.write_file(
            "calculator.py",
            "def calculate_discount(price, rate):\n"
            "    return price - (price * rate)\n"
        )

        agent = ReproductionAgent(sb)
        # Test that passes immediately
        test_script = (
            "from calculator import calculate_discount\n"
            "def test_discount():\n"
            "    assert calculate_discount(100, 0.10) == 90.0\n"
        )

        res = agent.run_reproduction_gate(
            issue_title="Bogus issue report",
            issue_body="Discount does not work",
            generated_test_code=test_script,
        )

        # RED gate must BLOCK because the test passed (could not reproduce)
        assert res.reproduced is False
        assert "RED GATE BLOCKED" in res.error_message


def test_red_gate_rejects_syntax_error():
    with Sandbox() as sb:
        sb.write_file("calculator.py", "def f(): pass\n")
        agent = ReproductionAgent(sb)
        broken_syntax_test = "def test_syntax_err(:\n    assert True\n"

        res = agent.run_reproduction_gate(
            issue_title="Syntax error test",
            issue_body="Broken test",
            generated_test_code=broken_syntax_test,
        )

        assert res.reproduced is False
        assert "SyntaxError" in res.error_message

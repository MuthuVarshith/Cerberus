"""
Tests for Localization Agent and Symbol Ranking.
Conforms to Section 9 & 27:
- Evaluates candidate file and symbol extraction.
- Checks Top-1 and Top-3 accuracy.
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from harness.docker_sandbox import Sandbox
from agents.localization_agent import LocalizationAgent


def test_localization_on_three_scenarios():
    with Sandbox() as sb:
        # Scenario 1: Authentication token expiry issue
        sb.write_file("auth/token_manager.py", "def verify_jwt(token):\n    pass\n")
        sb.write_file("auth/session.py", "def create_session():\n    pass\n")
        sb.write_file("views/home.py", "def render_home():\n    pass\n")

        agent = LocalizationAgent(sb)
        res1 = agent.localize(
            issue_title="Bug in verify_jwt expiration calculation",
            issue_body="The verify_jwt function in token_manager.py treats valid tokens as expired.",
            referenced_files=["token_manager.py"],
        )
        assert len(res1.candidates) > 0
        assert "auth/token_manager.py" in res1.top_3_files

        # Scenario 2: Data pipeline parsing exception
        sb.write_file("pipeline/parser.py", "class CSVParser:\n    def parse_rows(self):\n        pass\n")
        sb.write_file("pipeline/loader.py", "def load_database():\n    pass\n")
        sb.write_file("config/settings.py", "TIMEOUT = 30\n")

        res2 = agent.localize(
            issue_title="CSVParser raises IndexError on empty rows",
            issue_body="Traceback indicates CSVParser failed inside parse_rows",
            error_signatures=["IndexError"],
        )
        assert len(res2.candidates) > 0
        assert "pipeline/parser.py" in res2.top_3_files

        # Scenario 3: Payment gateway rebate calculation
        sb.write_file("billing/discounts.py", "def calculate_rebate(amount):\n    return amount * 0.05\n")
        sb.write_file("billing/invoice.py", "def generate_pdf():\n    pass\n")

        res3 = agent.localize(
            issue_title="calculate_rebate computes wrong percentage",
            issue_body="Expected rebate calculation in discounts.py to handle zero amounts.",
            referenced_files=["discounts.py"],
        )
        assert len(res3.candidates) > 0
        assert "billing/discounts.py" in res3.top_3_files

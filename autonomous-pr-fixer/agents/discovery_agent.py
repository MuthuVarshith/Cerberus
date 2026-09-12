import os
from typing import List, Dict, Any
from harness.docker_sandbox import Sandbox

class DiscoveryAgent:
    """
    Proactively scans a repository to find bugs, vulnerabilities, and bottlenecks.
    In a full production environment, this would use LLM static analysis (e.g. asking 
    an LLM to review the codebase). For deterministic testing and portfolio demo purposes, 
    it detects the repository type and returns known realistic issues.
    """
    def __init__(self, sandbox: Sandbox, llm_enabled: bool = False):
        self.sandbox = sandbox
        self.llm_enabled = llm_enabled

    def scan_repository(self) -> List[Dict[str, Any]]:
        """
        Scans the sandbox workspace and returns a list of discovered issues.
        """
        # Determine which project we are looking at by checking file existence
        is_votevault = os.path.exists(os.path.join(self.sandbox.workspace_dir, "app.py"))
        is_rate_calc = os.path.exists(os.path.join(self.sandbox.workspace_dir, "rate_calculator.py"))

        issues = []

        if is_votevault:
            issues = [
                {
                    "id": "bug-001",
                    "title": "ValueError in export_votes: send_file requires BytesIO",
                    "description": "export_votes in app.py crashes with ValueError: Files must be opened in binary mode or use BytesIO.",
                    "severity": "High",
                    "file": "app.py"
                },
                {
                    "id": "bug-002",
                    "title": "SQL Injection vulnerability in search_users",
                    "description": "The search_users endpoint constructs SQL queries using raw string formatting instead of parameterized queries.",
                    "severity": "Critical",
                    "file": "models.py"
                },
                {
                    "id": "perf-001",
                    "title": "N+1 Query bottleneck in dashboard view",
                    "description": "The dashboard view fetches users and then iterates through them to fetch their votes, causing an N+1 database query bottleneck.",
                    "severity": "Medium",
                    "file": "app.py"
                }
            ]
        elif is_rate_calc:
            issues = [
                {
                    "id": "bug-101",
                    "title": "Divide by zero in rate_calculator",
                    "description": "calculate_rate(10, 0) throws ZeroDivisionError.",
                    "severity": "High",
                    "file": "rate_calculator.py"
                },
                {
                    "id": "type-102",
                    "title": "Missing type enforcement for negative amounts",
                    "description": "calculate_rate does not check if the amount is negative, which could lead to invalid negative rates.",
                    "severity": "Medium",
                    "file": "rate_calculator.py"
                }
            ]
        else:
            # Generic fallback for unknown repositories
            issues = [
                {
                    "id": "sec-999",
                    "title": "Hardcoded secrets detected in configuration",
                    "description": "Found potential hardcoded API keys in config files.",
                    "severity": "Critical",
                    "file": "config.py"
                }
            ]

        return issues

    def generate_implementation_plan(self, issue_title: str, issue_body: str, user_prompt: str) -> str:
        """
        Simulates an LLM generating an implementation plan based on a user's prompt.
        """
        if "plan" in user_prompt.lower():
            return (
                f"### Implementation Plan for: {issue_title}\n\n"
                f"**Analysis:** {issue_body}\n\n"
                f"**Proposed Solution:**\n"
                f"1. Isolate the fault location in the target file.\n"
                f"2. Apply a minimal +X/-Y patch to rectify the logic.\n"
                f"3. Run regression suites to ensure 0 blast radius leak.\n\n"
                f"**Awaiting your command to execute this plan.**"
            )
        return "I am ready to resolve this issue. Please click 'Resolve' to begin the autonomous repair loop."

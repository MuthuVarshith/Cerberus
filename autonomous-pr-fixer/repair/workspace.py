"""The agent's workspace: a Cerberus Sandbox on the base commit, dependencies installed, reproduction test in place.

This reuses the gate's own sandbox and setup detection; there is no second sandbox. In the
default (Docker) isolation, setup is the only networked step and everything the agent runs
afterwards executes offline in the repair container.
"""
from __future__ import annotations

from typing import Optional

from agents.repo_setup_agent import RepoSetupAgent
from harness.docker_sandbox import Sandbox
from harness.repo_config import RepoConfigError, load_repo_config
from repair.tools.base import REPRO_TEST_PATH


class WorkspaceError(RuntimeError):
    pass


def open_workspace(repo_dir: str, reproduction_test: str, base_ref: Optional[str] = None,
                   isolation: Optional[str] = None, timeout_sec: int = 120) -> Sandbox:
    sb = Sandbox(base_dir=repo_dir, isolation=isolation, base_ref=base_ref, timeout_sec=timeout_sec)
    try:
        try:
            cfg = load_repo_config(sb.workspace_dir)
        except RepoConfigError as exc:
            raise WorkspaceError(f".cerberus.yml: {exc}") from None
        plan = RepoSetupAgent(sb).detect()
        commands = cfg.setup if cfg.setup is not None else plan.install_commands
        failed = [r for r in sb.run_setup(commands, timeout=cfg.budgets.command_timeout_seconds) if r.exit_code != 0]
        if failed:
            raise WorkspaceError(f"Dependency installation failed: {failed[0].output[-500:]}")
        sb.ensure_harness_dir()
        sb.write_file(REPRO_TEST_PATH, reproduction_test)
        return sb
    except BaseException:
        sb.destroy()
        raise

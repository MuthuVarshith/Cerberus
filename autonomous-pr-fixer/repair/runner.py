"""One repair run end to end: workspace, gate adapter, agent, artifacts."""
from __future__ import annotations

import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from typing import Optional

from harness.docker_sandbox import SandboxError
from repair.agent import Printer, RepairAgent
from repair.config import RepairConfig
from repair.llm.provider import ChatModel, create_model
from repair.observability.langfuse import Tracer, create_tracer
from repair.state import FinalStatus, RepairState
from repair.verification.adapter import CerberusGate
from repair.workspace import WorkspaceError, open_workspace

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts", "repair")


@dataclass
class RepairTask:
    repo_dir: str
    issue_id: str
    title: str
    body: str
    reproduction_test: str
    base_ref: Optional[str] = None
    issue_number: int = 1


def _head(repo_dir: str, ref: Optional[str]) -> str:
    res = subprocess.run(["git", "rev-parse", ref or "HEAD"], cwd=repo_dir, capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else ""


def run_repair(task: RepairTask, config: RepairConfig, model: Optional[ChatModel] = None,
               tracer: Optional[Tracer] = None, run_id: Optional[str] = None, out_root: str = DEFAULT_OUT,
               printer: Optional[Printer] = None) -> RepairState:
    run_id = run_id or f"repair_{uuid.uuid4().hex[:10]}"
    out_dir = os.path.join(out_root, run_id)
    state = RepairState(run_id=run_id, repository=os.path.abspath(task.repo_dir), commit_sha=_head(task.repo_dir, task.base_ref),
                        issue_id=task.issue_id, issue_title=task.title, issue_description=task.body,
                        max_iterations=config.max_attempts, model=config.model)
    if model is None:
        model = create_model(config.provider, config.model, config.effort, config.server_fallbacks, config.min_seconds_between_calls)  # raises LLMError without a key
    try:
        sandbox = open_workspace(task.repo_dir, task.reproduction_test, base_ref=task.base_ref,
                                 isolation=config.isolation, timeout_sec=config.tool_timeout_seconds)
    except (WorkspaceError, SandboxError) as exc:
        state.final_status = FinalStatus.SETUP_FAILURE
        state.final_message = str(exc)[:500]
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "agent_run.json"), "w", encoding="utf-8") as f:
            f.write(state.model_dump_json(indent=2))
        return state
    gate = CerberusGate(repo_dir=task.repo_dir, issue_number=task.issue_number, issue_title=task.title,
                        issue_body=task.body, reproduction_test=task.reproduction_test, base_ref=task.base_ref,
                        artifacts_dir=os.path.join(out_dir, "gate"), isolation=config.isolation)
    # Structured JSON log lines for this run go to <out_dir>/agent.log only.
    repair_logger = logging.getLogger("cerberus.repair")
    repair_logger.setLevel(logging.INFO)
    repair_logger.propagate = False
    os.makedirs(out_dir, exist_ok=True)
    handler = logging.FileHandler(os.path.join(out_dir, "agent.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    repair_logger.addHandler(handler)
    try:
        agent = RepairAgent(model, gate, sandbox, config, tracer=tracer or create_tracer(),
                            printer=printer, out_dir=out_dir)
        return agent.run(state, task.reproduction_test)
    finally:
        repair_logger.removeHandler(handler)
        handler.close()
        sandbox.destroy()

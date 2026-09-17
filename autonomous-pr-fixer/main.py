"""
CLI entrypoint for Cerberus, a verification gate for bug-fix patches.

Pipeline:
  TRIAGE -> SETUP (networked, before any repair command) -> RED -> BASELINE
  -> LOCALIZATION -> PATCH LOOP (GREEN) -> REGRESSION -> SCOPE -> ADMISSION

Every run ends in exactly one terminal state: ADMITTED, REFUSED (with a
RefusalCode naming the gate) or ERROR. The pipeline never invents its own
inputs: a reproduction test comes from the caller or a model, a patch from the
caller's patch generator or a model. Without them the run is refused.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from harness.docker_sandbox import ISOLATION_HOST_UNSAFE, Sandbox, SandboxError, resolve_isolation
from harness.diff_utils import DiffUtils
from harness.admission_controller import AdmissionController, AdmissionDecision
from harness.pipeline_state import PipelineState, PipelineStateMachine, RefusalCode
from harness.config import load_config
from harness.logger import log_event
from harness.repo_config import RepoConfig, RepoConfigError, load_repo_config
from harness.run_artifact import write_run_artifact
from harness.scope_gate import ScopeReport, analyze_scope, split_test_changes
from agents.triage_agent import TriageAgent
from agents.reproduction_agent import ReproductionAgent, ReproductionResult
from agents.localization_agent import LocalizationAgent
from agents.patch_agent import PatchAgent, PatchLoopResult
from agents.regression_agent import RegressionAgent, RegressionReport
from agents.repo_setup_agent import RepoSetupAgent
from agents.llm_patch_generator import LLMPatchGenerator, has_api_key
from agents.llm_reproduction_synthesizer import LLMReproductionSynthesizer
from agents.patch_sources import (
    CLAUDE_CODE_PRESET,
    DiffPatchSource,
    ExternalAgentPatchSource,
    LLMPatchSource,
    PatchRequest,
    PatchSource,
    PatchSourceError,
    generator_for,
)
from github.pr_publisher import PRPublisher

PatchGenerator = Callable[[int, str], str]


def _parse_referenced_file(issue_body: str, workspace_dir: str) -> Optional[str]:
    match = re.search(r"\bat\s+([\w./\\-]+)(?::\d+)?", issue_body)
    if not match:
        return None
    parsed_path = match.group(1).replace("\\", "/").split(":")[0]
    # Issue text is untrusted: the referenced path must resolve inside the workspace.
    workspace = os.path.abspath(workspace_dir)
    candidate = os.path.abspath(os.path.join(workspace, parsed_path))
    if os.path.commonpath([workspace, candidate]) != workspace:
        return None
    if os.path.isfile(candidate):
        return os.path.relpath(candidate, workspace).replace("\\", "/")
    return None


def _llm_patching_enabled(explicit: Optional[bool] = None) -> bool:
    """Whether stage 5 should ask a model for the patch. Off unless asked for."""
    if explicit is not None:
        return explicit
    return os.environ.get("CERBERUS_USE_LLM", "").lower() in ("1", "true", "yes")


def _llm_repro_enabled(explicit: Optional[bool] = None) -> bool:
    """Whether stage 2 should synthesize reproduction tests using a model."""
    if explicit is not None:
        return explicit
    return os.environ.get("CERBERUS_USE_LLM_REPRO", "").lower() in ("1", "true", "yes")


class _RunRecorder:
    """Collects what happened in a run and writes exactly one artifact at the end."""

    def __init__(self, run_id: str, issue_number: int, issue_title: str, mode: str, artifacts_dir: str):
        self.run_id = run_id
        self.issue_number = issue_number
        self.issue_title = issue_title
        self.mode = mode
        self.artifacts_dir = artifacts_dir
        self.sm = PipelineStateMachine()
        self.base_commit_sha = "unknown"
        self.sandbox_info: Dict[str, Any] = {}
        self.repro_res: Optional[ReproductionResult] = None
        self.baseline_info: Dict[str, Any] = {}
        self.patch_res: Optional[PatchLoopResult] = None
        self.regr_res: Optional[RegressionReport] = None
        self.scope_res: Optional[ScopeReport] = None
        self.decision: Optional[AdmissionDecision] = None
        self.diff_text = ""
        self.diff_hash = ""
        self.pr_info: Dict[str, Any] = {}
        self.github_enabled = False
        self.refusal: Dict[str, Any] = {}
        self.repo_config: Optional[RepoConfig] = None
        self.patch_source: Dict[str, Any] = {}
        self.artifact_path: Optional[str] = None
        self.written = False

    def refuse(self, code: RefusalCode, stage: str, reason: str) -> bool:
        return self._stop(PipelineState.REFUSED, stage, reason, code)

    def error(self, stage: str, reason: str) -> bool:
        return self._stop(PipelineState.ERROR, stage, reason, None)

    def _stop(self, state: PipelineState, stage: str, reason: str, code: Optional[RefusalCode]) -> bool:
        if not self.sm.is_terminal:
            self.sm.transition(state)
        self.refusal = {"code": code.value if code else None, "stage": stage, "message": reason}
        log_event(self.run_id, stage, state.value, self.issue_number, reason)
        print(f"  [{state.value}] {reason}")
        self.finish(reason)
        _print_summary(
            mode=self.mode,
            issue_number=self.issue_number,
            final_state=self.sm.state.value,
            reason=f"{code.value}: {reason}" if code else reason,
            artifact_path=self.artifact_path or "N/A",
        )
        return False

    def finish(self, reason: Optional[str]) -> None:
        scope = self.scope_res
        red = self.repro_res
        self.artifact_path = write_run_artifact(
            run_id=self.run_id,
            issue_number=self.issue_number,
            issue_title=self.issue_title,
            pipeline_state_history=[s.value for s in self.sm.history],
            admission_decision=self.decision.to_dict() if self.decision else {},
            regression_results=self.regr_res.to_dict() if self.regr_res else {},
            blast_radius=scope.to_dict() if scope else {},
            patch_attempts=self.patch_res.total_attempts if self.patch_res else 0,
            reached_green=bool(self.patch_res and self.patch_res.reached_green),
            base_commit_sha=self.base_commit_sha,
            artifacts_dir=self.artifacts_dir,
            execution_mode=self.mode,
            patch_changed=bool(self.decision and self.decision.patch_changed),
            diff_hash=self.diff_hash,
            diff_files=scope.changed_files if scope else [],
            diff_lines_added=scope.lines_added if scope else 0,
            diff_lines_deleted=scope.lines_deleted if scope else 0,
            patch_verified_against_test=bool(self.patch_res and self.patch_res.reached_green),
            github_integration_enabled=self.github_enabled,
            pr_created=(self.pr_info.get("status") == "published"),
            pr_url=self.pr_info.get("pr_url"),
            admission_rejection_reason=reason,
            final_state=self.sm.state.value,
            reproduction_test_code=red.test_code if red else "",
            reproduction_output=red.raw_output if red else "",
            diff_text=self.diff_text,
            sections={
                "refusal": self.refusal or None,
                "sandbox": self.sandbox_info,
                "red_gate": (
                    {
                        "reproduced": red.reproduced,
                        "runs": red.runs,
                        "failing_test_ids": red.failing_test_ids,
                        "failure_types": red.failure_types,
                        "refusal_code": red.refusal_code,
                        "message": red.error_message,
                    }
                    if red
                    else None
                ),
                "baseline": self.baseline_info or None,
                "patch_history": [
                    {
                        "attempt": h.attempt,
                        "applied": h.applied_successfully,
                        "reproduction_test_passed": h.reproduction_test_passed,
                        "status": h.structured_failure.status if h.structured_failure else "GREEN",
                        "detail": (h.structured_failure.traceback or "")[-1000:] if h.structured_failure else "",
                    }
                    for h in (self.patch_res.history if self.patch_res else [])
                ],
                "repo_config": self.repo_config.to_dict() if self.repo_config else None,
                "patch_source": self.patch_source or None,
            },
        )
        self.written = True


def run_pipeline(
    repo_dir: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    dry_run: bool = True,
    mode: str = "local",
    use_llm: Optional[bool] = None,
    use_llm_repro: Optional[bool] = None,
    fork_owner: Optional[str] = None,
    run_id: Optional[str] = None,
    repro_test_code: Optional[str] = None,
    patch_generator: Optional[PatchGenerator] = None,
    sandbox_isolation: Optional[str] = None,
    patch_source: Optional[PatchSource] = None,
    base_ref: Optional[str] = None,
    verify_change: bool = False,
) -> bool:
    """Run the verification pipeline for one issue. Returns True only when admitted.

    Args:
        repro_test_code: a caller-supplied reproduction test, held to the RED gate.
        patch_generator: a caller-supplied `(attempt, feedback) -> diff` callable.
        sandbox_isolation: "docker" (default) or "host-unsafe" (trusted fixtures only).
        patch_source: a PatchSource (pre-written diff, model, or external agent).
        base_ref: the commit to verify against; defaults to the repository's HEAD.
        verify_change: the patch is an existing change (e.g. a PR) rather than a
            repair; scope then defaults to any file unless .cerberus.yml restricts it.
    """
    run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"
    cfg = load_config()
    rec = _RunRecorder(run_id, issue_number, issue_title, mode, cfg.run_artifacts_dir)

    print("=" * 60)
    print("Cerberus: verification gate for bug-fix patches")
    print(f"Run ID: {run_id} | Issue #{issue_number} | Mode: {mode.upper()}")
    print("=" * 60)

    try:
        isolation = resolve_isolation(sandbox_isolation)
    except SandboxError as exc:
        return rec.error("CONFIG", str(exc))
    rec.sandbox_info = {"isolation": isolation}
    if mode == "github" and isolation == ISOLATION_HOST_UNSAFE:
        return rec.error("CONFIG", "GitHub mode requires the Docker sandbox; host-unsafe execution can never publish.")
    if mode == "github" and not dry_run and not cfg.github_token:
        return rec.error("CONFIG", "GitHub mode requested but GITHUB_TOKEN is not configured.")

    try:
        try:
            sb = Sandbox(base_dir=repo_dir, timeout_sec=cfg.sandbox_timeout_seconds, isolation=isolation, base_ref=base_ref)
        except SandboxError as exc:
            return rec.error("SANDBOX", str(exc))
        with sb:
            rec.base_commit_sha = sb.base_commit or "unknown"
            rec.sandbox_info.update({
                "image": sb.image if sb.is_docker else None,
                "source_dirty": sb.source_dirty,
            })
            # Policy is read from the base commit before any repository code runs,
            # so a patch cannot loosen the rules it is judged by.
            try:
                rec.repo_config = load_repo_config(sb.workspace_dir)
            except RepoConfigError as exc:
                return rec.error("CONFIG", str(exc))
            try:
                return _run_in_sandbox(
                    sb, rec, cfg, rec.repo_config, repo_dir, issue_number, issue_title, issue_body, dry_run, mode,
                    use_llm, use_llm_repro, fork_owner, repro_test_code, patch_generator, patch_source, verify_change,
                )
            except PatchSourceError as exc:
                return rec.error("PATCH_SOURCE", str(exc))
    except Exception as exc:
        if not rec.sm.is_terminal:
            rec.sm.transition(PipelineState.ERROR)
            rec.refusal = {"code": None, "stage": "PIPELINE", "message": f"{type(exc).__name__}: {exc}"}
            log_event(run_id, "PIPELINE", "ERROR", issue_number, f"{type(exc).__name__}: {exc}")
            rec.finish(f"Unhandled error: {type(exc).__name__}: {exc}")
        raise
    finally:
        if not rec.sm.is_terminal:
            # A code path returned without deciding. That is a bug, recorded as one.
            rec.sm.transition(PipelineState.ERROR)
            rec.finish("Pipeline exited without reaching a terminal state.")


def _run_in_sandbox(
    sb: Sandbox,
    rec: _RunRecorder,
    cfg: Any,
    repo_cfg: RepoConfig,
    repo_dir: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    dry_run: bool,
    mode: str,
    use_llm: Optional[bool],
    use_llm_repro: Optional[bool],
    fork_owner: Optional[str],
    repro_test_code: Optional[str],
    patch_generator: Optional[PatchGenerator],
    patch_source: Optional[PatchSource],
    verify_change: bool,
) -> bool:
    sm = rec.sm
    run_id = rec.run_id
    budgets = repo_cfg.budgets
    if repo_cfg.present:
        print("  [OK] Using .cerberus.yml from the base commit")

    if sb.source_dirty:
        print("  [WARN] The source repository has uncommitted changes; only the committed HEAD is verified.")

    # TRIAGE (reads files only; nothing executes in the sandbox yet)
    print("\n[1/9] TRIAGE")
    sm.transition(PipelineState.TRIAGE_PENDING)
    triage_report = TriageAgent(sb).triage_issue(issue_number, issue_title, issue_body)
    sm.transition(PipelineState.TRIAGED)
    print(f"  [OK] Base commit: {rec.base_commit_sha[:12]}")
    print(f"  [OK] Error signatures: {', '.join(triage_report.error_signatures) or 'none found'}")

    target_code_file = _parse_referenced_file(issue_body, sb.workspace_dir)
    if not target_code_file:
        existing = [f for f in triage_report.referenced_files if os.path.isfile(os.path.join(sb.workspace_dir, f))]
        target_code_file = existing[0] if existing else None

    # SETUP (the only phase with network access in Docker mode)
    print("\n[2/9] SETUP")
    sm.transition(PipelineState.SETUP_PENDING)
    setup_plan = RepoSetupAgent(sb).detect()
    install_commands = repo_cfg.setup if repo_cfg.setup is not None else setup_plan.install_commands
    test_command = repo_cfg.test_command or setup_plan.test_command
    print(f"  [OK] {'.cerberus.yml' if repo_cfg.setup is not None or repo_cfg.test_command else setup_plan.reason}")
    for cmd in install_commands:
        print(f"  -> {cmd}")
    results = sb.run_setup(install_commands, timeout=budgets.command_timeout_seconds)
    failed = [r for r in results if r.exit_code != 0]
    if failed:
        return rec.error("SETUP", f"Dependency installation failed: {failed[0].output[-1000:]}")
    rec.sandbox_info["setup_commands"] = install_commands
    rec.sandbox_info["setup_skipped"] = sb.setup_skipped
    if sb.setup_skipped:
        print("  [WARN] host-unsafe sandbox: dependency installation skipped")
    sm.transition(PipelineState.SETUP_COMPLETE)

    # REPRODUCTION + RED
    print("\n[3/9] REPRODUCTION / RED GATE")
    sm.transition(PipelineState.REPRODUCTION_PENDING)
    repro_agent = ReproductionAgent(
        sb, error_signatures=triage_report.error_signatures,
        runs=budgets.red_runs, timeout=budgets.command_timeout_seconds,
    )
    repro_tokens = 0
    if repro_test_code:
        print("  -> Using caller-supplied reproduction test")
        rec.repro_res = repro_agent.run_reproduction_gate(issue_title, issue_body, repro_test_code)
    elif _llm_repro_enabled(use_llm_repro):
        if not has_api_key():
            return rec.refuse(
                RefusalCode.NO_REPRODUCTION_TEST, "REPRODUCTION",
                "Model reproduction was requested but no ANTHROPIC_API_KEY/OPENAI_API_KEY/GEMINI_API_KEY is set.",
            )
        if not target_code_file:
            return rec.refuse(
                RefusalCode.NO_REPRODUCTION_TEST, "REPRODUCTION",
                "Model reproduction needs a concrete source file referenced in the issue (e.g. 'at path/to/file.py:12').",
            )
        print(f"  -> Model-generated reproduction test, boundary: {target_code_file}")
        repro_synth = LLMReproductionSynthesizer(
            sandbox=sb, candidate_files=[target_code_file], issue_title=issue_title, issue_body=issue_body,
        )
        print(f"  -> Model: {repro_synth.model}")
        rec.repro_res = repro_synth.synthesize_and_verify(max_attempts=3)
        repro_tokens = repro_synth.usage.total_tokens
    else:
        return rec.refuse(
            RefusalCode.NO_REPRODUCTION_TEST, "REPRODUCTION",
            "No reproduction test is available: supply one (--repro-test) or enable model reproduction (--use-llm-repro).",
        )

    if not rec.repro_res.reproduced:
        code = RefusalCode(rec.repro_res.refusal_code or RefusalCode.RED_NOT_FAILING.value)
        return rec.refuse(code, "RED_GATE", rec.repro_res.error_message)
    sm.transition(PipelineState.REPRODUCED_RED)
    log_event(run_id, "RED_GATE", "PASS", issue_number, "RED reproduction confirmed")
    print(f"  [OK] RED confirmed in {rec.repro_res.runs}/{rec.repro_res.runs} runs: " + ", ".join(
        f"{tid} ({rec.repro_res.failure_types.get(tid) or '?'})" for tid in rec.repro_res.failing_test_ids
    ))

    # BASELINE
    print("\n[4/9] BASELINE")
    sm.transition(PipelineState.BASELINE_PENDING)
    if not test_command:
        return rec.refuse(RefusalCode.NO_TEST_COMMAND, "BASELINE", "No test command was detected for this repository.")
    regr_agent = RegressionAgent(
        sb, timeout=budgets.command_timeout_seconds,
        exclude=repo_cfg.test_exclude, configured_report=repo_cfg.test_report,
    )
    baseline = regr_agent.record_baseline(test_command)
    if baseline.report is None:
        return rec.refuse(RefusalCode.BASELINE_UNVERIFIABLE, "BASELINE", f"Baseline test results are unusable: {baseline.error}")
    rec.baseline_info = {
        "test_command": test_command,
        "excluded_patterns": repo_cfg.test_exclude,
        "total": baseline.report.total,
        "failing": baseline.report.ids_with("failed", "error"),
    }
    sm.transition(PipelineState.BASELINE_RECORDED)
    print(f"  [OK] {baseline.report.total} tests, {len(rec.baseline_info['failing'])} already failing")

    # LOCALIZATION
    print("\n[5/9] LOCALIZATION")
    sm.transition(PipelineState.LOCALIZATION_PENDING)
    loc_res = LocalizationAgent(sb).localize(
        issue_title=issue_title,
        issue_body=issue_body,
        error_signatures=triage_report.error_signatures,
        referenced_files=triage_report.referenced_files,
        max_candidates=3,
    )
    top_matches = [c for c in loc_res.candidates if target_code_file and target_code_file in c.file]
    top_cand = top_matches[0] if top_matches else (loc_res.candidates[0] if loc_res.candidates else None)
    top_file = top_cand.file if top_cand else target_code_file
    allowed_patterns = list(repo_cfg.scope.allowed_paths) or (["*"] if verify_change else [])
    allowed_files = [top_file] if top_file and not verify_change else []
    if not allowed_files and not allowed_patterns:
        return rec.refuse(RefusalCode.NOT_LOCALIZED, "LOCALIZATION", "Could not localize a repair target file.")
    sm.transition(PipelineState.LOCALIZED)
    print(f"  [OK] Allowed scope: {', '.join(allowed_files + [f'pattern {p}' for p in allowed_patterns])}")

    # PATCH LOOP + GREEN
    print("\n[6/9] PATCH LOOP / GREEN GATE")
    sm.transition(PipelineState.PATCH_PENDING)
    generator: Optional[PatchGenerator] = patch_generator
    llm_generator: Optional[LLMPatchGenerator] = None
    request = PatchRequest(
        attempt=1, feedback="", issue_number=issue_number, issue_title=issue_title, issue_body=issue_body,
        allowed_files=allowed_files, allowed_patterns=allowed_patterns,
        reproduction_test_code=rec.repro_res.test_code, source_repo_dir=repo_dir, base_commit=rec.base_commit_sha,
    )
    if generator is not None:
        rec.patch_source = {"name": "caller-supplied generator"}
        print("  -> Using caller-supplied patch generator")
    elif patch_source is not None:
        generator = generator_for(patch_source, request)
        rec.patch_source = {"name": patch_source.name}
        print(f"  -> Patch source: {patch_source.name}")
    elif _llm_patching_enabled(use_llm):
        if not has_api_key():
            return rec.refuse(
                RefusalCode.NO_PATCH_SOURCE, "PATCH_LOOP",
                "Model patching was requested but no ANTHROPIC_API_KEY/OPENAI_API_KEY/GEMINI_API_KEY is set.",
            )
        if not top_file:
            return rec.refuse(RefusalCode.NOT_LOCALIZED, "PATCH_LOOP", "Model patching needs a localized file to show the model.")
        llm_generator = LLMPatchGenerator(sandbox=sb, candidate_files=[top_file], issue_title=issue_title, issue_body=issue_body)
        source = LLMPatchSource(llm_generator)
        generator = generator_for(source, request)
        rec.patch_source = {"name": source.name}
        print(f"  -> Model-generated patches, boundary: {top_file} (model {llm_generator.model})")
    else:
        return rec.refuse(
            RefusalCode.NO_PATCH_SOURCE, "PATCH_LOOP",
            "No patch source is configured: supply a patch (--patch), a change (--base/--head), "
            "an external agent (--agent), or enable model patching (--use-llm).",
        )

    loop_res = PatchAgent(
        sb, max_attempts=budgets.patch_attempts, max_lines_changed=repo_cfg.scope.max_lines,
    ).run_patch_loop(allowed_files, generator, verifier=repro_agent.green_verifier(rec.repro_res))
    if patch_source is not None and getattr(patch_source, "transcripts", None):
        rec.patch_source["transcripts"] = patch_source.transcripts
    print(f"  -> {loop_res.total_attempts} attempt(s)")
    token_usage: Optional[Dict[str, int]] = None
    if llm_generator is not None:
        token_usage = {
            "prompt_tokens": llm_generator.usage.prompt_tokens,
            "completion_tokens": llm_generator.usage.completion_tokens,
            "total_tokens": llm_generator.usage.total_tokens + repro_tokens,
        }
    elif repro_tokens > 0:
        token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": repro_tokens}

    changes = DiffUtils.workspace_changes(sb)
    rec.diff_text = changes.diff_text
    rec.diff_hash = DiffUtils.compute_diff_hash(changes.diff_text)
    rec.patch_res = PatchLoopResult(
        reached_green=loop_res.reached_green,
        total_attempts=loop_res.total_attempts,
        winning_diff=changes.diff_text,
        history=loop_res.history,
        total_lines_changed=changes.total_lines,
        final_changed_files=changes.files,
        diff_hash=rec.diff_hash,
        is_empty_diff=changes.is_empty,
    )
    if not loop_res.reached_green:
        failures = [h.structured_failure for h in loop_res.history
                    if h.structured_failure and h.structured_failure.status != "ABORT_DUPLICATE_DIFF"]
        last = f" Last attempt: {failures[-1].status}: {failures[-1].traceback.strip()[-300:]}" if failures else ""
        return rec.refuse(
            RefusalCode.GREEN_NOT_REACHED, "PATCH_LOOP",
            f"No candidate patch made the reproduction test pass after {loop_res.total_attempts} attempt(s).{last}",
        )
    green = repro_agent.verify_green(rec.repro_res, runs=budgets.green_runs)
    if not green.passed:
        rec.patch_res.reached_green = False
        return rec.refuse(RefusalCode(green.refusal_code), "GREEN_GATE", green.message)
    sm.transition(PipelineState.PATCH_GREEN)
    print(f"  [OK] GREEN confirmed in {budgets.green_runs}/{budgets.green_runs} runs")

    # REGRESSION
    print("\n[7/9] REGRESSION")
    sm.transition(PipelineState.REGRESSION_PENDING)
    # Existing test files the patch only extended run in their base form (see scope_gate).
    base_test_files = [] if repo_cfg.scope.allow_test_modifications else split_test_changes(changes)[1]
    if base_test_files:
        print(f"  -> running existing test files extended by the patch in their base form: {', '.join(base_test_files)}")
    rec.regr_res = regr_agent.run_regression_suite(test_command, baseline.report, base_test_files=base_test_files)
    r = rec.regr_res
    if not r.results_parsed:
        return rec.refuse(RefusalCode.REGRESSION_UNVERIFIABLE, "REGRESSION", f"Test results after the patch are unusable: {r.error_message}")
    print(f"  -> {r.passed_count}/{r.total_tests} passing; newly failing {len(r.newly_failing)}, "
          f"pre-existing {len(r.preexisting_failures)}, flaky {len(r.flaky_tests)}")
    if not r.regression_free:
        detail = ", ".join(r.newly_failing + [f"{t} (no longer runs)" for t in r.missing_tests])
        return rec.refuse(RefusalCode.REGRESSION, "REGRESSION", f"The patch introduced regressions: {detail}")
    sm.transition(PipelineState.REGRESSION_CLEAN)

    # SCOPE
    print("\n[8/9] SCOPE")
    sm.transition(PipelineState.SCOPE_PENDING)
    rec.scope_res = analyze_scope(
        sb,
        allowed_files=allowed_files,
        allowed_patterns=allowed_patterns,
        max_files=repo_cfg.scope.max_files,
        max_lines=repo_cfg.scope.max_lines,
        allow_test_modifications=repo_cfg.scope.allow_test_modifications,
        changes=changes,
    )
    s = rec.scope_res
    print(f"  -> files {s.changed_files} (+{s.lines_added}/-{s.lines_deleted}); symbols {s.changed_symbols or 'none identified'}")
    if not s.is_acceptable:
        return rec.refuse(RefusalCode.SCOPE_VIOLATION, "SCOPE", s.violation_reason or "Scope violation")
    sm.transition(PipelineState.SCOPE_ACCEPTABLE)

    # ADMISSION
    print("\n[9/9] ADMISSION")
    sm.transition(PipelineState.ADMISSION_PENDING)
    rec.decision = decision = AdmissionController.evaluate(rec.patch_res, rec.regr_res, rec.scope_res)
    for reason in decision.reasons:
        print(f"  {reason}")
    if not decision.approved:
        return rec.refuse(RefusalCode(decision.rejection_state), "ADMISSION", decision.rejection_summary)
    sm.transition(PipelineState.ADMITTED)
    log_event(run_id, "ADMISSION", "ADMITTED", issue_number, "All gates passed")

    # Publication is gated on admission, and only a Docker-verified run may publish.
    publish = mode == "github" and not dry_run and sb.is_docker
    rec.github_enabled = mode == "github" and bool(cfg.github_token)
    publisher = PRPublisher(github_token=cfg.github_token)
    pr_body = publisher.build_evidence_report(
        issue_number=issue_number,
        issue_title=issue_title,
        reproduction_res=rec.repro_res,
        patch_res=rec.patch_res,
        regression_res=rec.regr_res,
        scope_res=rec.scope_res,
        decision=decision,
        branch_name=triage_report.working_branch,
        token_usage=token_usage,
        base_commit=rec.base_commit_sha,
    )
    rec.pr_info = publisher.publish_pr(
        issue_number=issue_number,
        issue_title=issue_title,
        repo_slug=cfg.github_repo_slug or "org/repo",
        branch_name=triage_report.working_branch,
        pr_body=pr_body,
        dry_run=not publish,
        fork_owner=fork_owner,
        diff_text=rec.diff_text,
        source_repo_dir=repo_dir,
        base_commit=rec.base_commit_sha,
    )
    rec.finish(None)

    status = rec.pr_info.get("status")
    if status == "error":
        pr_display = f"PUBLISH FAILED: {rec.pr_info.get('error')}"
    elif status == "published":
        pr_display = rec.pr_info.get("pr_url") or "published"
    else:
        pr_display = rec.pr_info.get("display_url") or "NOT CREATED"

    _print_summary(
        mode=mode,
        issue_number=issue_number,
        final_state=sm.state.value,
        reason="All 4 verification gates passed.",
        artifact_path=rec.artifact_path or "N/A",
        attempts=rec.patch_res.total_attempts,
        patch_stat=f"+{s.lines_added}/-{s.lines_deleted} in {len(s.changed_files)} file(s), hash {rec.diff_hash[:12]}",
        pr_link=pr_display,
    )
    return True


def _print_summary(
    mode: str,
    issue_number: int,
    final_state: str,
    reason: str,
    artifact_path: str,
    attempts: Optional[int] = None,
    patch_stat: Optional[str] = None,
    pr_link: Optional[str] = None,
) -> None:
    print("\n========================================")
    print("RESULT")
    print("========================================")
    print(f"Mode: {mode.upper()}")
    print(f"Issue: #{issue_number}")
    print(f"Final state: {final_state}")
    if attempts is not None:
        print(f"Patch attempts: {attempts}")
    if patch_stat:
        print(f"Patch: {patch_stat}")
    if pr_link:
        print(f"PR: {pr_link}")
    print(f"Reason: {reason}")
    print(f"Run artifact: {artifact_path}")
    print("========================================\n")


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def resolve_change(repo_dir: str, base: str, head: str) -> tuple[str, str, str]:
    """Return (base_sha, head_sha, diff) for an existing change in the source repository.

    Runs git on the user's own repository only, with list arguments and with
    external diff drivers and textconv disabled.
    """
    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True, encoding="utf-8", errors="replace")

    shas = []
    for ref in (base, head):
        if ref.startswith("-"):
            raise ValueError(f"Invalid revision {ref!r}.")
        res = _git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        if res.returncode != 0:
            raise ValueError(f"{ref!r} does not name a commit in {repo_dir}.")
        shas.append(res.stdout.strip())
    diff = _git("-c", "core.autocrlf=false", "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", shas[0], shas[1])
    if diff.returncode != 0:
        raise ValueError(f"git diff failed: {diff.stderr.strip()}")
    if not diff.stdout.strip():
        raise ValueError(f"{base}..{head} contains no changes.")
    return shas[0], shas[1], diff.stdout


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cerberus: verification gate for bug-fix patches")
    parser.add_argument("--repo", default=".", help="Path to a local git repository")
    parser.add_argument("--issue", type=int, default=1, help="Issue number")
    parser.add_argument("--title", default="", help="Issue title")
    parser.add_argument("--body", default="", help="Issue body")
    parser.add_argument("--mode", choices=["local", "github"], default="local",
                        help="local never publishes; github may publish a draft PR when not --dry-run")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Do not push or open a PR")
    parser.add_argument("--repro-test", metavar="PATH", help="Reproduction test file to hold to the RED gate")
    parser.add_argument("--patch", metavar="PATH", help="Unified diff to verify as the candidate patch")
    parser.add_argument("--base", metavar="REF", help="Verify an existing change: base revision (use with --head)")
    parser.add_argument("--head", metavar="REF", help="Verify an existing change: head revision, e.g. a PR branch")
    parser.add_argument("--agent", choices=["claude-code"],
                        help="Use an external coding agent preset as the patch source (runs on the host, outside the sandbox)")
    parser.add_argument("--agent-command", metavar="CMD",
                        help="External agent command; the prompt is sent on stdin, {prompt_file} and {workdir} are substituted")
    parser.add_argument("--agent-timeout", type=int, default=1800, help="Seconds allowed per external agent attempt")
    parser.add_argument("--run-id", metavar="ID", help="Run identifier (letters, digits, _ and -); names the artifact directory")
    parser.add_argument("--use-llm", action="store_true", default=None,
                        help="Generate patches with a model (needs a provider API key). Also CERBERUS_USE_LLM=1.")
    parser.add_argument("--use-llm-repro", action="store_true", default=None,
                        help="Generate the reproduction test with a model. Also CERBERUS_USE_LLM_REPRO=1.")
    parser.add_argument("--demo", action="store_true", default=False,
                        help="Run the deterministic rate_calculator example (examples/rate_calculator).")
    parser.add_argument("--unsafe-local-sandbox", action="store_true", default=False,
                        help="Run repository code on this machine without container isolation. "
                             "Trusted local fixtures only; can never publish.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    isolation = ISOLATION_HOST_UNSAFE if args.unsafe_local_sandbox else None

    if args.demo:
        from examples.demo import prepare_rate_calculator_demo

        with tempfile.TemporaryDirectory(prefix="cerberus_demo_") as tmp:
            scenario = prepare_rate_calculator_demo(tmp)
            admitted = run_pipeline(
                repo_dir=scenario.repo_dir,
                issue_number=scenario.issue_number,
                issue_title=scenario.issue_title,
                issue_body=scenario.issue_body,
                dry_run=True,
                mode="local",
                repro_test_code=scenario.repro_test_code,
                patch_source=DiffPatchSource([scenario.patch_diff], name="demo fixture diff"),
                sandbox_isolation=isolation,
            )
        return 0 if admitted else 1

    if not args.title:
        print("[ERROR] --title is required (or use --demo).")
        return 2
    if args.run_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_id):
        print("[ERROR] --run-id may contain only letters, digits, '_' and '-'.")
        return 2

    chosen = [flag for flag, value in (
        ("--patch", args.patch), ("--head", args.head), ("--agent", args.agent),
        ("--agent-command", args.agent_command), ("--use-llm", args.use_llm),
    ) if value]
    if len(chosen) > 1:
        print(f"[ERROR] Choose one patch source; got {', '.join(chosen)}.")
        return 2
    if bool(args.base) != bool(args.head):
        print("[ERROR] --base and --head must be used together.")
        return 2

    patch_source: Optional[PatchSource] = None
    base_ref: Optional[str] = None
    if args.patch:
        patch_source = DiffPatchSource([_read_text(args.patch)], name=f"diff file {os.path.basename(args.patch)}")
    elif args.head:
        try:
            base_ref, head_sha, diff = resolve_change(args.repo, args.base, args.head)
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            return 2
        patch_source = DiffPatchSource([diff], name=f"change {base_ref[:12]}..{head_sha[:12]}")
    elif args.agent or args.agent_command:
        command = CLAUDE_CODE_PRESET if args.agent == "claude-code" else shlex.split(args.agent_command)
        patch_source = ExternalAgentPatchSource(command, name=args.agent or command[0], timeout=args.agent_timeout)

    admitted = run_pipeline(
        repo_dir=args.repo,
        issue_number=args.issue,
        issue_title=args.title,
        issue_body=args.body,
        dry_run=args.dry_run,
        mode=args.mode,
        use_llm=args.use_llm,
        use_llm_repro=args.use_llm_repro,
        repro_test_code=_read_text(args.repro_test) if args.repro_test else None,
        patch_source=patch_source,
        sandbox_isolation=isolation,
        base_ref=base_ref,
        verify_change=bool(args.head),
        run_id=args.run_id,
    )
    return 0 if admitted else 1


if __name__ == "__main__":
    sys.exit(main())

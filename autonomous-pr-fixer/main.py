"""
CLI entrypoint for Cerberus, a verification gate for bug-fix patches.

Pipeline stages:
  [1/8] TRIAGE
  [2/8] REPRODUCTION
  [3/8] RED GATE
  [4/8] LOCALIZATION
  [5/8] PATCH LOOP (GREEN gate)
  [6/8] REGRESSION
  [7/8] BLAST RADIUS
  [8/8] ADMISSION

The pipeline never invents its own inputs. A reproduction test comes from the
caller or from a model; a patch comes from the caller's patch generator or from
a model. When neither is available the run is refused, not improvised.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import uuid
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from harness.docker_sandbox import Sandbox
from harness.diff_utils import DiffUtils
from harness.admission_controller import AdmissionController, AdmissionDecision
from harness.pipeline_state import PipelineState, PipelineStateMachine
from harness.config import load_config
from harness.logger import log_event
from harness.run_artifact import write_run_artifact
from agents.triage_agent import TriageAgent
from agents.reproduction_agent import ReproductionAgent, ReproductionResult
from agents.localization_agent import LocalizationAgent
from agents.patch_agent import PatchAgent, PatchLoopResult
from agents.regression_agent import RegressionAgent, RegressionReport
from agents.repo_setup_agent import RepoSetupAgent
from agents.llm_patch_generator import LLMPatchGenerator, ScriptedPatchGenerator, has_api_key
from agents.llm_reproduction_synthesizer import LLMReproductionSynthesizer
from github.pr_publisher import PRPublisher

PatchGenerator = Callable[[int, str], str]


def _parse_referenced_file(issue_body: str, workspace_dir: str) -> Optional[str]:
    match = re.search(r"\bat\s+([\w./\\-]+)(?::\d+)?", issue_body)
    if not match:
        return None
    parsed_path = match.group(1).replace("\\", "/").split(":")[0]
    if os.path.exists(os.path.join(workspace_dir, parsed_path)):
        return parsed_path
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
    """Collects what happened in a run and writes exactly one artifact at the end.

    Every exit path goes through `finish`, so a run that stops early still leaves
    an audit record with its final state and the reason it stopped.
    """

    def __init__(self, run_id: str, issue_number: int, issue_title: str, mode: str, artifacts_dir: str):
        self.run_id = run_id
        self.issue_number = issue_number
        self.issue_title = issue_title
        self.mode = mode
        self.artifacts_dir = artifacts_dir
        self.sm = PipelineStateMachine()
        self.base_commit_sha = "unknown"
        self.repro_res: Optional[ReproductionResult] = None
        self.patch_res: Optional[PatchLoopResult] = None
        self.regr_res: Optional[RegressionReport] = None
        self.decision: Optional[AdmissionDecision] = None
        self.diff_text = ""
        self.diff_stats: Dict[str, Any] = {"files": [], "lines_added": 0, "lines_deleted": 0, "total_lines": 0}
        self.diff_hash = ""
        self.pr_info: Dict[str, Any] = {}
        self.github_enabled = False
        self.artifact_path: Optional[str] = None

    def fail(self, state: PipelineState, stage: str, reason: str) -> bool:
        """Move to a terminal rejection or error state and write the artifact."""
        if not self.sm.is_terminal:
            self.sm.transition(state)
        log_event(self.run_id, stage, "FAIL", self.issue_number, reason)
        print(f"  [FAIL] {reason}")
        self.finish(reason)
        _print_summary(
            mode=self.mode,
            issue_number=self.issue_number,
            status="PR BLOCKED",
            final_state=self.sm.state.value,
            reason=reason,
            artifact_path=self.artifact_path or "N/A",
        )
        return False

    def finish(self, reason: Optional[str]) -> None:
        blast = self.regr_res.blast_radius if self.regr_res else None
        self.artifact_path = write_run_artifact(
            run_id=self.run_id,
            issue_number=self.issue_number,
            issue_title=self.issue_title,
            pipeline_state_history=[s.value for s in self.sm.history],
            admission_decision=self.decision.to_dict() if self.decision else {},
            regression_results=(
                {
                    "passed": self.regr_res.passed_count,
                    "failed": self.regr_res.failed_count,
                    "total": self.regr_res.total_tests,
                }
                if self.regr_res
                else {}
            ),
            blast_radius=(
                {
                    "files_changed": blast.observed_files,
                    "lines_added": blast.lines_added,
                    "lines_deleted": blast.lines_deleted,
                    "is_acceptable": blast.is_acceptable,
                }
                if blast
                else {}
            ),
            patch_attempts=self.patch_res.total_attempts if self.patch_res else 0,
            reached_green=bool(self.patch_res and self.patch_res.reached_green),
            base_commit_sha=self.base_commit_sha,
            artifacts_dir=self.artifacts_dir,
            execution_mode=self.mode,
            patch_changed=bool(self.decision and self.decision.patch_changed),
            diff_hash=self.diff_hash,
            diff_files=self.diff_stats["files"],
            diff_lines_added=self.diff_stats["lines_added"],
            diff_lines_deleted=self.diff_stats["lines_deleted"],
            patch_verified_against_test=bool(self.patch_res and self.patch_res.reached_green),
            github_integration_enabled=self.github_enabled,
            pr_created=(self.pr_info.get("status") == "published"),
            pr_url=self.pr_info.get("pr_url"),
            admission_rejection_reason=reason,
            final_state=self.sm.state.value,
            reproduction_test_code=self.repro_res.test_code if self.repro_res else "",
            reproduction_output=self.repro_res.raw_output if self.repro_res else "",
            diff_text=self.diff_text,
        )


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
) -> bool:
    """Run the verification pipeline for one issue. Returns True only when admitted.

    Args:
        repro_test_code: a caller-supplied reproduction test. It is held to the
            same RED gate as a generated one.
        patch_generator: a caller-supplied `(attempt, feedback) -> diff` callable,
            for example a `ScriptedPatchGenerator` over a human-authored diff.
    """
    run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"
    cfg = load_config()
    rec = _RunRecorder(run_id, issue_number, issue_title, mode, cfg.run_artifacts_dir)
    sm = rec.sm

    print("=" * 60)
    print("Cerberus: verification gate for bug-fix patches")
    print(f"Run ID: {run_id} | Issue #{issue_number} | Mode: {mode.upper()}")
    print("=" * 60)

    if mode == "github" and not dry_run and not cfg.github_token:
        return rec.fail(PipelineState.ERROR, "CONFIG", "GitHub mode requested but GITHUB_TOKEN is not configured.")

    try:
        with Sandbox(base_dir=repo_dir, timeout_sec=cfg.sandbox_timeout_seconds) as sb:
            return _run_in_sandbox(
                sb, rec, cfg, issue_number, issue_title, issue_body, dry_run, mode,
                use_llm, use_llm_repro, fork_owner, repro_test_code, patch_generator,
            )
    except Exception as exc:
        if not sm.is_terminal:
            sm.transition(PipelineState.ERROR)
            log_event(run_id, "PIPELINE", "ERROR", issue_number, f"{type(exc).__name__}: {exc}")
            rec.finish(f"Unhandled error: {type(exc).__name__}: {exc}")
        raise


def _run_in_sandbox(
    sb: Sandbox,
    rec: _RunRecorder,
    cfg: Any,
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
) -> bool:
    sm = rec.sm
    run_id = rec.run_id

    # The gates compare the workspace against a commit. Without one there is no
    # base to diff against, so the run cannot produce evidence at all.
    head = sb.exec("git rev-parse --verify HEAD")
    if head.exit_code != 0:
        return rec.fail(
            PipelineState.ERROR,
            "SETUP",
            "Repository is not a git repository with at least one commit; there is no base revision to verify against.",
        )
    rec.base_commit_sha = head.stdout.strip()
    sb.exec("git config user.name \"Cerberus\"")
    sb.exec("git config user.email \"cerberus@autonomous.local\"")

    setup_agent = RepoSetupAgent(sb)
    setup_plan = setup_agent.detect()

    # [1/8] TRIAGE
    print("\n[1/8] TRIAGE")
    sm.transition(PipelineState.TRIAGE_PENDING)
    log_event(run_id, "TRIAGE", "STARTED", issue_number, "Starting triage")
    triage = TriageAgent(sb)
    triage_report = triage.triage_issue(issue_number, issue_title, issue_body)
    sm.transition(PipelineState.TRIAGED)
    print(f"  [OK] Base commit: {rec.base_commit_sha[:12]}")
    print(f"  [OK] Error signatures: {', '.join(triage_report.error_signatures) or 'none found'}")
    print(f"  [OK] Repo setup: {setup_plan.package_manager}/{setup_plan.test_framework} ({setup_plan.reason})")
    log_event(run_id, "TRIAGE", "PASS", issue_number, "Triage complete")

    target_code_file = _parse_referenced_file(issue_body, sb.workspace_dir)
    if not target_code_file:
        existing = [f for f in triage_report.referenced_files if os.path.isfile(os.path.join(sb.workspace_dir, f))]
        target_code_file = existing[0] if existing else None

    sm.transition(PipelineState.INDEXING)
    sm.transition(PipelineState.INDEXED)

    if setup_plan.install_commands:
        print("\n[SETUP] DEPENDENCIES")
        for cmd in setup_plan.install_commands:
            print(f"  -> {cmd}")
        if not setup_agent.install(setup_plan, timeout=max(cfg.sandbox_timeout_seconds, 120)):
            return rec.fail(PipelineState.ERROR, "SETUP", "Repository dependency installation failed.")
        print("  [OK] Repository dependencies installed")

    # [2/8] REPRODUCTION
    print("\n[2/8] REPRODUCTION")
    sm.transition(PipelineState.REPRODUCTION_PENDING)
    log_event(run_id, "REPRODUCTION", "STARTED", issue_number, "Obtaining reproduction test")
    repro_agent = ReproductionAgent(sb)
    repro_tokens = 0

    if repro_test_code:
        print("  -> Using caller-supplied reproduction test")
        rec.repro_res = repro_agent.run_reproduction_gate(issue_title, issue_body, repro_test_code)
    elif _llm_repro_enabled(use_llm_repro):
        if not has_api_key():
            return rec.fail(
                PipelineState.REJECTED_NON_REPRODUCIBLE,
                "REPRODUCTION",
                "Model reproduction was requested but no ANTHROPIC_API_KEY/OPENAI_API_KEY/GEMINI_API_KEY is set.",
            )
        if not target_code_file:
            return rec.fail(
                PipelineState.REJECTED_NON_REPRODUCIBLE,
                "REPRODUCTION",
                "Model reproduction needs a concrete source file referenced in the issue (e.g. 'at path/to/file.py:12').",
            )
        print(f"  -> Model-generated reproduction test, boundary: {target_code_file}")
        repro_synth = LLMReproductionSynthesizer(
            sandbox=sb,
            candidate_files=[target_code_file],
            issue_title=issue_title,
            issue_body=issue_body,
        )
        print(f"  -> Model: {repro_synth.model}")
        rec.repro_res = repro_synth.synthesize_and_verify(max_attempts=3)
        repro_tokens = repro_synth.usage.total_tokens
    else:
        return rec.fail(
            PipelineState.REJECTED_NON_REPRODUCIBLE,
            "REPRODUCTION",
            "No reproduction test is available: supply one (--repro-test) or enable model reproduction (--use-llm-repro).",
        )

    # [3/8] RED GATE
    print("\n[3/8] RED GATE")
    if not rec.repro_res.reproduced:
        return rec.fail(
            PipelineState.REJECTED_NON_REPRODUCIBLE,
            "RED_GATE",
            f"RED gate not satisfied: {rec.repro_res.error_message}",
        )
    sm.transition(PipelineState.REPRODUCED_RED)
    log_event(run_id, "RED_GATE", "PASS", issue_number, "RED reproduction confirmed")
    print("  [OK] RED reproduction confirmed (test fails on unpatched repo)")

    # [4/8] LOCALIZATION
    print("\n[4/8] LOCALIZATION")
    sm.transition(PipelineState.LOCALIZATION_PENDING)
    log_event(run_id, "LOCALIZATION", "STARTED", issue_number, "Localizing fault")
    localizer = LocalizationAgent(sb)
    loc_res = localizer.localize(
        issue_title=issue_title,
        issue_body=issue_body,
        error_signatures=triage_report.error_signatures,
        referenced_files=triage_report.referenced_files,
        max_candidates=3,
    )
    top_matches = [c for c in loc_res.candidates if target_code_file and target_code_file in c.file]
    top_cand = top_matches[0] if top_matches else (loc_res.candidates[0] if loc_res.candidates else None)
    top_file = top_cand.file if top_cand else target_code_file
    if not top_file:
        return rec.fail(PipelineState.ERROR, "LOCALIZATION", "Could not localize a repair target file.")
    sm.transition(PipelineState.LOCALIZED)
    print(f"  [OK] Top-1 candidate: {top_file}::{top_cand.symbol if top_cand else '<module>'}")
    log_event(run_id, "LOCALIZATION", "PASS", issue_number, f"Top candidate: {top_file}")

    # [5/8] PATCH LOOP
    print("\n[5/8] PATCH LOOP")
    sm.transition(PipelineState.PATCH_PENDING)
    log_event(run_id, "PATCH_LOOP", "STARTED", issue_number, "Entering patch loop")

    token_usage: Optional[Dict[str, int]] = None
    generator: Optional[PatchGenerator] = patch_generator
    llm_generator: Optional[LLMPatchGenerator] = None

    if generator is not None:
        print("  -> Using caller-supplied patch generator")
    elif _llm_patching_enabled(use_llm):
        if not has_api_key():
            return rec.fail(
                PipelineState.REJECTED_PATCH_FAILED,
                "PATCH_LOOP",
                "Model patching was requested but no ANTHROPIC_API_KEY/OPENAI_API_KEY/GEMINI_API_KEY is set.",
            )
        llm_generator = LLMPatchGenerator(
            sandbox=sb,
            candidate_files=[top_file],
            issue_title=issue_title,
            issue_body=issue_body,
        )
        generator = llm_generator
        print(f"  -> Model-generated patches, boundary: {top_file}")
        print(f"  -> Model: {llm_generator.model}")
    else:
        return rec.fail(
            PipelineState.REJECTED_PATCH_FAILED,
            "PATCH_LOOP",
            "No patch source is configured: supply a patch (--patch) or enable model patching (--use-llm).",
        )

    patch_agent = PatchAgent(sb, max_lines_changed=cfg.patch_max_lines_changed)
    loop_res = patch_agent.run_patch_loop([top_file], generator)
    print(f"  -> {loop_res.total_attempts} attempt(s)")

    if llm_generator is not None:
        token_usage = {
            "prompt_tokens": llm_generator.usage.prompt_tokens,
            "completion_tokens": llm_generator.usage.completion_tokens,
            "total_tokens": llm_generator.usage.total_tokens + repro_tokens,
        }
        print(f"  -> {llm_generator.usage.total_tokens} tokens (patch)")
    elif repro_tokens > 0:
        token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": repro_tokens}

    # The diff is read back off the workspace, so what gets measured, gated, and
    # published is the state of the files on disk rather than what the
    # generator claimed it was changing.
    rec.diff_text = DiffUtils.get_workspace_diff(sb)
    rec.diff_stats = DiffUtils.compute_diff_stats(rec.diff_text)
    rec.diff_hash = DiffUtils.compute_diff_hash(rec.diff_text)
    rec.patch_res = PatchLoopResult(
        reached_green=loop_res.reached_green,
        total_attempts=loop_res.total_attempts,
        winning_diff=rec.diff_text,
        history=loop_res.history,
        total_lines_changed=rec.diff_stats["total_lines"],
        final_changed_files=rec.diff_stats["files"],
        diff_hash=rec.diff_hash,
        is_empty_diff=rec.diff_stats["is_empty"],
    )

    if not loop_res.reached_green:
        return rec.fail(
            PipelineState.REJECTED_PATCH_FAILED,
            "PATCH_LOOP",
            f"No candidate patch made the reproduction test pass after {loop_res.total_attempts} attempt(s).",
        )
    sm.transition(PipelineState.PATCH_GREEN)
    print("  [OK] Target test GREEN (verified on the modified workspace)")
    log_event(run_id, "PATCH_LOOP", "PASS", issue_number, "Patch reached GREEN")

    # [6/8] REGRESSION
    print("\n[6/8] REGRESSION")
    sm.transition(PipelineState.REGRESSION_PENDING)
    log_event(run_id, "REGRESSION", "STARTED", issue_number, "Running regression suite")
    if not setup_plan.test_command:
        return rec.fail(
            PipelineState.REJECTED_REGRESSION,
            "REGRESSION",
            "No regression test command was detected for this repository.",
        )
    regr_agent = RegressionAgent(sb, max_total_lines=cfg.patch_max_lines_changed)
    rec.regr_res = regr_agent.run_regression_suite([top_file], test_suite_cmd=setup_plan.test_command)
    blast = rec.regr_res.blast_radius
    blast.observed_files = rec.diff_stats["files"]
    blast.lines_added = rec.diff_stats["lines_added"]
    blast.lines_deleted = rec.diff_stats["lines_deleted"]
    print(f"  -> {rec.regr_res.passed_count} passed, {rec.regr_res.failed_count} failed, {rec.regr_res.error_count} errors")
    rec.decision = AdmissionController.evaluate(rec.patch_res, rec.regr_res)

    if not rec.decision.regression_passed:
        return rec.fail(
            PipelineState.REJECTED_REGRESSION,
            "REGRESSION",
            f"Regression suite did not pass ({rec.regr_res.failed_count} failed, {rec.regr_res.error_count} errors).",
        )
    sm.transition(PipelineState.REGRESSION_CLEAN)
    log_event(run_id, "REGRESSION", "PASS", issue_number, "Regression suite clean")

    # [7/8] BLAST RADIUS
    print("\n[7/8] BLAST RADIUS")
    sm.transition(PipelineState.BLAST_RADIUS_PENDING)
    if not blast.is_acceptable:
        return rec.fail(
            PipelineState.REJECTED_BLAST_RADIUS,
            "BLAST_RADIUS",
            f"Scope violation: {blast.scope_violation_reason}",
        )
    sm.transition(PipelineState.BLAST_RADIUS_ACCEPTABLE)
    print(f"  [OK] Files changed: {len(blast.observed_files)} (+{blast.lines_added}/-{blast.lines_deleted} lines)")
    log_event(run_id, "BLAST_RADIUS", "PASS", issue_number, "Blast radius acceptable")

    # [8/8] ADMISSION
    print("\n[8/8] ADMISSION")
    sm.transition(PipelineState.ADMISSION_PENDING)
    decision = rec.decision
    print(f"  Target Test Gate:   {'PASS' if decision.target_test_passed else 'FAIL'}")
    print(f"  Regression Gate:    {'PASS' if decision.regression_passed else 'FAIL'}")
    print(f"  Blast Radius Gate:  {'PASS' if decision.blast_radius_acceptable else 'FAIL'}")
    print(f"  Patch Changed Gate: {'PASS' if decision.patch_changed else 'FAIL'}")

    if not decision.approved:
        state = (
            PipelineState.REJECTED_EMPTY_PATCH
            if decision.rejection_state == "REJECTED_EMPTY_PATCH"
            else PipelineState.REJECTED_ADMISSION
        )
        return rec.fail(state, "ADMISSION", decision.rejection_summary)

    sm.transition(PipelineState.ADMITTED)
    log_event(run_id, "ADMISSION", "PASS", issue_number, "PR admitted")

    # Publication is gated on the admission decision: this point is unreachable
    # unless all four gates held.
    rec.github_enabled = mode == "github" and bool(cfg.github_token)
    publisher = PRPublisher(sb, github_token=cfg.github_token)
    pr_body = publisher.build_evidence_report(
        issue_number=issue_number,
        issue_title=issue_title,
        reproduction_res=rec.repro_res,
        patch_res=rec.patch_res,
        regression_res=rec.regr_res,
        decision=decision,
        branch_name=triage_report.working_branch,
        token_usage=token_usage,
    )
    rec.pr_info = publisher.publish_pr(
        issue_number=issue_number,
        issue_title=issue_title,
        repo_slug=cfg.github_repo_slug or "org/repo",
        branch_name=triage_report.working_branch,
        pr_body=pr_body,
        dry_run=dry_run or mode == "local",
        fork_owner=fork_owner,
    )
    rec.finish(None)

    if rec.pr_info.get("status") == "error":
        pr_display = f"PR PUBLISH FAILED: {rec.pr_info.get('error')}"
    elif rec.pr_info.get("status") == "published":
        pr_display = rec.pr_info.get("pr_url") or "published (no URL returned)"
    else:
        pr_display = rec.pr_info.get("display_url") or "NOT CREATED"
        pr_display = pr_display.removeprefix("PR: ")

    _print_summary(
        mode=mode,
        issue_number=issue_number,
        status="ADMITTED",
        final_state=sm.state.value,
        reason="All 4 verification gates passed.",
        artifact_path=rec.artifact_path or "N/A",
        attempts=rec.patch_res.total_attempts,
        patch_stat=f"+{blast.lines_added}/-{blast.lines_deleted} in {len(blast.observed_files)} file(s), hash {rec.diff_hash[:12]}",
        pr_link=pr_display,
    )
    return True


def _print_summary(
    mode: str,
    issue_number: int,
    status: str,
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
    print(f"Status: {status}")
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


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cerberus: verification gate for bug-fix patches")
    parser.add_argument("--repo", default=".", help="Path to a local git repository")
    parser.add_argument("--issue", type=int, default=1, help="Issue number")
    parser.add_argument("--title", default="", help="Issue title")
    parser.add_argument("--body", default="", help="Issue body")
    parser.add_argument("--mode", choices=["local", "github"], default="local",
                        help="local never publishes; github may publish when not --dry-run")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Do not push or open a PR")
    parser.add_argument("--repro-test", metavar="PATH", help="Reproduction test file to hold to the RED gate")
    parser.add_argument("--patch", metavar="PATH", help="Unified diff to verify as the candidate patch")
    parser.add_argument("--use-llm", action="store_true", default=None,
                        help="Generate patches with a model (needs a provider API key). Also CERBERUS_USE_LLM=1.")
    parser.add_argument("--use-llm-repro", action="store_true", default=None,
                        help="Generate the reproduction test with a model. Also CERBERUS_USE_LLM_REPRO=1.")
    parser.add_argument("--demo", action="store_true", default=False,
                        help="Run the deterministic rate_calculator example (examples/rate_calculator).")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

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
                patch_generator=ScriptedPatchGenerator([scenario.patch_diff]),
            )
        return 0 if admitted else 1

    if not args.title:
        print("[ERROR] --title is required (or use --demo).")
        return 2

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
        patch_generator=ScriptedPatchGenerator([_read_text(args.patch)]) if args.patch else None,
    )
    return 0 if admitted else 1


if __name__ == "__main__":
    sys.exit(main())

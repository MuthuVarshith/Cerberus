# Cerberus progress

Core identity: an independent verification gate that decides whether a bug-fix patch has earned the right to become a
pull request. Features that do not improve verification, trust, reproducibility, safety, evidence, or measured repair
quality are out of scope.

Branch: `cerberus/verification-gate` (one commit per phase; nothing pushed).

## Phase status

| Phase | Status |
| --- | --- |
| 0 — Cleanup and truthful claims | Done |
| 1 — Reliable gates and sandbox | Not started |
| 2 — Generalization (`.cerberus.yml`, patch-source interface, verify existing PR, JUnit XML) | Not started |
| 3 — Repair quality | Not started |
| 4 — GitHub App workflow | Not started |
| 5 — Evaluation benchmark | Not started |

## Phase 0 — completed

- Demo logic moved out of the pipeline. `rate_calculator` is a fixture in `examples/rate_calculator/` run through the
  normal pipeline by `python main.py --demo`, using a supplied reproduction test and scripted patch.
- VoteVault's hardcoded reproduction test and fix preserved in `examples/votevault/` (the VoteVault source is not in
  this repository).
- Static-analysis mode removed. A failed RED gate always refuses. `PatchAgent.run_patch_loop` raises on an empty test
  command instead of treating it as GREEN.
- Production pipeline never synthesizes its own inputs: no reproduction test → `REJECTED_NON_REPRODUCIBLE`; no patch
  source → `REJECTED_PATCH_FAILED`; model mode requested without a key → refused (no silent scripted fallback).
- New CLI inputs: `--repro-test PATH`, `--patch PATH` (human-authored or externally generated patch).
- Every exit path writes `run.json` with `final_state`, the real base commit SHA, reproduction test and output, and
  the workspace diff (schema 1.2). Regression failure now ends in `REJECTED_REGRESSION` instead of recording
  `REGRESSION_CLEAN`.
- Removed `git commit --amend`, index renormalization and `core.autocrlf` rewriting from the pipeline.
- Removed unauthenticated `/api/scan`, `/api/repair`, `/api/repair-all`, `/api/repair-status`, `/api/chat-plan`,
  `/api/baseline-read`, `/api/runs`, the dashboard, the repository scanner (`github/api_reader.py`) and the canned
  `DiscoveryAgent`. The service now exposes only `/health` and `/webhook`.
- Removed fabricated values: fallback PR URL, synthesized stage durations, hardcoded fallback username, placeholder
  base SHA, stipulated "SWE-bench Lite 25", baseline and ablation tables, invented timings and patch sizes in the
  smoke runner, invented "target" column in the metrics table.
- `evaluation/swe_bench_runner.py` renamed to `evaluation/smoke_runner.py` and labelled as scripted gate scenarios.
- Deleted untracked scratch scripts (`patch_*.py`, `run_all_tests.py`) and test repository clones
  (`my_test_repo`, `test_diff_repo`, `test_diff_repo2` — copies of the author's FusionTrace repo on GitHub — and
  `tiny_test_repo`). Deleted generated `artifacts/run_*` directories.
- Tests write run artifacts to a temporary directory (`tests/conftest.py`); `pytest.ini` limits collection to `tests/`.
- READMEs rewritten to describe only implemented behaviour.

## Architectural decisions

- **Scripted inputs are caller inputs, not pipeline branches.** Demos and human patches use the same
  `repro_test_code` / `patch_generator` parameters a future external-agent adapter will use.
- **Refuse rather than improvise.** Missing evidence ends the run with a recorded reason.
- **Local execution policy (decided with the user):** Docker is the default sandbox; a loudly labelled, explicit
  opt-in development mode may run trusted local fixtures on the host with secrets stripped, and can never publish.
  To be implemented in Phase 1.
- **Git:** work on a branch with one commit per phase.

## Known limitations (current)

- Sandbox silently falls back to host execution without Docker (Phase 1).
- Docker is not installed on the development machine, so the Docker path has not been exercised end to end.
- RED accepts import/collection errors as reproduction (Phase 1).
- Regression gate is not baseline-aware and parses console output (Phase 1).
- Scope gate ignores untracked new files (Phase 1).
- `PRPublisher` commits with `git add -A`, which would include `test_reproduce.py` in a live PR.
- Webhook idempotency is in memory.

## Test log

| Date | Scope | Result |
| --- | --- | --- |
| 2026-09-17 | Phase 0 full suite (`python -m pytest -q`, host sandbox, no Docker) | 114 passed |

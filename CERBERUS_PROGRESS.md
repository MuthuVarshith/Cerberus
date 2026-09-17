# Cerberus progress

Core identity: an independent verification gate that decides whether a bug-fix patch has earned the right to become a
pull request. Features that do not improve verification, trust, reproducibility, safety, evidence, or measured repair
quality are out of scope.

Branch: `cerberus/verification-gate` (one commit per phase; nothing pushed).

## Phase status

| Phase | Status |
| --- | --- |
| 0 — Cleanup and truthful claims | Done |
| 1 — Reliable gates and sandbox | Done |
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

## Phase 1 — completed

- **Terminal states:** `ADMITTED`, `REFUSED` (+ `RefusalCode`), `ERROR`. Progress states enforce order
  (triage → setup → RED → baseline → localization → GREEN → regression → scope → admission); `ADMITTED` is reachable
  only from `ADMISSION_PENDING`; a `finally` guard records `ERROR` if any path returns without deciding.
  (Phase 0's `REJECTED_*` states are replaced by `REFUSED` + code.)
- **JUnit evidence** (`harness/junit.py`): all test outcomes come from JUnit XML; missing, malformed, DTD/entity or
  empty reports are refused. Exception type and innermost frame are parsed from the final traceback line.
- **RED gate** (`agents/reproduction_agent.py`): parse → relevance (imports repo code; references an issue symbol written
  as code, checked against repository-wide symbols) → 3 JUnit runs → reject setup/collection errors and
  Import/Syntax errors → accept assertions, `Failed`, issue-named exceptions, or exceptions raised in repository code →
  identical outcomes across runs.
- **GREEN:** the patch loop uses a JUnit verifier; after the loop GREEN is re-verified 3×; a changed reproduction test
  (sha256) is refused as `REPRODUCTION_TEST_TAMPERED`.
- **Regression** (`agents/regression_agent.py`): baseline JUnit run on the unpatched commit; post-patch comparison by
  test ID (newly failing, disappeared, pre-existing); suspected regressions re-checked on base via `git stash` and
  reclassified as flaky if they fail there too. Non-pytest commands are unverifiable.
- **Scope** (`harness/scope_gate.py`): tracked + untracked (intent-to-add) changes vs the base SHA; new/deleted files;
  line counts; changed Python functions/classes via `ast` spans on both sides of each hunk (computed by a fixed helper
  script inside the sandbox). Policy: files ⊆ allowed scope, ≤ 3 files, ≤ 200 lines. Symbols are evidence, not policy.
- **Sandbox** (`harness/docker_sandbox.py`): Docker default, fails closed (no Docker, daemon down, image missing);
  networked setup container → `docker commit` → offline repair container (`--network none`, `--cap-drop ALL`,
  `no-new-privileges`, memory/CPU/PID limits, tmpfs `/tmp`, in-container `timeout -s KILL`, no host env).
  Explicit `host-unsafe` mode: allowlisted env, no installs, cannot publish or run GitHub mode.
  `sandbox/Dockerfile` builds `cerberus-sandbox:py3.11` (python:3.11-slim + git + pytest).
- **Workspace:** `git clone --no-hardlinks -c core.autocrlf=false` of committed HEAD; `.cerberus/` harness dir hidden
  via `.git/info/exclude`; patches touching `.git/`, `.cerberus/`, absolute or `..` paths are rejected; rollback is
  `git reset --hard <base>` + `git clean -fd` in the disposable clone.
- **Publishing** (`github/pr_publisher.py`): never inside the sandbox. Fresh host clone at base SHA, verified diff
  applied with list-argument git, token via `GIT_CONFIG_*` env (base64 extraheader), draft PRs, refuses diffs touching
  `.github/workflows/`, `.git/`, `.cerberus/`.
- **Repo setup:** detection only (no execution); pip-based installs; pytest test command.
- Removed dead code: `harness/tools.py` (ACI), `harness/context_manager.py`, `SANDBOX_NETWORK_DISABLED`.
- Issue-referenced paths are confined to the workspace.

## Architectural decisions

- **Scripted inputs are caller inputs, not pipeline branches.** Demos and human patches use the same
  `repro_test_code` / `patch_generator` parameters a future external-agent adapter will use.
- **Refuse rather than improvise.** Missing evidence ends the run with a recorded reason.
- **Local execution policy (decided with the user):** Docker is the default sandbox; a loudly labelled, explicit
  opt-in development mode (`--unsafe-local-sandbox` / `CERBERUS_SANDBOX=host-unsafe`) runs trusted local fixtures on
  the host with secrets stripped, and can never publish. The test suite opts in explicitly in `tests/conftest.py`.
- **No host git after untrusted code runs.** A rewritten `.git/config` in the bind-mounted workspace could make host git
  execute commands (e.g. `core.fsmonitor`), so all workspace git runs in the sandbox; host git touches only the source
  repo (clone) and the separate publishing clone.
- **Base SHA, not HEAD.** Code in the sandbox can move `HEAD`; diffs and rollback use the recorded base commit.
- **Threat model:** Cerberus verifies patches to trusted-but-buggy repositories. Repository code runs inside the test
  process and could forge its own JUnit results; that is out of scope.
- **Git:** work on a branch with one commit per phase.

## Known limitations (current)

- Docker is not installed on the development machine: the Docker path is verified only with a mocked Docker CLI.
  It must be run against a real daemon (build `sandbox/Dockerfile`, run `python main.py --demo`) before any claim
  that it works.
- POSIX bind-mount permissions (workspace made world-writable inside a 0700 temp root, `umask 0000` in container) are
  designed but unexercised on Linux.
- Test command is always `python -m pytest -q`; no per-repo configuration yet (Phase 2 `.cerberus.yml`).
- Scope boundary is the single localized file; localization quality is unmeasured.
- The patch generator and verification gate are separated by a callable, but there is no formal patch-source
  interface, external-agent adapter, or "verify an existing PR" mode yet (Phase 2).
- RED relevance is heuristic; a test can pass every rule and still encode wrong behaviour.
- Regression flake detection re-checks only suspected regressions, once, on the base code.
- Webhook idempotency is in memory; webhook runs have no reproduction/patch source unless model modes are enabled.
- Live publishing (push + API) is covered only by mocked tests.

## Test log

| Date | Scope | Result |
| --- | --- | --- |
| 2026-09-17 | Phase 0 full suite (`python -m pytest -q`, host sandbox, no Docker) | 114 passed |
| 2026-09-17 | Phase 1 full suite (`python -m pytest -q`, explicit host-unsafe sandbox, Docker mocked) | 173 passed |
| 2026-09-17 | `python main.py --demo` without Docker | `ERROR` (fails closed), exit 1 |
| 2026-09-17 | `python main.py --demo --unsafe-local-sandbox` | `ADMITTED`, exit 0 |
| 2026-09-17 | `python evaluation/smoke_runner.py --unsafe-local-sandbox` | 2 admitted, 3 refused (RED_NOT_FAILING, REGRESSION, SCOPE_VIOLATION) as designed |

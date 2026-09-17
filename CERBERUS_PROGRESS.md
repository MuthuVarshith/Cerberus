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
| 2 — Generalization (`.cerberus.yml`, patch-source interface, verify existing change, JUnit XML) | Done (local; GitHub PR fetch + Check Run deferred to Phase 4) |
| Evaluation benchmark (built before Phase 3) | Done: v1 frozen, scored |
| 4 — GitHub App workflow | Done (tested against a fake GitHub API; not installed on a real repository) |
| 3 — Repair quality | Deferred (see "Phase 3 decision") |

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

## Phase 2 — completed

- **`.cerberus.yml`** (`harness/repo_config.py`): `setup`, `test.command` / `test.report` / `{junit_xml}` placeholder,
  `test.exclude` (fnmatch on JUnit IDs), `scope.allowed_paths` / `max_files` / `max_lines` /
  `allow_test_modifications`, `budgets` (patch attempts, RED runs, GREEN runs, command timeout). `yaml.safe_load`,
  strict validation (unknown keys, types, ranges, relative paths). Loaded from the base commit before any repository
  code runs; invalid config → `ERROR`. Patches touching `.cerberus.yml` are rejected.
- **Patch-source interface** (`agents/patch_sources.py`): `PatchSource.generate_patch(PatchRequest) -> diff`.
  Adapters: `DiffPatchSource` (diff file, existing change, fixtures), `LLMPatchSource` (built-in generator),
  `ExternalAgentPatchSource` (headless agent in a scratch export of the base commit; edits captured by a pure-Python
  tree diff so git never runs on the agent's tree; binary/symlink changes refused). `claude-code` preset allows only
  file tools.
- **Verify an existing change:** `--base REF --head REF`. Workspace is cloned at base (`Sandbox(base_ref=...)`);
  candidate is `git diff base head` from the user's repository (list args, no ext-diff/textconv). Scope defaults to
  any file unless configured.
- **Test-weakening detection:** the scope gate refuses modifications/deletions of existing test files unless
  `allow_test_modifications: true`; new test files are allowed. The regression gate can't see an edited assertion.
- **Language-agnostic regression gate:** any runner producing JUnit XML via `{junit_xml}` or `test.report`. RED/GREEN
  and patch generation stay Python/pytest.
- **Security fix:** `retrieval/lexical_search.py` no longer runs `git grep` on the host inside the workspace (the
  workspace `.git` is untrusted after repository code runs); pure-Python search only.
- CLI enforces exactly one patch source (`--patch`, `--base/--head`, `--agent`/`--agent-command`, `--use-llm`).
- Dependency: `pyyaml>=6.0`.

## Evaluation benchmark — completed

- `benchmark/instances/v1.yml`: 24 seeded instances over three template libraries (`textkit`, `intervals`,
  `money`): 7 correct fixes, 4 plausible-but-wrong fixes, 3 regressions, 2 scope leaks, 2 test-weakening patches,
  1 non-reproducible issue, 4 invalid reproduction tests, 1 ineffective patch.
- Hidden specification tests per template (`benchmark/hidden/`) are the ground truth for patch correctness.
- `python evaluation/benchmark.py --validate` checks authoring independently of Cerberus; it caught two ambiguous seed
  edits before freezing. `--freeze` writes `benchmark/MANIFEST.json`; scored runs refuse a changed set.
- Metrics: admitted-fix rate, false-admission rate, admitted code failing hidden tests, rejection accuracy,
  false-rejection rate, reproduction validity (RED-passing tests that pass on the reference fix), refusals by code,
  patch attempts, wall time; Wilson 95% intervals. Cost: not measured (no model).
- Baseline measured on the same instances: admit when the candidate applies and the reproduction test passes.
- Known result shape (from the first scored run): all false admissions are plausible-but-wrong patches that pass every
  visible test; `mn-07` (deleting the only test file) is refused as `REGRESSION_UNVERIFIABLE`, which is not in its
  frozen list of acceptable codes and is counted against rejection accuracy rather than edited.

## Phase 4 — completed (GitHub App)

- `github/app_auth.py`: RS256 App JWT with `cryptography`; cached installation tokens. Least-privilege permission
  list documented (no Workflows permission).
- `github/client.py`: minimal REST client (PRs, issues, collaborator permission, Check Runs, comments) with injectable
  transport, redacted errors, output clipped to GitHub limits.
- `github/run_store.py`: SQLite store; deliveries claimed once (idempotent across restarts); run rows from queued to
  terminal state linked to Check Run id and `run.json`; runs-per-hour counts; interrupted runs marked `ERROR` on startup.
- `github/app_service.py`: routing and authorization (label or `/cerberus verify` by write-access users; label-gated
  re-verification on push; `/cerberus repair` only with a configured patch source; bots ignored); per-repo run
  serialization; hourly cap; Docker required; each run is a CLI subprocess with a wall-clock timeout.
  PR verification uses the PR's single new test file as the reproduction test and reports a Check Run.
- `github/check_report.py`: Check Run conclusion/title/summary/text for ADMITTED, REFUSED, ERROR and timeouts.
- `github/webhook_handler.py`: `/health` and `/webhook` only; routes through the App; 503 when unconfigured; legacy
  issue-label dispatch removed. CLI gained `--run-id`.

## Phase 3 decision

Most of Phase 3 is deferred, deliberately:

- The rules for this work say not to optimize before a benchmark exists, and to improve repair only once verification
  is trustworthy. Verification is not yet validated against real Docker, real repositories or real agents.
- No model API key is available in this environment, so changes to localization, edit formats, candidate sampling or
  reproduction synthesis could not be measured, only asserted.
- The repair engine is not the product: external agents are usable as patch sources through the Phase 2 interface.

What would be worth doing once a key and real repositories are available, in order: (1) run the benchmark with
`--agent claude-code` and `--use-llm` patch sources to get a measured baseline for model-generated patches;
(2) search/replace edit format for the built-in generator if apply failures dominate; (3) several candidate patches
selected by the gates; (4) several candidate reproduction tests selected by the RED gate.

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
- **External agents are outside the sandbox.** They run on the host with their own credentials and permission model;
  Cerberus sandboxes verification, not generation. Presets restrict tools where the agent supports it.
- **Policy from the base commit.** `.cerberus.yml` is read before repository code runs and cannot be changed by the
  patch under verification.
- **Threat model:** Cerberus verifies patches to trusted-but-buggy repositories. Repository code runs inside the test
  process and could forge its own JUnit results; that is out of scope.
- **Git:** work on a branch with one commit per phase.

## Known limitations (current)

- Docker is not installed on the development machine: the Docker path is verified only with a mocked Docker CLI.
  It must be run against a real daemon (build `sandbox/Dockerfile`, run `python main.py --demo`) before any claim
  that it works.
- POSIX bind-mount permissions (workspace made world-writable inside a 0700 temp root, `umask 0000` in container) are
  designed but unexercised on Linux.
- Without `.cerberus.yml`, the test command is `python -m pytest -q` and the repair scope is the single localized
  file; localization quality is unmeasured.
- The external-agent adapter is tested only with a scripted stand-in; it has not been run with real Claude Code or
  Codex (that consumes the user's account and needs explicit permission).
- The GitHub App is tested only against a fake GitHub API; registering and installing an App requires the user.
- The benchmark instances are small, synthetic and author-written; there is no real-world or human-authored bug set.
- PR verification requires the PR to add exactly one new test file.
- Test-file detection uses Python naming conventions (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`).
- RED relevance is heuristic; a test can pass every rule and still encode wrong behaviour.
- Regression flake detection re-checks only suspected regressions, once, on the base code.
- Live publishing (push + API) is covered only by mocked tests.

## Test log

| Date | Scope | Result |
| --- | --- | --- |
| 2026-09-17 | Phase 0 full suite (`python -m pytest -q`, host sandbox, no Docker) | 114 passed |
| 2026-09-17 | Phase 1 full suite (`python -m pytest -q`, explicit host-unsafe sandbox, Docker mocked) | 173 passed |
| 2026-09-17 | `python main.py --demo` without Docker | `ERROR` (fails closed), exit 1 |
| 2026-09-17 | `python main.py --demo --unsafe-local-sandbox` | `ADMITTED`, exit 0 |
| 2026-09-17 | `python evaluation/smoke_runner.py --unsafe-local-sandbox` | 2 admitted, 3 refused (RED_NOT_FAILING, REGRESSION, SCOPE_VIOLATION) as designed |
| 2026-09-17 | Phase 2 full suite (`python -m pytest -q`, explicit host-unsafe sandbox, Docker mocked) | 190 passed |

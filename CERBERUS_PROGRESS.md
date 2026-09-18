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
| Real Docker and external repositories | Done (Docker Desktop on Windows; three open-source repositories) |
| Portfolio packaging | Done (`DEMO.md`, `INTERVIEW.md`, `PORTFOLIO.md`, static summary page) |
| Real coding-agent run | Blocked: headless `claude -p` answers `Not logged in` on this machine |
| 3 — Repair quality | Deferred (see "Phase 3 decision") |

**Implementation frozen** after commit `e394d03` (code) / `371a62b` (records). Later commits are documentation,
demo material and cleanup only.

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

## Real Docker and external repositories — completed

Environment: Docker Desktop 4.91 (Engine 29.8, WSL 2) on Windows 11. Docker Desktop initially failed to start because
of stale AF_UNIX socket files left by a crashed session; the folders holding them were renamed aside, not deleted
(`%LOCALAPPDATA%\Docker\run.stale-20260917`, `run.stale2-20260917`, `%LOCALAPPDATA%\docker-secrets-engine.stale-20260917`).

- **Sandbox verified in real containers** (`tests/test_docker_integration.py`, skipped without Docker): only `lo` in the
  repair phase and no outbound connection; no host environment (including a planted secret); `CapEff`/`CapBnd` empty,
  `NoNewPrivs` 1; `memory.max` 2 GiB and `pids.max` 256, with a 400-process spawn stopped by the limit; in-container
  kill timeout; leftover processes killed after each command; container-planted symlinks not followed by the host;
  networked setup whose filesystem carries into the offline container; setup refused after repair starts; container
  and setup image removed on destroy; missing image fails closed; orphaned container stops and removes itself; demo
  `ADMITTED` and a regressing patch `REFUSED/REGRESSION` inside Docker.
- **Security fix — symlinks.** Host-side reads and writes of workspace files followed symlinks, so on a Linux or macOS
  host a link committed to a repository or created by code in the container could redirect a harness write
  (candidate patch, reproduction test, pathspec files) or read (JUnit reports, `pyproject.toml`, `.cerberus.yml`,
  files sent to a model) outside the workspace. `Sandbox._safe_resolve` now refuses any symlinked component; reads
  and writes use `O_NOFOLLOW`; `remove_file` unlinks without following; repository scanners skip links;
  `.cerberus.yml` may not be a link; `.git/info/exclude` is written once before any repository code runs and git
  commands exclude `.cerberus/` explicitly. On Linux (unit tests run inside a container) the directory-symlink test
  fails against the previous code (the write escaped) and passes now.
- **Security fix — leftover processes.** A background process started by a command survived it and could race host
  file access. Every command now ends with `kill -9 -1` inside the container, and PID 1 is a small Python reaper so
  the killed processes do not accumulate as zombies against the PID limit.
- **Robustness — orphaned containers.** A process killed without cleanup (observed during validation; also the GitHub
  App's timeout path) left containers running. Containers now run with `--rm` and a `cerberus.sandbox=true` label,
  and PID 1 exits after `CERBERUS_SANDBOX_MAX_LIFETIME_SECONDS` (default 4 h; the App uses run timeout + 300 s).
- **Gate fix — tests added to existing files.** The unmodified upstream sqlparse commit was refused as
  `SCOPE_VIOLATION` because it appends its test to `tests/test_parse.py`, the shape of most real fixes. Existing test
  files with only added lines are now allowed and recorded as `extended_test_files`; the regression gate checks them
  out at the base commit for its run and restores the patch's bytes afterwards, so an added skip marker, early
  `return` or monkeypatch cannot change how existing tests judge the patch. With that step disabled, a test adding an
  early `return` to a test the patch breaks is admitted; with it, the patch is refused as `REGRESSION`. Changed or
  deleted test lines and any `conftest.py` change remain scope violations.
- **Bug fix — blank trailing context line.** `extract_diff_from_markdown` stripped all trailing whitespace, deleting a
  final context line that is a single space; git then rejected the patch as corrupt (the unmodified boltons commit
  was refused as `GREEN_NOT_REACHED` for this reason). Only surrounding line breaks are stripped now.
- **Evidence.** `GREEN_NOT_REACHED` names the last attempt's failure in one line; `run.json` gains `patch_history`
  (status, applied, detail per attempt); the duplicate-diff abort record no longer counts as an attempt.
- **External repositories** (`evaluation/external_repos.py`, `evaluation/external/`): sqlparse `f66d12c`, boltons
  `ead236e`, more-itertools `cca3294`; reproduction test = the test the fix commit added; 11 variants. Report of
  record `evaluation/external/report.md` at `e394d03`: 11/11 expected verdicts.
- **Benchmark under Docker:** `benchmark/reports/v1-docker.md` at `e394d03`: every per-instance decision identical to
  the host-unsafe v1 record; mean patch attempts 1 (was 1.05 before the attempt-count fix); median wall time 10.0 s,
  p90 13.0 s per instance including container startup.

## Portfolio phase — completed (documentation and demo), one item blocked

- **Documentation:** `DEMO.md` (8–10 minute recording script with verified commands), `INTERVIEW.md` (15 technical
  answers tied to the implementation), `PORTFOLIO.md` (summary, results, limitations); `README.md` gained the problem
  statement, a pipeline diagram, the real-agent status and a future-work list.
- **Static summary page**, kept in the repository as `site/index.html` (open locally or serve `site/` with GitHub
  Pages) and published as a private Claude artifact: <https://claude.ai/artifact/PrPgtFPsjTYetf8VHrwhsR>. Read-only —
  no service, no execution, no credentials, no Docker socket. The verifier itself is deliberately not exposed: it
  runs untrusted repository code and belongs behind the validated sandbox, not behind a public URL. A free
  spin-down host (Render's free tier and similar) would add nothing a static page does not already give.
- **Real coding-agent run: blocked, not skipped.** `claude` 2.1.272 is installed and `~/.claude.json` holds an OAuth
  account, but a headless `claude -p` subprocess answers `Not logged in · Please run /login`, so no real agent run has
  happened and no result is claimed. The demo script carries the exact command to use once the CLI is authenticated;
  `evaluation/external/tests/sqlparse-332-reproduce.py` exists so that run is a single command.
- **Not done under the freeze:** `--run-id` is ignored by `--demo` (documented rather than fixed), and the GitHub App
  still requires exactly one new test file.

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
  process and could forge its own JUnit results; that is out of scope. Escaping the sandbox is in scope: the host
  never follows a workspace symlink, and no container process outlives its command, so nothing in the container can
  redirect or race host-side file access.
- **Existing tests judge the patch in their base form.** A patch may add tests to an existing test file; the
  regression gate checks those files out at the base commit for its run and restores the patch's bytes afterwards.
  Changing or deleting existing test lines stays a scope violation.
- **Git:** work on a branch with one commit per phase.

## Known limitations (current)

- The Docker path has run against Docker Desktop on a Windows host only. On that host, symlinks created in a container
  are not followable from Windows at all; the symlink defences matter on Linux and macOS hosts, where they are covered
  by unit tests run inside a Linux container, not by a Linux host running Docker.
- POSIX bind-mount permissions (workspace made world-writable inside a 0700 temp root, `umask 0000` in container) are
  designed but unexercised on a Linux host.
- External validation covers three bugs in three small pure-Python libraries chosen for fast suites; it shows the
  gates work on code Cerberus was not written against, not how they perform on a representative bug sample.
- Without `.cerberus.yml`, the test command is `python -m pytest -q` and the repair scope is the single localized
  file; localization quality is unmeasured.
- The external-agent adapter is tested only with a scripted stand-in; it has not been run with real Claude Code or
  Codex (that consumes the user's account and needs explicit permission).
- The GitHub App is tested only against a fake GitHub API; registering and installing an App requires the user.
- The benchmark instances are small, synthetic and author-written; there is no real-world or human-authored bug set.
- GitHub App PR verification requires the PR to add exactly one new test file, so the common shape of a real fix
  (a test appended to an existing file) is refused by the App; the CLI verifies it with `--base/--head --repro-test`.
- `--demo` generates its own run id and ignores `--run-id`; the run still prints its artifact path. Left as is under
  the implementation freeze and documented in `DEMO.md`.
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
| 2026-09-17 | Benchmark + Phase 4 full suite (GitHub API faked, host-unsafe sandbox, Docker mocked) | 211 passed |
| 2026-09-17 | `python evaluation/benchmark.py --validate --unsafe-local-sandbox` | 24 instances, 0 problems (after fixing 2 ambiguous seed edits) |
| 2026-09-17 | Benchmark v1 report of record at `271bf37` (`benchmark/reports/v1.md`) | Cerberus admitted 11 (4 should have been refused); ungated baseline admitted 20 (13 should have been refused); 7/7 correct fixes admitted by both; rejection accuracy 12/17 |
| 2026-09-17 | `docker build -t cerberus-sandbox:py3.11 sandbox/`; `python main.py --demo` (Docker, before the fixes below) | `ADMITTED`, 25 s |
| 2026-09-17 | `tests/test_sandbox.py` on Linux (python:3.11 container standing in for a Linux host) | 23 passed, 0 skipped; directory-symlink test fails against the previous sandbox code |
| 2026-09-17 | Full suite at `e394d03` (real Docker engine for `test_docker_integration.py`) | 231 passed, 5 skipped (symlink tests: no symlink privilege on this Windows account) |
| 2026-09-17 | `python evaluation/external_repos.py --record` at `e394d03` | 11/11 expected verdicts (6 admitted, 5 refused with the expected codes) |
| 2026-09-17 | `python evaluation/benchmark.py` (Docker) at `e394d03` (`benchmark/reports/v1-docker.md`) | Same per-instance decisions as the v1 record; mean attempts 1; median 10.0 s, p90 13.0 s |
| 2026-09-17 | Portfolio phase: full suite with Docker | 231 passed, 5 skipped (9 min 13 s) |
| 2026-09-17 | Portfolio phase: `python main.py --demo` (Docker) | `ADMITTED`; artifact `artifacts/run_b9b0f21bfd/run.json` |
| 2026-09-17 | Portfolio phase: `python evaluation/external_repos.py --only sqlparse-332` | 4 of 4 expected verdicts, 2 min 49 s including the clone |
| 2026-09-18 | GitHub App live on `MuthuVarshith/VoteVault-`, five PRs, recorded (`docs/video/`) | #2 and #4 correct fixes: `ADMITTED`; #3 export fix that deletes the admin check: `REGRESSION` (`test_export_votes_requires_admin`); #5 returns `None`: `GREEN_NOT_REACHED`; #6 edits an existing test: `SCOPE_VIOLATION`. All five matched the verdicts predicted by local CLI runs beforehand |
| 2026-09-18 | GitHub App live on `MuthuVarshith/test-repository` (local service, smee.io relay, Docker) | PR #1 correct fix: Check Run success, `ADMITTED`; PR #2 careless fix: Check Run failure, `REFUSED / REGRESSION` naming `test_small_orders_pay_full_price` |
| 2026-09-17 | Portfolio phase: headless `claude -p` probe | `Not logged in · Please run /login` — real coding-agent run not attempted |

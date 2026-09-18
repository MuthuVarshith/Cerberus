<div align="center">

# Cerberus

### Autonomous Patch Verification Gate

**Agents write patches. Cerberus decides whether a patch has earned a pull request.**

[Demo](#demo) · [How it works](#how-it-works) · [Results](#results) · [Security](#security-model) ·
[Quick start](#quick-start) · [Documentation](#documentation)

</div>

---

Cerberus is an independent verification gate for bug-fix patches — written by a person, a script, or an AI coding
agent such as Claude Code. It never writes the fix itself. It runs every patch against evidence the patch's author
does not control, inside an isolated Docker sandbox, and ends in exactly one decision with the reason attached:
**`ADMITTED`**, **`REFUSED`** with a code naming the gate that stopped it, or **`ERROR`**.

## Demo

<p align="center">
  <img src="docs/assets/cerberus-demo.gif" alt="Terminal recording of Cerberus admitting a fix, then checking four patches on the real sqlparse repository" width="476">
</p>

A real session, sped up to 30 seconds. First `python main.py --demo`: a calculator with a divide-by-zero bug goes
through all nine stages — the bug is reproduced three times, the fix makes the test pass three times, no existing
test breaks — and the patch is **ADMITTED**. Then `python evaluation/external_repos.py --only sqlparse-332` checks
four patches against the real [sqlparse](https://github.com/andialbrecht/sqlparse) project: the maintainers' fix is
admitted, a fix that silently breaks three other tests is **REFUSED (`REGRESSION`)**, and the same fix with those
tests edited to hide the damage is **REFUSED (`SCOPE_VIOLATION`)**. The recording ends on the generated report.

## Highlights

<p align="center">
  <img src="docs/assets/01-overview.png" alt="Cerberus overview: headline results and the three possible verdicts" width="860">
</p>

| | Result |
| --- | --- |
| **Real open-source projects** | **11 / 11** correct verdicts on sqlparse, boltons and more-itertools |
| **Wrong patches approved** | **65% → 36%** compared with approving any patch whose own test passes |
| **Correct fixes approved** | **7 / 7** — no good fix wrongly rejected |
| **Test suite** | **231** automated tests, including 13 that run against a real Docker engine |

*The overview card above is the top of the project's summary page ([`site/index.html`](site/index.html)). It exists
so a reader gets the four numbers that matter before any detail: accuracy on real projects, improvement over the
obvious alternative, and the size of the test suite behind those claims.*

## Why Cerberus

A coding agent can produce a plausible patch for almost any bug report in seconds. The bottleneck is no longer
writing the patch — it is deciding whether it deserves a reviewer's time. An agent that judges its own work is the
weakest evidence available: it chose the change, it chose which tests to run, and *"the test I wrote passes"* says
nothing about the hundreds of tests that already existed.

Cerberus is the reviewer's side of that exchange. It re-derives the evidence itself: that the bug is real, that the
patch fixes it, that nothing else broke, and that the patch changed only what it should.

## How it works

```text
 bug report + reproduction test            patch (person · script · coding agent · model)
                  │                                      │
                  ▼                                      ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  TRIAGE → SETUP (only networked phase) → RED ×3 → BASELINE       │
  │     → LOCALIZATION → apply patch → GREEN ×3 → REGRESSION         │
  │     → SCOPE → ADMISSION                                          │
  │  everything after SETUP runs offline in a Docker container       │
  └──────────────────────────────────────────────────────────────────┘
                  │
                  ▼
    ADMITTED → draft PR / GitHub Check Run      REFUSED (code) / ERROR
    evidence for every run: artifacts/<run_id>/run.json
```

<p align="center">
  <img src="docs/assets/02-pipeline-and-run.png" alt="The nine-stage pipeline and the terminal output of a real admitted run" width="860">
</p>

*This screenshot pairs the pipeline with the real terminal output of an admitted run, so each stage in the diagram
can be matched to the evidence it produced: RED confirmed in 3 of 3 runs with the exception type, the baseline test
count, GREEN in 3 of 3 runs, zero newly failing tests, and the exact function the patch changed.*

| Gate | Passes only when |
| --- | --- |
| **RED** | The reproduction test fails on the unpatched code **for the right reason** — an assertion, an exception named in the issue, or an exception raised inside repository code — identically in 3 runs. Import, syntax and collection errors never count. |
| **Baseline** | The repository's own test suite runs on the unpatched commit and every result is recorded by test ID. |
| **GREEN** | With the patch, the *unmodified* reproduction test passes in 3 of 3 runs. Its SHA-256 is checked, so a patch that rewrites the test is refused. |
| **Regression** | No test that passed at baseline now fails, errors, or stops running. Failures that already existed are reported, not blamed on the patch; suspected regressions are re-checked on the base code to separate flaky tests. |
| **Scope** | Every changed file — including new and deleted files — is inside the allowed scope, within size limits, and no existing test line was changed. Adding tests is allowed; rewriting the tests that judge the patch is not. |
| **Admission** | GREEN, regression, scope, and a non-empty change against the base commit all hold. |

All outcomes come from JUnit XML, never from exit codes, and an unreadable result is a refusal — never a pass.

### A worked example

The real sqlparse bug [`f66d12c`](https://github.com/andialbrecht/sqlparse/commit/f66d12c245412f28c58f045b646eb53c0e691b8b):
for the SQL name `db.schema.tbl.col`, `get_real_name()` returned `schema` instead of `col`.

1. **RED** — the reproduction test fails on the original code in 3 of 3 runs with an `AssertionError`. The bug is real.
2. **Baseline** — all 492 of sqlparse's tests run and are recorded.
3. **GREEN** — with the maintainers' fix, the unmodified test passes in 3 of 3 runs.
4. **Regression** — all 492 tests again: none newly failing.
5. **Scope** — one file changed, one function: `NameAliasMixin.get_real_name`.
6. **Verdict: ADMITTED**, with a `run.json` recording every step.

A different patch for the same bug also fixed it but changed how bracketed names are handled. It passed RED and
GREEN — an agent checking only its own test would have shipped it — and was **REFUSED** at the regression gate,
which named the three real sqlparse tests it broke.

## Results

### Benchmark

<p align="center">
  <img src="docs/assets/03-benchmark.png" alt="Benchmark table comparing Cerberus with an ungated baseline" width="860">
</p>

*This screenshot is the core quantitative claim. It compares Cerberus with the obvious alternative — approving any
patch whose own test passes — on the same frozen cases, scored by hidden tests that neither side ever sees.*

| | Cerberus | Approve-if-own-test-passes |
| --- | --- | --- |
| Correct fixes approved | 7 of 7 | 7 of 7 |
| Wrong patches approved | **4 of 11 (36%)** | 13 of 20 (65%) |
| Bad patches refused with the right reason | 12 of 17 | — |
| Approved code that fails hidden tests | 4 | 7 |

The benchmark is 24 seeded cases — correct fixes, plausible-but-wrong fixes, regressions, scope leaks, test
weakening, non-reproducible issues, invalid reproduction tests — frozen by a SHA-256 manifest so it cannot drift.
Cerberus cut wrong approvals from 13 to 4 without rejecting a single correct fix. The four it still approves pass
every visible test; catching those needs tests that describe the missing behaviour, which is why the benchmark
measures them instead of hiding them. Full report: [`v1-docker.md`](autonomous-pr-fixer/benchmark/reports/v1-docker.md).

### Real open-source projects

<p align="center">
  <img src="docs/assets/04-external-repositories.png" alt="Verdicts on real bugs in sqlparse, boltons and more-itertools" width="860">
</p>

*This screenshot shows the system working on code it was not written against. Each row is a real bug pinned to its
upstream fix commit, with the project's own test suite (492, 445 and 679 tests) deciding regressions.*

**11 of 11** runs ended in the expected verdict: every maintainer fix admitted, both as a patch and as the
unmodified upstream commit, and every bad variant refused with the right code — `REGRESSION`, `SCOPE_VIOLATION`,
`GREEN_NOT_REACHED`, `RED_INVALID_TEST`, `RED_NOT_FAILING`. Real-world validation also surfaced two edge cases that
synthetic tests could not; both are now handled and locked in by regression tests. Full report:
[`evaluation/external/report.md`](autonomous-pr-fixer/evaluation/external/report.md).

### Every refusal names its reason

<p align="center">
  <img src="docs/assets/05-refusal-codes.png" alt="Table of refusal codes and what each means" width="860">
</p>

*This screenshot is the vocabulary of the gate. A refusal is only useful if the author can act on it, so every
refusal carries a code that names the exact gate and reason, and the same code is recorded in `run.json` and in the
GitHub Check Run.*

## Security model

<p align="center">
  <img src="docs/assets/06-security-model.png" alt="Summary of the Docker sandbox and credential protections" width="860">
</p>

*This screenshot summarises why it is safe to run someone else's repository code: Cerberus executes untrusted code
by design, so the sandbox is as much the product as the gates are.*

- **Two-phase Docker sandbox.** Dependency installation runs in a networked container that is committed to an image;
  everything after runs offline with `--network none`, all capabilities dropped, `no-new-privileges`, memory, CPU and
  process limits, and an in-container kill timeout.
- **Fails closed.** No Docker, no daemon, or no image ends the run in `ERROR`. There is no silent host fallback.
- **No credentials cross the boundary.** Containers receive four fixed environment variables; the GitHub token never
  enters the sandbox, argv, or a remote URL.
- **Symlink-safe host access.** The host never follows a symlink inside the workspace, so a link planted by repository
  code cannot redirect a harness write outside it.
- **No stragglers.** No process outlives its command, and a container orphaned by a killed process stops and removes
  itself.
- **Never merges.** Cerberus opens draft pull requests or reports a Check Run; merging stays with a human.

These properties are verified from inside real containers by
[`tests/test_docker_integration.py`](autonomous-pr-fixer/tests/test_docker_integration.py).

## Quick start

**Requirements:** Python 3.11+, Git, and Docker.

```bash
cd autonomous-pr-fixer
python -m pip install -r requirements.txt
docker build -t cerberus-sandbox:py3.11 sandbox/
python main.py --demo
```

Run the test suite, and the real-repository check shown in the demo:

```bash
python -m pytest -q
python evaluation/external_repos.py --only sqlparse-332
```

### Verify your own patch

```bash
python main.py --repo /path/to/git/repo \
  --issue 42 --title "parse_date fails on leap years" \
  --body "parse_date('2024-02-29') raises ValueError at dates/parse.py:18" \
  --repro-test path/to/test_reproduce.py \
  --patch path/to/fix.diff
```

### Verify an existing change, such as a pull-request branch

```bash
python main.py --repo /path/to/git/repo \
  --title "parse_date fails on leap years" --body "..." \
  --repro-test path/to/test_reproduce.py \
  --base main --head fix-branch
```

### Let a coding agent write the fix

```bash
python main.py --repo /path/to/git/repo --title "..." --body "..." \
  --repro-test path/to/test_reproduce.py --agent claude-code
```

The agent works in a throwaway copy of the code with file tools only — no shell — so it cannot run the tests or
decide that its own patch is good. Cerberus judges the diff it leaves behind exactly like any other patch.

<details>
<summary><b>Patch sources</b></summary>

Exactly one source is used per run; the gates treat them all the same.

| Source | Flag | Notes |
| --- | --- | --- |
| Diff file | `--patch fix.diff` | human-written or produced elsewhere |
| Existing change | `--base REF --head REF` | e.g. a pull-request branch |
| External coding agent | `--agent claude-code` or `--agent-command "..."` | runs headless in a scratch copy of the base commit; edits are captured as a diff without running git on the agent's tree |
| Built-in model generator | `--use-llm` | single-prompt unified diff via `litellm`; needs a provider API key |

External agents run on the host under their own permission model; Cerberus sandboxes the verification of what they
produce. The `claude-code` preset allows only `Read,Edit,Write,Glob,Grep`.

</details>

<details>
<summary><b>Per-repository configuration: <code>.cerberus.yml</code></b></summary>

Optional, at the repository root, read from the **base** commit before any repository code runs. A patch that edits it
is refused; unknown keys and invalid values end the run in `ERROR`.

```yaml
version: 1
setup:                                  # replaces detected install commands (runs in the networked setup phase)
  - python -m pip install -e ".[test]"
test:
  command: python -m pytest -q          # pytest commands get --junitxml appended
  # command: tox -e py311 -- --junitxml={junit_xml}   # or use the placeholder
  # report: build/junit.xml                            # or read a report the command writes itself
  exclude:                              # JUnit test IDs (fnmatch) ignored by baseline and regression
    - "tests.test_network::*"
scope:
  allowed_paths: ["src/*"]              # fnmatch globs; default is the localized file
  max_files: 3
  max_lines: 200
  allow_test_modifications: false
budgets:
  patch_attempts: 5
  red_runs: 3
  green_runs: 3
  command_timeout_seconds: 600
```

The regression gate accepts any test runner that writes JUnit XML; RED and GREEN are Python/pytest.

</details>

<details>
<summary><b>GitHub App and publishing</b></summary>

```bash
cd autonomous-pr-fixer
uvicorn github.webhook_handler:app --host 127.0.0.1 --port 8000
```

The service exposes only `GET /health` and `POST /webhook` and runs as a GitHub App. Required permissions: Checks,
Pull requests, Issues and Contents (read & write), Metadata (read). Workflows permission is not requested.

| Trigger | Who | What happens |
| --- | --- | --- |
| Add the `cerberus` label to a PR | user with write access | verify the PR, report a Check Run |
| New commits on a labelled PR | anyone who can push to it | re-verify |
| Comment `/cerberus verify` on a PR | user with write access | verify the PR |
| Comment `/cerberus repair` on an issue | user with write access | repair with a configured patch source; draft PR only if publishing is enabled |

HMAC-verified deliveries (fails closed), idempotent delivery handling in SQLite, per-repository serialization, an
hourly run cap, a wall-clock timeout per run, and bot events ignored. Publishing happens only for an admitted run in
the Docker sandbox: the verified diff is applied to a fresh host-side clone and opened as a **draft** PR, with the
token passed through environment configuration only. Diffs touching `.git/`, `.cerberus/` or `.github/workflows/` are
refused.

</details>

<details>
<summary><b>Environment variables</b></summary>

Read directly from the process; `.env` is not loaded automatically. See
[`.env.example`](autonomous-pr-fixer/.env.example).

| Variable | Purpose |
| --- | --- |
| `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` / `_PATH` | GitHub App identity |
| `CERBERUS_DATA_DIR`, `CERBERUS_MAX_RUNS_PER_HOUR`, `CERBERUS_RUN_TIMEOUT_SECONDS` | App state and limits |
| `CERBERUS_REPAIR_PATCH_SOURCE`, `CERBERUS_PUBLISH_REPAIRS` | `/cerberus repair` source and publishing |
| `GITHUB_TOKEN`, `GITHUB_REPO_SLUG`, `GITHUB_BASE_BRANCH` | CLI publishing without the App |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `CERBERUS_MODEL` | Optional model access |
| `CERBERUS_USE_LLM`, `CERBERUS_USE_LLM_REPRO` | Enable model patching / reproduction |
| `CERBERUS_SANDBOX`, `CERBERUS_SANDBOX_IMAGE` | `docker` (default) or `host-unsafe`; image name |
| `CERBERUS_SANDBOX_MAX_LIFETIME_SECONDS` | Container self-removal deadline; default 14400 |
| `PATCH_MAX_ATTEMPTS`, `PATCH_MAX_LINES_CHANGED` | Patch-loop budget; default 5 attempts / 200 lines |
| `SANDBOX_TIMEOUT_SECONDS` | Default per-command timeout; 60 |
| `RUN_ARTIFACTS_DIR` | Where `run.json` files go; default `artifacts` |

`--unsafe-local-sandbox` runs trusted local fixtures on the host without Docker, with secrets stripped from the
environment; it can never publish.

</details>

## Project structure

```text
autonomous-pr-fixer/
├── main.py          CLI and pipeline orchestration
├── agents/          reproduction (RED/GREEN), regression, patch loop, patch sources, localization, repo setup
├── harness/         Docker sandbox, JUnit parsing, scope gate, admission controller, state machine, diff utilities
├── github/          GitHub App (auth, client, run store, routing, Check Runs), webhook handler, PR publisher
├── retrieval/       Python AST index and lexical search
├── sandbox/         Dockerfile for the sandbox image
├── benchmark/       frozen instances, hidden tests, manifest, reports
├── evaluation/      benchmark runner, external-repository runner and cases
├── examples/        demo fixture
└── tests/           231 tests, including real-Docker integration tests
docs/assets/         demo recording and screenshots used in this README
site/                static summary page
```

**Built with:** Python 3.11 · pytest · JUnit XML · Docker · Git plumbing · FastAPI · SQLite · GitHub REST API with
App JWTs · PyYAML · `cryptography`.

## Documentation

| Document | Contents |
| --- | --- |
| [`site/index.html`](site/index.html) | One-page visual summary — the source of the screenshots above; static, runs nothing |
| [`DEMO.md`](DEMO.md) | Step-by-step script for recording a 8–10 minute demo |
| [`INTERVIEW.md`](INTERVIEW.md) | Technical Q&A on every design decision |
| [`PORTFOLIO.md`](PORTFOLIO.md) | Concise project summary |
| [`CERBERUS_PROGRESS.md`](CERBERUS_PROGRESS.md) | Engineering log: phases, decisions, test history |

## Limitations and roadmap

Cerberus is a research project for Python/pytest repositories. Passing the gates is strong evidence, not proof: a
patch that passes every visible test can still be wrong, which the benchmark measures directly. The Docker sandbox
has been validated on Windows (Docker Desktop), and the GitHub App against a simulated GitHub API.

Next steps:

1. Live end-to-end runs with a real coding agent (the Claude Code integration is built).
2. The GitHub App installed on real repositories, including PRs that append tests to existing files.
3. Sandbox validation on a Linux host.
4. A larger evaluation set across more open-source projects.

## Author

**MuthuVarshith** — [@MuthuVarshith](https://github.com/MuthuVarshith)

## License

No license file is included yet; treat this repository as source-available until one is added.

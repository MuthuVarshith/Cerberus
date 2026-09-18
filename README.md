<div align="center">

# Cerberus

### Autonomous Patch Verification Gate

**Agents write patches. Cerberus decides whether a patch has earned a pull request.**

[Demo](#demo) · [How it works](#how-it-works) · [Results](#results) · [Live on GitHub](#live-on-github) ·
[Security](#security-model) · [Quick start](#quick-start) · [Use it on your repository](#use-cerberus-on-your-own-github-repository)

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
| **Live on GitHub** | **7 / 7** expected ✅ / ❌ checks on real pull requests across two repositories, posted by the Cerberus GitHub App |
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

## Live on GitHub

The Cerberus GitHub App verifying real pull requests on GitHub, with each check triggered by a
`/cerberus verify` comment and every pipeline stage streaming live in the terminal.

### VoteVault: five pull requests, recorded live

[`MuthuVarshith/VoteVault-`](https://github.com/MuthuVarshith/VoteVault-) is a Flask voting app with two real bugs:
the admin CSV export crashes (`send_file` was given text instead of bytes), and `calculate_rate` divides by zero. Five
pull requests propose fixes — two correct, three wrong in three different ways.

<p align="center">
  <img src="docs/assets/cerberus-github-live-demo.gif" alt="Time-lapse of Cerberus verifying five VoteVault pull requests: the terminal streams each pipeline while GitHub shows the results" width="1000">
</p>

*A 4-minute session compressed to 30 seconds. Left: the terminal streaming each check stage by stage — green for
passing gates, red for refusals. Right: GitHub, where each `/cerberus verify` comment turns into a ✅ or ❌ on the pull
request. **Full recording (4 min 18 s):**
[`docs/video/cerberus-github-app-full-demo.mp4`](docs/video/cerberus-github-app-full-demo.mp4).*

| Pull request | What the patch does | Cerberus on GitHub |
| --- | --- | --- |
| [#2](https://github.com/MuthuVarshith/VoteVault-/pull/2) Fix CSV export crash | sends the CSV as bytes | ✅ **Admitted** |
| [#3](https://github.com/MuthuVarshith/VoteVault-/pull/3) Make the vote export work | fixes the crash **and deletes the admin check** | ❌ **`REGRESSION`** — caught the security hole |
| [#4](https://github.com/MuthuVarshith/VoteVault-/pull/4) Guard calculate_rate | returns 0.0 for a zero period | ✅ **Admitted** |
| [#5](https://github.com/MuthuVarshith/VoteVault-/pull/5) Handle ZeroDivisionError | swallows the error and returns `None` | ❌ **`GREEN_NOT_REACHED`** — the bug is not fixed |
| [#6](https://github.com/MuthuVarshith/VoteVault-/pull/6) Simplify calculate_rate | changes the maths, then edits an existing test to match | ❌ **`SCOPE_VIOLATION`** — test weakening |

<p align="center">
  <img src="docs/assets/10-live-terminal-regression.png" alt="Split screen: the terminal shows Cerberus refusing pull request #3 with REGRESSION in red, naming test_export_votes_requires_admin; GitHub is on the right" width="1000">
</p>

*The moment the security hole is caught. Pull request #3's patch passes RED and GREEN — the export no longer crashes —
but the regression gate reruns the project's own tests and `test_export_votes_requires_admin` now fails: with the
admin check gone, anyone could download every vote. The refusal names the test, in red, as it happens.*

<p align="center">
  <img src="docs/assets/12-votevault-check-regression.png" alt="GitHub Checks tab for VoteVault pull request #3: Refused at REGRESSION, with the verified diff showing the deleted admin check" width="860">
</p>

*The same verdict as the reviewer sees it on GitHub. The evidence table shows which gates passed and which failed, and
the verified diff at the bottom shows exactly what was wrong: the three deleted lines of the admin check.*

<p align="center">
  <img src="docs/assets/11-live-terminal-scope-violation.png" alt="Split screen: the terminal shows Cerberus refusing pull request #6 with SCOPE_VIOLATION; GitHub shows an Admitted check for another pull request" width="1000">
</p>

*Test weakening, refused. Pull request #6 rounds every rate and then edits an existing test so it still passes. The
scope gate refuses any patch that changes lines of the tests it is judged by — while, on the right, a correct fix
sits admitted.*

### First live test: test-repository

The first live run used [`MuthuVarshith/test-repository`](https://github.com/MuthuVarshith/test-repository): a small checkout-pricing library
with a real off-by-one bug — an order of exactly 10 items should get the 10% bulk discount but pays full price
(`bulk_price(2.0, 10)` returns `20.0` instead of `18.0`). Two pull requests offer a fix:

- [**#1**](https://github.com/MuthuVarshith/test-repository/pull/1) — the correct one-character fix (`>` → `>=`)
  plus a new test for exactly 10 items.
- [**#2**](https://github.com/MuthuVarshith/test-repository/pull/2) — a careless fix that discounts *every* order. It
  makes the new test pass, so an agent checking only its own test would ship it.

```text
comment "/cerberus verify" on a PR ─► GitHub App webhook ─► Cerberus (signature checked)
      ─► RED → baseline → GREEN → regression → scope, in Docker ─► ✅ / ❌ Check Run on the PR
```

<p align="center">
  <img src="docs/assets/07-github-pr-trigger.png" alt="Two pull requests with a /cerberus verify comment; the commit on #1 shows a green tick, the commit on #2 a red cross" width="760">
</p>

*The trigger, as a reviewer sees it. The maintainer comments `/cerberus verify` on each pull request; about a minute
later the commit carries Cerberus's verdict — a green tick on #1, a red cross on #2. Only users with write access can
trigger a check, and bot comments are ignored.*

<p align="center">
  <img src="docs/assets/08-github-check-admitted.png" alt="GitHub Checks tab for pull request #1: Admitted, every verification gate passed, with the gate table, reproduction test and verified diff" width="860">
</p>

*Pull request #1 — **Admitted**. The Check Run is the evidence report: the bug reproduced in 3 of 3 runs, the
5-test baseline, GREEN, no test newly failing, and the exact files changed, followed by the reproduction test and the
diff that was verified. This is what a reviewer reads instead of re-running anything.*

<p align="center">
  <img src="docs/assets/09-github-check-refused.png" alt="GitHub Checks tab for pull request #2: Refused at REGRESSION, naming tests.test_discounts::test_small_orders_pay_full_price" width="860">
</p>

*Pull request #2 — **Refused at `REGRESSION`**. RED and GREEN both passed — the patch really does fix the 10-item
case — but the regression gate found that `test_small_orders_pay_full_price` now fails, and names it. This is the
refusal the gate exists for.*

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

The service exposes only `GET /health` and `POST /webhook` and runs as a GitHub App (setup:
[Use Cerberus on your own GitHub repository](#use-cerberus-on-your-own-github-repository)). Verifying pull requests
needs Checks and Issues (read & write), Contents and Pull requests (read); `/cerberus repair` with publishing enabled
also needs Contents and Pull requests (read & write). Workflows permission is never requested.

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

## Use Cerberus on your own GitHub repository

Cerberus runs as a **GitHub App** that you register under your own account and run on your own machine (or a
server). GitHub sends it an event when you ask for a check; Cerberus verifies the pull request in Docker and posts the
result back as a Check Run. This is exactly how the [live test above](#live-on-github) was run.

**What your repository needs**

- Python code with a **pytest** test suite that passes on the base branch, runnable **offline** (no database,
  network or API keys during tests).
- Dependencies installable with pip: a `requirements.txt`, `pyproject.toml` or `setup.py` (or a
  [`.cerberus.yml`](#use-cerberus-on-your-own-github-repository) `setup:` list).
- A pull request that fixes a bug and adds **exactly one new test file** that fails before the fix and passes after it.
  The App uses that file as the reproduction test. *(A PR that appends its test to an existing test file is refused
  with guidance; the CLI can still verify it with `--base/--head --repro-test`.)*
- A PR description (or linked `Fixes #N` issue) that names the function involved, e.g. *"`bulk_price(2.0, 10)` returns
  20.0 instead of 18.0"*.

**1. Register the GitHub App** — GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**:

| Field | Value |
| --- | --- |
| Homepage URL | your repository or this project's URL |
| Webhook → Active | ✅ |
| Webhook URL | a [smee.io](https://smee.io/new) channel URL (for a laptop) or your server's `https://…/webhook` |
| Webhook secret | a long random string you keep private |
| Repository permissions | **Checks:** Read & write · **Issues:** Read & write · **Contents:** Read-only · **Pull requests:** Read-only · Metadata: Read-only (automatic) |
| Subscribe to events | **Issue comment**, **Pull request** |
| Where can it be installed | Only on this account |

Everything else stays empty or at *No access*. After creating it, note the **App ID** and click **Generate a private
key** (a `.pem` file downloads — keep it private and never commit it).

**2. Install the App** — on the App's page, **Install App** → your account → **Only select repositories** → pick the
repositories Cerberus may check.

<p align="center">
  <img src="docs/assets/13-github-app-installation.png" alt="GitHub App installation page: read access to code, metadata and pull requests; read and write access to checks and issues; installed on two selected repositories" width="560">
</p>

*The installed App, as used for the live demos above: read-only access to code and pull requests, write access only to
checks and issues, limited to the two repositories selected.*

**3. Run Cerberus** — Docker must be running and the sandbox image built
(`docker build -t cerberus-sandbox:py3.11 autonomous-pr-fixer/sandbox/`).

On Windows, one command starts the smee relay and the service, asks for the App ID, key and secret (the secret as
hidden input), and shows each check's pipeline live in the terminal:

```bash
powershell -ExecutionPolicy Bypass -File tools/github-app/start_cerberus_app.ps1
```

On macOS or Linux, the same thing by hand:

```bash
export GITHUB_APP_ID=123456 GITHUB_APP_PRIVATE_KEY_PATH=~/Downloads/your-app.private-key.pem
read -rs GITHUB_WEBHOOK_SECRET && export GITHUB_WEBHOOK_SECRET
python tools/github-app/smee_forward.py https://smee.io/YOUR-CHANNEL http://127.0.0.1:8000/webhook &
cd autonomous-pr-fixer && python -m uvicorn github.webhook_handler:app --host 127.0.0.1 --port 8000
```

[`tools/github-app/smee_forward.py`](tools/github-app/smee_forward.py) is a standard-library relay (no Node.js
needed) that preserves GitHub's HMAC signature. On a server with a public HTTPS address, skip the relay and point the
App's webhook URL straight at `/webhook`.

**4. Trigger a check** — on a pull request, either comment `/cerberus verify` or add a label named `cerberus` (new
commits on a labelled PR are re-checked automatically). Only users with write access can trigger checks.

**5. Read the result** — within a minute or two the PR's commit shows ✅ or ❌; **Details** opens the full evidence
report. Locally, each run's output is in `autonomous-pr-fixer/.cerberus-app/work/<run_id>/cli.log` and its evidence
in `.cerberus-app/artifacts/<run_id>/run.json`. Stop the service with **Ctrl+C** when you are done.

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
tools/github-app/    smee relay and one-command launcher for running the GitHub App locally
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
| [`docs/video/`](docs/video/) | Full screen recording of the live GitHub App session (4 min 18 s) |
| [`CERBERUS_PROGRESS.md`](CERBERUS_PROGRESS.md) | Engineering log: phases, decisions, test history |

## Limitations and roadmap

Cerberus is a research project for Python/pytest repositories. Passing the gates is strong evidence, not proof: a
patch that passes every visible test can still be wrong, which the benchmark measures directly. The Docker sandbox
has been validated on Windows (Docker Desktop), and the GitHub App on a real repository with the service running
locally behind a smee.io relay.

Next steps:

1. Live end-to-end runs with a real coding agent (the Claude Code integration is built).
2. The GitHub App verifying PRs that append their test to an existing test file, and hosted on a server.
3. Sandbox validation on a Linux host.
4. A larger evaluation set across more open-source projects.

## Author

**MuthuVarshith** — [@MuthuVarshith](https://github.com/MuthuVarshith)

## License

No license file is included yet; treat this repository as source-available until one is added.

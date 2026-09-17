# Cerberus

**A verification gate that decides whether a bug-fix patch has earned the right to become a pull request.**

Cerberus does not try to be a general coding agent. A patch can come from a person, a script, or a model; Cerberus
holds it to the same evidence: a reproduction test that fails on the unpatched code for the right reason (RED),
passes with the patch (GREEN), no test that newly fails relative to a baseline run, and a change that stays inside an
allowed scope. If any check cannot be satisfied or cannot be verified, the run is refused and the reason is recorded.

> **Status: research prototype for Python/pytest repositories.** This README describes only what the code in this
> repository does and what its tests exercise. Progress, decisions and known gaps are tracked in
> [`CERBERUS_PROGRESS.md`](CERBERUS_PROGRESS.md).
>
> Also here: [`DEMO.md`](DEMO.md) (recording script), [`INTERVIEW.md`](INTERVIEW.md) (technical Q&A),
> [`PORTFOLIO.md`](PORTFOLIO.md) (summary), and [`site/index.html`](site/index.html) — a standalone summary page
> (open it locally, or serve `site/` with GitHub Pages; it is static and runs nothing).

## The problem

**Agents write patches; Cerberus decides whether a patch has earned a PR.**

A coding agent can produce a plausible diff for almost any bug report in seconds. The expensive part is not writing
the patch — it is deciding whether the patch deserves a human's attention. An agent judging its own work is the
weakest possible evidence: it chose the change, it chose which tests to run, and "the test I wrote passes" says
nothing about the tests that already existed.

Cerberus is the reviewer's side of that exchange. It never writes the fix. It takes a patch from wherever it came
from — a person, a script, a coding agent, a model — runs it against evidence the patch's author does not control,
and produces one of three outcomes with a reason attached: `ADMITTED`, `REFUSED` with a code, or `ERROR`. The
strongest claim it makes is "these gates found this evidence", which is why refusals are as interesting as
admissions.

```text
bug report + reproduction test          patch (person / script / coding agent / model)
                   │                                    │
                   ▼                                    ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  TRIAGE → SETUP (only networked phase) → RED ×3 → BASELINE    │
   │     → LOCALIZATION → apply patch → GREEN ×3 → REGRESSION      │
   │     → SCOPE → ADMISSION                                       │
   │  everything after SETUP runs offline in a Docker container    │
   └───────────────────────────────────────────────────────────────┘
                   │
                   ▼
     ADMITTED → draft PR / Check Run      REFUSED (code) / ERROR
     evidence: artifacts/<run_id>/run.json
```

## The gates

| Gate | Passes only when | Evidence recorded |
| --- | --- | --- |
| **RED** | The reproduction test parses; imports repository code and references a symbol the issue names (when it names one); runs with a JUnit report; has no setup/collection errors; fails because of an assertion, `pytest.fail`/`DID NOT RAISE`, an exception type named in the issue, or an exception raised inside repository code; and fails identically in 3 runs. Import and syntax errors never count. | failing test IDs, exception types, runs |
| **Baseline** | The repository's pytest command produces a readable JUnit report on the unpatched commit. | test count, already-failing test IDs |
| **GREEN** | With the patch, the unmodified reproduction test passes in 3 of 3 runs, with no failures, errors or skips. A modified reproduction test is refused. | attempts, per-attempt failures |
| **Regression** | No test that passed at baseline now fails, errors, or stops running. Failures that already existed at baseline are reported, not counted. Suspected regressions are re-run on the base code; tests that fail there too are reported as flaky. Unreadable results are refused, never passed. | newly failing, pre-existing, flaky |
| **Scope** | No line of an existing test file is changed or deleted, and no `conftest.py` is touched (unless the repository allows it); every changed file — including newly created and deleted files — is inside the allowed scope (the localized file, or `.cerberus.yml` globs); at most 3 files and 200 changed lines by default. Tests *added* to an existing test file are allowed: the regression gate runs that file in its base form, so added lines (a skip marker, an early `return`) cannot change how existing tests judge the patch. | files, new/deleted files, modified and extended test files, lines, changed Python functions/classes |
| **Admission** | GREEN, regression, scope, and a non-whitespace change against the base commit all hold. | decision and reasons |

Every run ends in exactly one terminal state — `ADMITTED`, `REFUSED` (with a refusal code such as `RED_WRONG_REASON`
or `SCOPE_VIOLATION`), or `ERROR` — enforced by the transition graph in
[`harness/pipeline_state.py`](autonomous-pr-fixer/harness/pipeline_state.py), and writes
`artifacts/<run_id>/run.json`.

Cerberus never writes its own reproduction test or patch as a fallback.

## Sandbox

Repository code never runs with your credentials.

- **Docker (default).** Fails closed if Docker or the sandbox image is unavailable. Dependency installation runs in a
  networked setup container that is then committed to an image; every repair-phase command runs in a container from
  that image with `--network none`, all capabilities dropped, `no-new-privileges`, memory/CPU/PID limits, a tmpfs
  `/tmp`, an in-container kill timeout, and no host environment variables. No process outlives the command that
  started it (leftovers are killed and reaped), and the host never follows a symlink inside the workspace, so a
  link planted by repository content or code cannot redirect a harness read or write outside it.
- **`--unsafe-local-sandbox`.** Runs on the host as your user, for trusted local fixtures only: prints a warning,
  passes only an allowlisted environment (no tokens or API keys), skips dependency installation, and can never
  publish.

Workspaces are fresh clones of the source repository's committed `HEAD`; uncommitted changes are not verified. After
the clone, every git operation on the workspace runs inside the sandbox, and all comparisons use the recorded base
commit SHA rather than `HEAD`.

> Tested against a real Docker engine (Engine 29.8 in Docker Desktop 4.91 on Windows, Linux containers) by
> [`tests/test_docker_integration.py`](autonomous-pr-fixer/tests/test_docker_integration.py), which checks inside real
> containers: no network interface but loopback in the repair phase, no host environment, empty capability sets,
> `NoNewPrivs`, the 2 GiB memory and 256-PID limits, the in-container kill timeout, killed leftover processes, a
> networked setup phase whose filesystem carries into the offline container, cleanup of containers and images, fail
> closed on a missing image, and the demo admitted and a regressing patch refused inside Docker. Those tests skip when
> Docker is unavailable. Symlink refusal is unit-tested; on Linux it was run in a container standing in for a Linux
> host. A Linux or macOS host running the Docker path has not been exercised.

## Quick start

Requirements: Python 3.11+, Git, and Docker for the default sandbox.

```bash
cd autonomous-pr-fixer
python -m pip install -r requirements.txt
python -m pytest -q
docker build -t cerberus-sandbox:py3.11 sandbox/
python main.py --demo
```

Without Docker, run the demo on the host (trusted fixture only):

```bash
python main.py --demo --unsafe-local-sandbox
```

The demo copies [`examples/rate_calculator`](autonomous-pr-fixer/examples/rate_calculator) into a temporary git
repository and runs the full pipeline with a reproduction test and patch read from files; every gate executes.

### Verify your own patch

```bash
python main.py --repo /path/to/git/repo \
  --issue 42 --title "parse_date fails on leap years" \
  --body "parse_date('2024-02-29') raises ValueError at dates/parse.py:18" \
  --repro-test path/to/test_reproduce.py \
  --patch path/to/fix.diff
```

### Verify an existing change (for example a pull request branch)

```bash
python main.py --repo /path/to/git/repo \
  --title "parse_date fails on leap years" --body "..." \
  --repro-test path/to/test_reproduce.py \
  --base main --head fix-branch
```

The workspace is the `--base` commit and the candidate patch is `git diff base head`. With no `.cerberus.yml` scope,
any file may change (within file/line limits). New tests, including tests appended to an existing test file, are
allowed; changing or deleting existing test lines is refused.

### Patch sources

The gate does not care where a patch comes from. Exactly one source is used per run:

| Source | Flag | Notes |
| --- | --- | --- |
| Diff file | `--patch fix.diff` | human-authored or produced elsewhere |
| Existing change | `--base REF --head REF` | e.g. a PR branch |
| External coding agent | `--agent claude-code` or `--agent-command "..."` | runs headless in a scratch copy of the base commit; its edits are captured as a diff without running git on the agent's tree |
| Built-in model generator | `--use-llm` | single-prompt unified diff via `litellm` |

External agents run **on the host, outside Cerberus's sandbox**, under their own permission model and credentials;
Cerberus sandboxes the verification of what they produce. The `claude-code` preset allows only file tools
(`Read,Edit,Write,Glob,Grep`), no shell. The prompt is sent on stdin; `{prompt_file}` and `{workdir}` are substituted in
`--agent-command`. The external agent path is tested with a scripted stand-in agent; it has not been run against a real
Claude Code or Codex session in this repository.

### Model-generated reproduction tests (optional)

```bash
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY / GEMINI_API_KEY
python main.py --repo /path/to/repo --title "..." --body "... at path/to/file.py:12" --use-llm-repro --use-llm
```

Model modes are off unless requested; requested without a key, the run is refused. Model output is held to the same
gates.

## Per-repository configuration: `.cerberus.yml`

Optional, at the repository root, read from the **base** commit before any repository code runs. A patch that edits it
is refused. Unknown keys and invalid values end the run in `ERROR`.

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

The regression gate accepts any test runner that writes JUnit XML (via `{junit_xml}` or `report`). The RED/GREEN gates
and patch generation remain Python/pytest-only.

## Publishing

Publishing happens only for an admitted run, in `--mode github` without `--dry-run`, with the Docker sandbox. The
verified diff is applied to a fresh host-side clone at the verified base commit and pushed; the token is passed to git
through environment configuration, never through argv, a remote URL, or the sandbox. Pull requests are opened as
drafts. Diffs touching `.git/`, `.cerberus/` or `.github/workflows/` are refused. Cerberus never merges.

## GitHub App

```bash
cd autonomous-pr-fixer
uvicorn github.webhook_handler:app --host 127.0.0.1 --port 8000
```

The service exposes only `GET /health` and `POST /webhook` and runs as a GitHub App (you register the App; Cerberus
does not). Required App permissions: Checks (read & write), Pull requests (read & write), Issues (read & write),
Contents (read & write), Metadata (read). Workflows permission is not requested. Subscribe to `pull_request` and
`issue_comment` events.

| Trigger | Who | What happens |
| --- | --- | --- |
| Add the `cerberus` label to a PR | user with write access | verify the PR, report a Check Run |
| New commits on a labelled PR | anyone who can push to it | re-verify |
| Comment `/cerberus verify` on a PR | user with write access | verify the PR |
| Comment `/cerberus repair` on an issue | user with write access | only if the server has a patch source configured; reproduction test from a ```` ```python ```` block; draft PR only if publishing is enabled |

To verify a PR, Cerberus uses the **single new test file the PR adds** as the reproduction test: it must fail on the
base commit and pass with the PR. A PR that adds zero or several test files is refused with guidance, without running
anything; this includes the common case of a PR that appends its test to an existing test file, which the CLI can
verify (`--base/--head` with `--repro-test`) but the App cannot yet. Issue text comes from the issue linked with
"Fixes #N", otherwise from the PR.

Operational properties: HMAC-verified deliveries (fails closed); deliveries claimed once in a SQLite store (idempotent
across restarts); every run recorded from queue to terminal state and linked to its `run.json` and Check Run; runs in
one repository serialized; runs per hour capped; each run is a CLI subprocess with a wall-clock timeout; interrupted
runs marked `ERROR` on restart; bot events ignored; the App refuses to run without the Docker sandbox; never merges.

> The App is tested against a fake GitHub API (routing, authorization, idempotency, rate limits, timeouts, and an
> end-to-end PR verification through the real CLI). It has not been installed on a real GitHub repository.

## Configuration

Environment variables are read directly from the process; `.env` is not loaded automatically. See
[`.env.example`](autonomous-pr-fixer/.env.example).

| Variable | Purpose |
| --- | --- |
| `GITHUB_WEBHOOK_SECRET`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` / `_PATH` | GitHub App identity; the secret is required for `/webhook` |
| `CERBERUS_DATA_DIR`, `CERBERUS_MAX_RUNS_PER_HOUR`, `CERBERUS_RUN_TIMEOUT_SECONDS` | App state location and limits |
| `CERBERUS_REPAIR_PATCH_SOURCE`, `CERBERUS_PUBLISH_REPAIRS` | `/cerberus repair`: `none`/`llm`/`claude-code`; open draft PRs or report only |
| `GITHUB_TOKEN`, `GITHUB_REPO_SLUG`, `GITHUB_BASE_BRANCH` | CLI publishing with a token (without the App) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `CERBERUS_MODEL` | Optional model access |
| `CERBERUS_USE_LLM`, `CERBERUS_USE_LLM_REPRO` | Enable model patching / reproduction |
| `CERBERUS_SANDBOX`, `CERBERUS_SANDBOX_IMAGE` | `docker` (default) or `host-unsafe`; image name |
| `CERBERUS_SANDBOX_MAX_LIFETIME_SECONDS` | A container stops and removes itself after this long even if the process that started it was killed; default 14400 (the App uses its run timeout + 300) |
| `PATCH_MAX_ATTEMPTS`, `PATCH_MAX_LINES_CHANGED` | Patch-loop budget, default 5 attempts / 200 lines |
| `SANDBOX_TIMEOUT_SECONDS` | Default per-command timeout, 60 |
| `RUN_ARTIFACTS_DIR` | Where `run.json` files go, default `artifacts` |

## Evaluation

The benchmark asks one question: **does the gate refuse patches that should not become PRs, compared with shipping
whatever passes its own reproduction test?**

```bash
python evaluation/benchmark.py --validate --unsafe-local-sandbox   # authoring integrity; does not run Cerberus
python evaluation/benchmark.py --unsafe-local-sandbox              # scored run on the frozen set
```

- **Instances:** 24 seeded cases over three small libraries (`benchmark/instances/v1.yml`): correct fixes,
  plausible-but-wrong fixes, regressions, scope leaks, test weakening, non-reproducible issues, invalid reproduction
  tests, and an ineffective patch.
- **Ground truth:** hidden specification tests (`benchmark/hidden/`) that Cerberus never sees decide whether a
  candidate is correct.
- **Baseline:** measured, not stipulated: on the same instances, admit whenever the candidate applies and the
  reproduction test passes with it.
- **Integrity:** `--validate` checks each instance independently of Cerberus (hidden tests pass on the clean template
  and fail on the seeded bug; reproduction tests fail on the bug and pass on the reference fix). The set is frozen
  by a SHA-256 manifest; scored runs refuse to start if it changed.
- **Limits:** the instances are small and were written by the Cerberus developer, and no model or external agent is
  in the loop. This measures gate decisions on known cases, not repair ability on real repositories. The only
  real-world cases are the three external bugs below.

Reports of record: [`benchmark/reports/v1-docker.md`](autonomous-pr-fixer/benchmark/reports/v1-docker.md) (commit
`e394d03`, Docker sandbox) and the earlier [`benchmark/reports/v1.md`](autonomous-pr-fixer/benchmark/reports/v1.md)
(commit `271bf37`, host-unsafe sandbox); both use pre-written candidate diffs on the 24 frozen instances and made the
same decision on every instance. Headline numbers:

| | Cerberus | Ungated baseline |
| --- | --- | --- |
| Patches admitted | 11 | 20 |
| Admitted but should have been refused | 4 of 11 (95% CI 15%–65%) | 13 of 20 (95% CI 43%–82%) |
| Admitted code failing hidden tests | 4 | 7 |
| Correct fixes admitted | 7 of 7 | 7 of 7 |

All four false admissions are plausible-but-wrong patches that pass every visible test and the reproduction test; the
gates cannot see those mistakes. With n = 24, the intervals are wide: these numbers show the gates behave as designed
on known failure modes, not how often they would help on real projects.

### External repositories

```bash
python evaluation/external_repos.py            # Docker only; clones the pinned repositories
```

Three real bugs in open-source projects Cerberus was not written against, each pinned to its upstream fix commit
([`evaluation/external/cases.json`](autonomous-pr-fixer/evaluation/external/cases.json)): sqlparse `f66d12c`,
boltons `ead236e`, more-itertools `cca3294`. The reproduction test is the test the fix commit added. Variants are the
upstream source fix, the unmodified upstream commit verified as an existing change, and hand-made wrong inputs.

Report of record: [`evaluation/external/report.md`](autonomous-pr-fixer/evaluation/external/report.md) (commit
`e394d03`, Docker): **11 of 11 runs ended in the expected state and refusal code.**

| Variant | Result |
| --- | --- |
| Upstream fix (3 bugs), upstream commit as an existing change (3 bugs) | 6 × `ADMITTED`; suites of 492, 445 and 679 tests, none newly failing |
| sqlparse: fix that also strips identifier quoting | `REGRESSION`, naming the 3 upstream tests it breaks |
| sqlparse: same patch plus edits to those 3 tests | `SCOPE_VIOLATION` (existing test lines changed) |
| boltons: fix that raises for every `factor=1.0` | `GREEN_NOT_REACHED` |
| more-itertools: reproduction test with a non-existent import | `RED_INVALID_TEST` |
| more-itertools: test that already passes on the bug | `RED_NOT_FAILING` |

These runs exposed two defects the synthetic benchmark could not (real fixes that append a test to an existing test
file were refused; a diff ending in a blank context line was corrupted before `git apply`), and the move to a real
Docker engine prompted a review that found host-side harness file access followed workspace symlinks. All three are
fixed and covered by tests. Three bugs chosen
for fast suites are a smoke test on foreign code, not a representative sample.

### Real coding agents

**Not yet done.** The `--agent claude-code` path is exercised only by a scripted stand-in agent in the test suite
(`tests/test_patch_sources.py`), which produces edits the same way a real agent would but without a model. A run
against a real headless Claude Code session needs an authenticated CLI on the machine
(`claude` → `/login`); on this development machine `claude -p` answers `Not logged in`, so no such run has happened
and nothing in this repository claims otherwise. The intended command, once logged in, is in [`DEMO.md`](DEMO.md).

What such a run would and would not prove: it would show that a patch written by an agent with no access to the
verifier survives (or fails) the same gates as a human patch. It would not make the gates stronger — a plausible but
semantically wrong agent patch that passes every visible test is admitted, exactly as the benchmark measures.

The older smoke runner (`python evaluation/smoke_runner.py`) still exercises five scripted scenarios.

## Known limitations

- **Python and pytest for RED/GREEN.** The regression gate needs JUnit XML: pytest commands get it automatically; other
  runners need `{junit_xml}` or `test.report` in `.cerberus.yml`, otherwise they are refused as unverifiable. The
  sandbox image contains only Python, git and pytest.
- **Dependency detection** covers `pyproject.toml`/`setup.py` (with `test`/`dev` extras), `requirements*.txt`;
  anything else needs `setup:` in `.cerberus.yml`. No private indexes or services such as databases.
- **Not yet exercised against real services:** the GitHub App (fake API only), external coding agents (scripted
  stand-in only), and the Docker sandbox on a Linux or macOS host (it has run on Docker Desktop for Windows).
- **External validation is small:** three bugs in three pure-Python libraries, chosen for fast test suites. It shows
  the gates work on code Cerberus was not written against, not how often they help on real projects.
- **Plausible-but-wrong patches pass the gates** when the visible tests don't cover the mistake; the benchmark measures
  this rather than hiding it.
- **The RED gate's relevance check is heuristic** (imports and identifiers written as code in the issue). A test can
  satisfy every rule and still encode the wrong expected behaviour; that needs human review.
- **Passing gates is not proof of correctness.** Tests only cover what they cover.
- **Scope policy** is the single localized file; localization is keyword/AST matching and is not measured.
- **Repository code controls its own test run**, so a deliberately malicious repository could forge JUnit results.
  The gates verify patches to trusted-but-buggy repositories, not adversarial ones.

## Repository layout

```text
autonomous-pr-fixer/
├── main.py          CLI and pipeline orchestration
├── agents/          triage, reproduction (RED/GREEN), localization, patch loop, patch sources, regression, repo setup, model generators
├── harness/         sandbox, JUnit parsing, scope gate, admission, state machine, diff utilities, .cerberus.yml, run artifacts, config
├── retrieval/       Python AST index and lexical search
├── github/          GitHub App (auth, client, run store, routing, Check Run reports), webhook handler, PR publisher
├── sandbox/         Dockerfile for the sandbox image
├── examples/        demo fixture (rate_calculator) and preserved VoteVault scenario
├── benchmark/       frozen instance set, templates, hidden tests, manifest, reports
├── evaluation/      benchmark runner, external-repository runner and cases, smoke scenarios, metrics reporter
└── tests/
```

## Future work

In the order the evidence argues for, not the order that is most fun to build:

1. **Run a real coding agent through the gate** (above) and record what it produces.
2. **Exercise the Docker sandbox on a Linux host**, where the symlink and permission defences actually bite.
3. **Install the GitHub App on a real repository.** The App is written and tested against a fake API; the first real
   installation will find things a fake cannot.
4. **Let the App verify PRs that append a test to an existing file** — the shape of most real fixes. The CLI already
   does this; the App still insists on exactly one new test file.
5. **A larger, less biased evaluation set.** 24 author-written instances and three external bugs are enough to show
   the gates behave as designed, not enough to estimate how often they help.
6. **Measure repair quality** (`Phase 3` in [`CERBERUS_PROGRESS.md`](CERBERUS_PROGRESS.md)) only after the above:
   improving a repair loop before verification is trustworthy optimises the wrong end of the system.

## CI

GitHub Actions runs the test suite on Ubuntu and Windows with Python 3.11 and a hard-coded-secret scan:
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Authors

- **MuthuVarshith** — author and maintainer ([@MuthuVarshith](https://github.com/MuthuVarshith))

## License

No license file is included. Treat this repository as source-available until a license is added.

# Cerberus

**A verification gate that decides whether a bug-fix patch has earned the right to become a pull request.**

Cerberus does not try to be a general coding agent. A patch can come from a person, a script, or a model; Cerberus
holds it to the same evidence: a reproduction test that fails on the unpatched code (RED), passes with the patch
(GREEN), no regressions in the repository's test suite, and a change that stays inside an allowed scope. If any
check fails, the run is refused and the reason is recorded.

> **Status: research prototype, Python/pytest repositories only.** This README describes only what the code in
> this repository does today. Work in progress and known gaps are tracked in
> [`CERBERUS_PROGRESS.md`](CERBERUS_PROGRESS.md).

## What is implemented

| Capability | State |
| --- | --- |
| RED gate: supplied or model-generated reproduction test must fail on the base commit | Implemented; accepts any non-syntax failure (see limitations) |
| GREEN gate: candidate patch must make the reproduction test pass | Implemented; an empty test command is rejected, never counted as a pass |
| Regression gate: repository test suite must pass after the patch | Implemented; not yet baseline-aware |
| Scope gate: changed files must be inside the localized boundary, ≤ 3 files, ≤ 200 lines | Implemented; tracked files only |
| Admission: all four gates as one conjunction, then publish | Implemented |
| Run record: `artifacts/<run_id>/run.json` written on every exit path, with final state, base commit, reproduction test and diff | Implemented |
| Patch sources: a diff file (`--patch`), or a model via `litellm` (`--use-llm`) | Implemented |
| Reproduction sources: a test file (`--repro-test`), or a model (`--use-llm-repro`) | Implemented |
| GitHub webhook (`/webhook`, HMAC-verified, fails closed) and PR publisher | Implemented; dry-run by default |
| Docker sandbox | Code path exists but falls back to **host execution** when Docker is unavailable — not a security boundary yet |

Cerberus never writes its own reproduction test or patch as a fallback. With no reproduction test the run is
refused at RED; with no patch source it is refused at the patch stage.

## Pipeline

```text
Issue ─► Triage ─► Reproduction ─► RED gate ─► Localization ─► Patch loop (GREEN gate)
                         │ refuse         │ refuse                     │ refuse
                         ▼                ▼                            ▼
                    run.json          run.json                     run.json

Patch loop ─► Regression gate ─► Scope gate ─► Admission ─► Evidence report ─► PR (only if admitted)
                   │ refuse          │ refuse       │ refuse
                   ▼                 ▼              ▼
               run.json          run.json       run.json
```

Every run ends in a named state (`ADMITTED`, a `REJECTED_*` state, or `ERROR`) enforced by a transition graph in
[`harness/pipeline_state.py`](autonomous-pr-fixer/harness/pipeline_state.py).

## Quick start

Requirements: Python 3.11+, Git.

```bash
cd autonomous-pr-fixer
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1    macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
```

### Run the deterministic demo

```bash
python main.py --demo
```

The demo copies [`examples/rate_calculator`](autonomous-pr-fixer/examples/rate_calculator) into a temporary git
repository and runs the full pipeline. Its reproduction test and patch are read from files in that directory, so the
run is offline and repeatable; every gate still executes. No PR is created.

### Verify your own patch

```bash
python main.py --repo /path/to/git/repo \
  --issue 42 --title "parse_date fails on leap years" \
  --body "parse_date('2024-02-29') raises ValueError at dates/parse.py:18" \
  --repro-test path/to/test_reproduce.py \
  --patch path/to/fix.diff
```

The repository must be a git repository with at least one commit. The patch must be a unified diff that applies with
`git apply`.

### Model-generated tests or patches (optional)

```bash
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY / GEMINI_API_KEY
python main.py --repo /path/to/repo --title "..." --body "... at path/to/file.py:12" --use-llm-repro --use-llm
```

Model modes are off unless requested. If a model mode is requested without a key, the run is refused.

## GitHub webhook service

```bash
cd autonomous-pr-fixer
uvicorn github.webhook_handler:app --host 127.0.0.1 --port 8000
```

The service exposes only `GET /health` and `POST /webhook`. Deliveries must carry a valid `X-Hub-Signature-256`
for `GITHUB_WEBHOOK_SECRET`; an unset secret rejects everything. Issues labelled `bug`/`auto-fix`, or comments
containing `@bot-fix`, dispatch the pipeline against the checkout in `CERBERUS_REPO_DIR`. Runs are dry-run unless
`CERBERUS_WEBHOOK_DRY_RUN=0`. Webhook runs have no reproduction test or patch source unless model modes are
enabled, so without them they are refused.

## Configuration

Environment variables are read directly from the process; a `.env` file is not loaded automatically. See
[`.env.example`](autonomous-pr-fixer/.env.example).

| Variable | Purpose |
| --- | --- |
| `GITHUB_TOKEN`, `GITHUB_REPO_SLUG`, `GITHUB_BASE_BRANCH` | Live PR publishing |
| `GITHUB_WEBHOOK_SECRET` | Required HMAC secret for `/webhook` |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `CERBERUS_MODEL` | Optional model access |
| `CERBERUS_USE_LLM`, `CERBERUS_USE_LLM_REPRO` | Enable model patching / reproduction |
| `CERBERUS_REPO_DIR`, `CERBERUS_WEBHOOK_DRY_RUN` | Webhook dispatch target and publishing switch |
| `PATCH_MAX_ATTEMPTS`, `PATCH_MAX_LINES_CHANGED` | Patch-loop budget, default 5 attempts / 200 lines |
| `SANDBOX_TIMEOUT_SECONDS` | Per-command timeout, default 60 |
| `RUN_ARTIFACTS_DIR` | Where `run.json` files go, default `artifacts` |

## Evaluation

```bash
python evaluation/smoke_runner.py
```

Five synthetic repositories with scripted patches exercise the real gates (a clean fix, a fix found on retry, a
non-reproducible issue, a regression, and a scope leak). This measures gate behaviour on known cases. It is not a
benchmark of repair ability, and Cerberus has no benchmark results yet.

## Known limitations

- **Sandbox:** without Docker, commands run on the host as the current user with the host environment. Do not run
  untrusted repositories this way.
- **RED gate:** any non-zero pytest exit other than a syntax error counts as reproduction, including import and
  collection errors.
- **Regression gate:** compares against "all tests pass", not against a baseline run, so repositories with already
  failing tests are always refused. Output is parsed from console text.
- **Scope gate:** newly created (untracked) files are not counted.
- **Localization:** keyword and AST-symbol matching over Python files; accuracy is not measured.
- **Webhook idempotency** is in memory and lost on restart.
- **Python and pytest only.**

## Repository layout

```text
autonomous-pr-fixer/
├── main.py          CLI and pipeline orchestration
├── agents/          triage, reproduction, localization, patch loop, regression, repo setup, model generators
├── harness/         sandbox, state machine, admission controller, diff utilities, run artifacts, config
├── retrieval/       Python AST index and lexical search
├── github/          webhook handler and PR publisher
├── examples/        demo fixture (rate_calculator) and preserved VoteVault scenario
├── evaluation/      smoke scenarios and metrics reporter
└── tests/
```

## CI

GitHub Actions runs the test suite on Ubuntu and Windows with Python 3.11 and a hard-coded-secret scan:
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Authors

- **MuthuVarshith** — author and maintainer ([@MuthuVarshith](https://github.com/MuthuVarshith))

## License

No license file is included. Treat this repository as source-available until a license is added.

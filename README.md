# Cerberus

![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat-square&logo=python)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)
![Tests](https://img.shields.io/badge/Tests-111-brightgreen?style=flat-square)
![Status](https://img.shields.io/badge/Status-Active-success?style=flat-square)

## Verification-first autonomous software repair

Cerberus is a Python research prototype for evaluating autonomous bug-fixing agents. A model may propose a reproduction test or patch, but it cannot approve its own work. The harness executes the candidate in an isolated workspace, checks the result with programs and exit codes, and only considers a pull request eligible when every admission gate passes.

> **Status:** the verification harness and offline demo are implemented and covered by 111 tests. The default CLI is a deterministic demonstration, not a general-purpose autonomous fixer. Real LLM calls, Docker isolation, GitHub publishing, and SWE-bench execution are optional or unverified in this checkout.

## Tech Stack

### Core Technologies
- **Language:** Python 3.11+
- **Testing:** pytest (111+ tests), parametrized test suites
- **CI/CD:** GitHub Actions (Linux & Windows)
- **Containerization:** Docker (optional, for sandboxing)
- **Code Analysis:** AST parsing, Python introspection
- **Version Control:** Git, GitHub API

### Key Libraries & Frameworks
- **LLM Integration:** LiteLLM (Anthropic Claude, OpenAI GPT models)
- **AST Analysis:** Python `ast`, `inspect` modules
- **Process Management:** subprocess, multiprocessing
- **Data Structures:** dataclasses, Pydantic (config validation)
- **Logging:** structured logging with JSON artifacts
- **GitHub Integration:** PyGithub, GitHub REST API

### Features & Capabilities
✅ Autonomous bug reproduction and localization  
✅ Patch generation with LLM integration  
✅ Regression testing with configurable test suites  
✅ Blast radius analysis for change scope validation  
✅ Docker-based sandboxing for untrusted code  
✅ Four-gate admission controller for PR eligibility  
✅ GitHub webhook integration for CI/CD  
✅ SWE-bench evaluation support  
✅ Comprehensive audit trails in JSON artifacts

## What Cerberus verifies

The admission controller evaluates a conjunction of four independent gates:

| Gate | Question | Evidence |
| --- | --- | --- |
| 1. Reproduction | Did the issue fail before the patch and pass after it? | Pre-patch and post-patch pytest exit codes |
| 2. Regression | Did the configured regression suite remain green? | Test process exit code and parsed counts |
| 3. Blast radius | Did the change stay within the allowed file and size boundary? | Git diff statistics and structural analysis |
| 4. Real diff | Is there a non-empty, attributable change? | Workspace diff content and changed-file data |

All four must be true. A failed gate becomes a named terminal rejection state; it is not silently converted into a successful run. Publication is called only after admission succeeds.

## Pipeline

```text
Issue or webhook
      |
      v
Triage -> Reproduction / RED gate -> Localization -> Patch loop
                                                        |
                                                        v
                              Regression -> Blast radius -> Admission
                                                               |
                                           +-------------------+------------------+
                                           |                                      |
                                           v                                      v
                                   Eligible for PR                       Rejected and logged
```

The main pipeline has eight stages:

1. Triage the issue and identify language, framework, error signatures, and a working branch.
2. Create a focused reproduction test. The RED gate requires it to fail on the unmodified workspace.
3. Index and localize likely files and symbols using AST and lexical retrieval.
4. Apply a deterministic demo repair or an optional LLM-generated unified diff.
5. Re-run the reproduction test and retry failed model patches within policy limits.
6. Run the configured regression command.
7. Check changed files and line limits against the repair boundary.
8. Evaluate the four gates, write an audit artifact, and optionally publish a PR.

## Repository layout

```text
.
├── README.md
├── .github/workflows/ci.yml       Cross-platform tests and secret-pattern audit
└── autonomous-pr-fixer/
    ├── main.py                     CLI and pipeline orchestration
    ├── agents/                     Triage, reproduction, localization, patch, regression
    ├── harness/                    Sandbox, state machine, gates, diffs, artifacts, config
    ├── retrieval/                  Python AST and lexical retrieval
    ├── github/                     Webhook listener and PR publisher
    ├── evaluation/                 Smoke, baseline, ablation, and metrics runners
    ├── tests/                      Automated test suite
    ├── artifacts/                  Per-run JSON audit output
    ├── requirements.txt
    └── .env.example
```

## Requirements

- Python 3.11 or newer
- Git
- Docker is optional, but required when the repository or generated code is untrusted
- An Anthropic or OpenAI API key only for LLM-enabled modes
- GitHub credentials only for live webhook or PR publishing

The fallback sandbox uses a temporary workspace and kills timed-out process trees, but executes processes as the current user. It is a workspace-isolation convenience, not a security boundary.

## Installation

From the repository root:

```bash
cd autonomous-pr-fixer
python -m venv .venv
```

Activate the environment:

```bash
# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS/Linux
source .venv/bin/activate
```

Install dependencies and run the tests:

```bash
python -m pip install -r requirements.txt
python -m pytest tests/ -q
```

On Windows, `py -3` can be used instead of `python` when the Python launcher is available.

## Run the offline demo

The default path uses a known, deterministic `calculate_rate` repair. It demonstrates the gates without spending money or requiring network access:

```bash
python main.py --demo --issue 101 \
  --title "Divide by zero in rate_calculator" \
  --body "calculate_rate(10, 0) throws ZeroDivisionError" \
  --mode local --dry-run
```

The CLI also accepts `--repo PATH`, `--issue NUMBER`, `--title TEXT`, `--body TEXT`, and `--mode local|github`. Local mode never publishes a PR. The current demo regression step runs `tests/test_sandbox.py`; the full project suite is run separately with `python -m pytest tests/ -q`.

## Optional LLM modes

LLM functionality is opt-in. The harness uses `litellm`, applies model-generated unified diffs through the normal patch loop, and still uses the deterministic gates for its decision.

```bash
# Generate a patch with a provider
python main.py --use-llm --dry-run

# Generate the reproduction test as well
python main.py --use-llm --use-llm-repro --dry-run
```

Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` before running. If a requested key is absent, the CLI reports the missing credential and falls back to the deterministic path. Provider usage is reported only when a real model transport supplies usage data; offline runs report no token measurement.

## GitHub webhook service

The FastAPI app exposes:

- `GET /health`
- `POST /webhook`

Start it from `autonomous-pr-fixer/`:

```bash
uvicorn github.webhook_handler:app --host 0.0.0.0 --port 8000
```

The handler verifies `X-Hub-Signature-256`, filters supported issue events, and deduplicates delivery IDs in memory. It dispatches only when `CERBERUS_REPO_DIR` points to a checkout. Webhook-triggered runs are dry-run by default; set `CERBERUS_WEBHOOK_DRY_RUN=0` only after validating the deployment, credentials, checkout, and target repository.

Never enable unsigned webhooks in production. `CERBERUS_ALLOW_UNSIGNED_WEBHOOKS=1` is a development escape hatch only.

## Configuration

Copy `.env.example` as a reference and export the values in the process environment. The current loader reads environment variables directly; merely creating a `.env` file does not load it automatically.

| Variable | Purpose |
| --- | --- |
| `GITHUB_TOKEN` | Token used for live push and PR creation |
| `GITHUB_WEBHOOK_SECRET` | Required HMAC secret for signed webhooks |
| `GITHUB_REPO_SLUG` | Target in `owner/repository` form |
| `GITHUB_BASE_BRANCH` | Base branch for a PR, default `main` |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Optional model credentials |
| `CERBERUS_MODEL` | Optional `litellm` model identifier |
| `CERBERUS_USE_LLM` | Enable model patch generation when `1`, `true`, or `yes` |
| `CERBERUS_USE_LLM_REPRO` | Enable model reproduction synthesis |
| `CERBERUS_REPO_DIR` | Checkout used by webhook dispatch |
| `CERBERUS_WEBHOOK_DRY_RUN` | Keep webhook runs from publishing unless set to `0` |
| `CERBERUS_ALLOW_UNSIGNED_WEBHOOKS` | Development-only unsigned webhook escape hatch |
| `PATCH_MAX_ATTEMPTS` | Maximum patch-loop attempts, default `5` |
| `PATCH_MAX_LINES_CHANGED` | Patch/blast-radius line budget, default `200` |
| `SANDBOX_TIMEOUT_SECONDS` | Command timeout, default `60` |
| `SANDBOX_NETWORK_DISABLED` | Parsed sandbox network policy setting |
| `LOG_LEVEL` | Logging level, default `INFO` |
| `RUN_ARTIFACTS_DIR` | Audit artifact directory, default `artifacts` |

Do not commit `.env` or expose tokens in shell history, logs, issues, or pull requests. Rotate any credential that has been exposed.

## Audit artifacts

Successful runs that reach the admission path write `artifacts/run_<id>/run.json`. The artifact records the pipeline history, gate results, test counts, changed files, line counts, diff hash, execution mode, and PR status. The current implementation returns early for some failures, such as missing live credentials, failed reproduction, or failed patch application, before writing that artifact; those paths are reported in the CLI output and logs.

## Evaluation

Run the measured smoke scenarios:

```bash
python evaluation/swe_bench_runner.py --subset smoke
```

The smoke runner uses five injected-bug cases and exercises the real sandbox, patch loop, regression agent, and admission controller. Its current expected profile is approximately 40% resolution, 80% reproduction, 80% regression-free, and 40% admission. These are harness measurements for synthetic cases, not SWE-bench results.

The baseline and ablation runners are useful for comparing authored scenarios:

```bash
python -m evaluation.baseline_runner
python -m evaluation.ablation_runner
```

They are derived from stipulated profiles. Real provider calls, Docker mode, live GitHub publishing, ground-truth localization accuracy, token cost, and a complete SWE-bench run have not been validated in this repository.

## Security model and limitations

- Prefer Docker mode for untrusted repositories and generated code.
- The fallback process sandbox uses `shell=True`; do not treat it as containment.
- Require and validate `GITHUB_REPO_SLUG` before enabling live publishing.
- Keep webhook secrets configured and enforce request-size limits at the reverse proxy.
- Webhook idempotency is in-process and is lost on restart or across workers.
- The default demo has hard-coded scenario shortcuts and is not a universal issue resolver.
- Blast-radius checks currently enforce changed-file and size constraints; authorized symbol-level comparison is not yet complete.
- Generated artifacts and local `.env` files should remain out of version control.

## CI

GitHub Actions runs the test suite on Ubuntu and Windows with Python 3.11 and performs a basic hard-coded-secret scan. The workflow is [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Authors & Contributors

- **MuthuVarshith** — Author & Maintainer
  - GitHub: [@MuthuVarshith](https://github.com/MuthuVarshith)

### Project History

Cerberus was developed as a research prototype to evaluate autonomous bug-fixing agents within a verification-first framework. The project emphasizes rigorous admission gates and reproducible, auditable repair processes.

### Contributing

Contributions, bug reports, and feature requests are welcome! Please open an issue or pull request.

## License

No license file is currently included. Treat this repository as source-available until a license is added.

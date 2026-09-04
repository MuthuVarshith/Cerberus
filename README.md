# Cerberus — Verification-First Autonomous Software Repair Harness

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-54%20passed%20%2F%200%20failed-brightgreen.svg)](autonomous-pr-fixer/tests/)
[![Architecture](https://img.shields.io/badge/architecture-verification--first-orange.svg)](#3-core-verification-first-idea)

**Cerberus** is a research prototype of a *verification-first* software repair harness. Its thesis: a candidate patch should never reach a Pull Request because a model claims the bug is fixed — it should reach a PR only after passing a deterministic, programmatic conjunction of four gates: the issue was **proven reproducible** (Gate 1), the fix **introduces zero regressions** (Gate 2), the change **stays inside an authorized AST blast radius** (Gate 3), and the workspace yields a **real, non-empty, attributable diff** (Gate 4).

The gate machinery, sandbox, state machine, retrieval layer, and audit trail are fully implemented and tested. The LLM patch-synthesis layer is **not** — see [Implementation Status](#1-implementation-status) before reading the evaluation section.

---

## 1. Implementation Status

This project is a **gatekeeper prototype**, not an end-to-end autonomous agent. The table below is the honest split, with the code that backs each claim.

| Subsystem | Status | Evidence |
| :--- | :--- | :--- |
| 4-gate admission conjunction | **Implemented** | [`harness/admission_controller.py`](autonomous-pr-fixer/harness/admission_controller.py) — `gate_1 and gate_2 and gate_3 and gate_4` |
| Pipeline state machine (23 states, enforced transitions) | **Implemented** | [`harness/pipeline_state.py`](autonomous-pr-fixer/harness/pipeline_state.py) |
| Sandbox isolation + hardening | **Implemented** | [`harness/docker_sandbox.py`](autonomous-pr-fixer/harness/docker_sandbox.py) |
| AST + lexical code-aware retrieval | **Implemented** | [`retrieval/`](autonomous-pr-fixer/retrieval/) |
| RED-gate reproduction verification | **Implemented** | [`agents/reproduction_agent.py`](autonomous-pr-fixer/agents/reproduction_agent.py) |
| Iterative patch loop (retry, duplicate-diff abort, minimal-patch policy) | **Implemented** | [`agents/patch_agent.py`](autonomous-pr-fixer/agents/patch_agent.py) |
| Regression suite + AST blast-radius analysis | **Implemented** | [`agents/regression_agent.py`](autonomous-pr-fixer/agents/regression_agent.py) |
| `run.json` machine-readable audit artifact | **Implemented** | [`harness/run_artifact.py`](autonomous-pr-fixer/harness/run_artifact.py) |
| HMAC-SHA256 webhook verification + idempotency | **Implemented** | [`github/webhook_handler.py`](autonomous-pr-fixer/github/webhook_handler.py) |
| GitHub PR creation (branch, push, REST call) | **Implemented** | [`github/pr_publisher.py`](autonomous-pr-fixer/github/pr_publisher.py) |
| **LLM patch generation** | **Not wired in** | No module calls a model. `litellm` is declared in `requirements.txt` but unused; `OPENAI_API_KEY` is read by `config.py` and never consumed. |
| **LLM reproduction-test synthesis** | **Not wired in** | `ReproductionAgent.build_reproduction_prompt()` builds the prompt; nothing sends it. `run_reproduction_gate()` requires the test to be passed in as an argument. |
| **`main.py` CLI repair** | **Scripted demo** | `main.py` writes a hard-coded corrected file (lines 189–207) instead of calling `PatchAgent.run_patch_loop`. It demonstrates the gates end-to-end on a known bug; it does not synthesize a fix. |
| **Webhook → pipeline dispatch** | **Stub** | `_run_repair_pipeline()` logs the event and returns. Signature checking, dedup, and `BackgroundTasks` dispatch are real; the task body is not. |

`PatchAgent`'s real retry loop *is* exercised — by [`evaluation/swe_bench_runner.py`](autonomous-pr-fixer/evaluation/swe_bench_runner.py) (live sandboxes, injected diffs) and [`tests/test_tdd_loop.py`](autonomous-pr-fixer/tests/test_tdd_loop.py).

---

## 2. Problem Statement

Autonomous coding agents edit code fluently but fail in three characteristic ways once they touch a real CI pipeline:

1. **Silent regressions.** The reported symptom is fixed in one function while a legacy assumption breaks in an unrelated module.
2. **Phantom patches.** Plausible-looking edits are produced for issues that were already fixed, cannot be reproduced, or were misread — pure churn.
3. **Blast-radius leaks.** Unconstrained agents refactor untouched modules, rewrite config, or edit security-critical routines far outside the fault boundary.

All three share a root cause: **the agent is its own judge.** Cerberus removes that authority.

## 3. Core Verification-First Idea

Authority to open a PR is moved out of the model and into a deterministic function. The model (once wired in) may only *propose*. The harness decides.

```python
# harness/admission_controller.py
admit_pr = gate_1 and gate_2 and gate_3 and gate_4
```

| Gate | Question | Source of truth |
| :--- | :--- | :--- |
| **1 — RED→GREEN** | Did a test that provably **failed** before the patch now **pass**? | pytest exit code, pre and post |
| **2 — No regressions** | Does the **full** existing suite still pass? | pytest exit code + parsed counts |
| **3 — Blast radius** | Are all edits inside the authorized AST boundary? | `git diff --numstat HEAD` + AST symbol diff |
| **4 — Real diff** | Is the diff non-empty, non-whitespace, and attributable to ≥1 file? | `git diff HEAD` content inspection |

Every gate reads from **process exit codes and git output**, never from model prose. A gate cannot be argued with, and no gate can be waived by a confident-sounding explanation. Failing any one gate routes the run to a terminal rejection state instead of a PR.

Gate 4 exists because gates 1–3 are all satisfiable by an empty patch when the target test is already green — the classic phantom-patch false positive.

---

## 4. Pipeline State Machine

Progress is not implicit in control flow; it is an explicit enum with a declared transition table. Undeclared moves raise `InvalidTransitionError`, and any move out of a terminal state raises `RuntimeError`.

```
INITIALIZED
  └─> TRIAGING ──────────────> REJECTED_INVALID_ISSUE        (terminal)
        └─> LOCALIZING ──────> REJECTED_NO_LOCALIZATION      (terminal)
              └─> REPRODUCING ─> REJECTED_NOT_REPRODUCIBLE   (terminal)
                    │                 (RED gate failed)
                    └─> PATCHING <──────────┐
                          ├─────────────────┘ retry (≤ max_attempts)
                          ├─> REJECTED_BY_POLICY             (terminal)
                          ├─> ABORT_DUPLICATE_DIFF           (terminal)
                          ├─> REJECTED_MAX_ATTEMPTS          (terminal)
                          └─> VERIFYING_REGRESSIONS
                                ├─> REJECTED_REGRESSION      (terminal)
                                └─> ANALYZING_BLAST_RADIUS
                                      ├─> REJECTED_BLAST_RADIUS (terminal)
                                      └─> ADMISSION_REVIEW
                                            ├─> REJECTED_ADMISSION  (terminal)
                                            └─> PUBLISHING ─> COMPLETED
```

Rejection is a **first-class, logged outcome**, not an exception path. `REJECTION_STATES` is a queryable set, so "why did this run not produce a PR?" is answered by reading one field.

## 5. System Architecture

```
                  GitHub Issue / Webhook Event
                              │
                     [ HMAC-SHA256 verify ]
                     [ X-GitHub-Delivery dedup ]
                              │
                              ▼
   ┌──────────────────────────────────────────────────────────┐
   │  TriageAgent           Pydantic-validated issue schema   │
   │                        actionability + severity scoring  │
   ├──────────────────────────────────────────────────────────┤
   │  LocalizationAgent     tree-sitter AST index (ast        │
   │                        fallback) + ripgrep/git-grep      │
   │                        → ranked candidates               │
   │                        → authorized repair boundary      │
   ├──────────────────────────────────────────────────────────┤
   │  ReproductionAgent     write test → run → REQUIRE FAIL   │
   │                        (hard RED gate; no fail, no run)  │
   ├──────────────────────────────────────────────────────────┤
   │  PatchAgent            propose → apply → re-run test     │
   │                        rollback on fail, retry ≤ 5       │
   │                        minimal-patch policy ≤ 150 lines  │
   │                        abort on duplicate diff hash      │
   ├──────────────────────────────────────────────────────────┤
   │  RegressionAgent       full suite + AST blast radius     │
   │                        ≤ 3 files, ≤ 200 lines default    │
   ├──────────────────────────────────────────────────────────┤
   │  AdmissionController   gate_1 ∧ gate_2 ∧ gate_3 ∧ gate_4 │
   ├──────────────────────────────────────────────────────────┤
   │  PRPublisher           evidence report + run.json + PR   │
   └──────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
      PR with full evidence          Terminal rejection state
                                     (logged, with reason)
```

Every stage runs inside a `Sandbox` (§7). Nothing touches the host working tree.

## 6. Component Responsibilities

| Component | File | Responsibility |
| :--- | :--- | :--- |
| **TriageAgent** | `agents/triage_agent.py` | Pydantic v2 issue-schema validation; rejects non-actionable issues before any compute is spent. |
| **LocalizationAgent** | `agents/localization_agent.py` | Ranks candidate files/symbols and emits the **authorized repair boundary** consumed by Gate 3. |
| **ReproductionAgent** | `agents/reproduction_agent.py` | Runs the reproduction test and **requires a non-zero exit**. Treats a `SyntaxError` in the test as non-reproduction, not as proof of a bug. |
| **PatchAgent** | `agents/patch_agent.py` | Iterative apply → verify → rollback loop with structured JSON failure feedback, duplicate-diff abort, and minimal-patch enforcement. |
| **RegressionAgent** | `agents/regression_agent.py` | Full-suite execution with regex-parsed pytest counts, plus `StructuralBlastRadius` (files, ± lines, modified symbols, unauthorized files). |
| **AdmissionController** | `harness/admission_controller.py` | The only component permitted to authorize a PR. Pure function of gate inputs. |
| **PRPublisher** | `github/pr_publisher.py` | Renders the human-readable evidence report, writes the JSON audit trail, creates the branch and PR. |
| **Sandbox** | `harness/docker_sandbox.py` | Docker-first isolation with a hardened local-process fallback. |
| **PipelineStateMachine** | `harness/pipeline_state.py` | Enforces legal state transitions; makes rejections explicit and queryable. |

### Retrieval layer

`retrieval/` implements code-aware search with graceful degradation at every level:

- **AST index** — tree-sitter when `tree-sitter` + `tree-sitter-python` are installed; falls back to the stdlib `ast` module otherwise. Extracts functions, classes, methods, and line spans.
- **Lexical search** — `ripgrep` if on `PATH`, else `git grep`, else a pure-Python scanner. Search always works, even in a bare container.

This is what lets Gate 3 reason about **symbols** rather than only line numbers: the blast-radius check compares the set of modified symbols against the authorized boundary, so a patch that stays within a file but rewrites an unrelated class is still caught.

## 7. Sandbox Security & Isolation

`harness/docker_sandbox.py` probes for a working Docker daemon and uses it when available. When Docker is absent it degrades to an isolated temp-directory process sandbox rather than failing — so the harness runs on a laptop and in CI without configuration changes.

**Docker mode:**

| Control | Setting |
| :--- | :--- |
| Memory cap | `--memory=2g` |
| CPU cap | `--cpus=2.0` |
| Fork-bomb guard | `--pids-limit=100` |
| Non-root execution | `--user 1000:1000` |
| Privilege escalation | `--security-opt=no-new-privileges:true` |
| Network | `--network none` (default; `network_disabled=True`) |
| Teardown | `docker rm -f` on `destroy()` / context exit |

**Both modes:**

- **Bounded output** — stdout/stderr truncated at `MAX_OUTPUT_CHARS = 500_000` so a runaway log cannot exhaust memory.
- **Process-tree kill on timeout** — `taskkill /F /T /PID` on Windows, `os.killpg(os.getpgid(pid), SIGKILL)` on POSIX. No orphaned children survive a timeout.
- **Path-traversal guard** — `_safe_resolve()` uses `os.path.commonpath` to reject any `write_file`/`read_file` path that escapes the workspace.
- **Deterministic teardown** — `__enter__`/`__exit__` guarantee cleanup of the temp workspace.

> **Fallback-mode caveat.** The process sandbox provides *isolation of the working tree*, not *containment of the code being executed*. It runs commands as the invoking user with `shell=True`. Treat Docker mode as the security boundary; treat fallback mode as a development convenience. Do not point fallback mode at untrusted repositories.

---

## 8. GitHub Integration & Security

`github/webhook_handler.py` (FastAPI):

- **HMAC-SHA256** signature verification of `X-Hub-Signature-256` using `hmac.compare_digest` — constant-time, no timing oracle.
- **Idempotency** via an `X-GitHub-Delivery` seen-set, so GitHub's at-least-once redelivery cannot trigger duplicate repairs.
- **Async dispatch** through `BackgroundTasks` so the webhook returns promptly.

> **Two known security gaps, both real:**
> 1. `_verify_signature()` **fails open** — when `GITHUB_WEBHOOK_SECRET` is unset it returns `True`, accepting unsigned payloads. Acceptable for local development; unsafe if exposed. Set the secret, or change the default to fail closed, before deploying.
> 2. `publish_pr()` writes `https://x-access-token:<token>@github.com/...` into the sandbox git remote and returns `push_res.stderr` verbatim on failure. Git can echo a remote URL in error output, so a push failure may surface the token in logs or in an API response.

## 9. Evaluation

### 9.1 What is measured vs. what is stipulated

Read this before the numbers.

| Metric | Provenance |
| :--- | :--- |
| Resolution rate, reproduction rate, regression-free rate, PR admission rate | **Measured.** Live `Sandbox`, real `PatchAgent.run_patch_loop`, real `RegressionAgent`, real `AdmissionController.evaluate`. |
| Avg patch attempts, avg patch size (± lines) | **Measured** from `PatchLoopResult` / `StructuralBlastRadius`. |
| Safety-rejection counts | **Measured** — these are the actual terminal states reached. |
| Avg runtime | **Measured**, plus a per-case constant offset. Varies run to run. |
| **Token consumption** | **Stipulated.** Hardcoded per-case constants in `swe_bench_runner.py`. No model is called, so no tokens are spent. |
| **Top-1 / Top-3 localization accuracy** | **Stipulated.** Hardcoded booleans, not compared against ground-truth patch files. |
| **`lite-25` subset** | **Synthetic.** `generate_swe_bench_lite_25()` fabricates records; it does not download or run SWE-bench Lite. |
| **Baseline & ablation tables** | **Hardcoded.** `baseline_runner.py` computes `rep_a/rep_b/rep_c` and then returns a literal string; `ablation_runner.py` computes nothing at all. |

So: the **gate behaviour** is empirically demonstrated. The **efficiency and localization figures are illustrative placeholders**, and the comparative tables are not evidence of anything.

### 9.2 Live smoke subset (5 injected-bug cases, reproducible)

```bash
cd autonomous-pr-fixer && python evaluation/swe_bench_runner.py --subset smoke
```

| Metric | Result |
| :--- | :--- |
| Resolution rate | **40.0%** (2/5) |
| Reproduction rate (RED gate) | **80.0%** (4/5) |
| Regression-free rate | **80.0%** |
| PR admission rate | **40.0%** |
| Avg patch attempts | **1.25** |
| Avg patch size | **+2.5 lines** |
| Avg runtime | **~2.6–2.9 s** per case |

Terminal rejection breakdown: **1** not reproducible, **1** regression detected, **1** blast-radius violation. Three of the five cases were stopped by a gate — which is the point of the exercise. A 40% admission rate on deliberately adversarial cases is the intended behaviour, not a failure.

## 10. Setup & Reproduction

Requires **Python 3.11+**. Docker is optional (the sandbox falls back automatically).

```bash
git clone <your-repo-url>
cd intelligent-noether/autonomous-pr-fixer
python -m venv .venv
```

Activate the environment — `source .venv/bin/activate` on macOS/Linux, `.venv\Scripts\activate` on Windows — then:

```bash
pip install -r requirements.txt
```

Run the test suite (54 tests, no network or Docker required):

```bash
python -m pytest tests/ -q
```

Run the gate demonstration:

```bash
python main.py --issue 1 --dry-run
```

> `main.py` is a **scripted demonstration**. It exercises triage → localization → RED gate → regression → blast radius → admission → `run.json` on a known bug, but the corrected file it writes is hard-coded (`main.py:189–207`). It shows that the gates work; it does not show a model repairing anything. See §1.

### Portability

Commands are built against the interpreter running the harness, not a hardcoded launcher. [`harness/py_interpreter.py`](autonomous-pr-fixer/harness/py_interpreter.py) resolves `sys.executable` once (quoting it when the path contains spaces), and `Sandbox.python_cmd` returns `python` instead when the sandbox is a container, where a host path would be meaningless. Windows, macOS, Linux, and CI runners all work without configuration.

### Configuration

Copy `.env.example` to `.env`. Recognized keys:

| Key | Effect |
| :--- | :--- |
| `GITHUB_TOKEN` | PAT used by `publish_pr`. Omit for dry-run. |
| `GITHUB_WEBHOOK_SECRET` | Enables HMAC verification. **Without it, verification fails open.** |
| `GITHUB_REPO_SLUG` | `owner/repo` target for PR creation. |
| `SANDBOX_TIMEOUT_SECONDS`, `SANDBOX_NETWORK_DISABLED` | Applied by the sandbox. |
| `LOG_LEVEL`, `RUN_ARTIFACTS_DIR` | Applied. |
| `PATCH_MAX_ATTEMPTS`, `PATCH_MAX_LINES_CHANGED` | **Parsed but never applied** — `PatchAgent` uses its own defaults (5 / 150). |
| `OPENAI_API_KEY` | **Read and never used.** No model is invoked. |

**Never commit `.env`.** It holds a live token.

## 11. Auditability: `run.json`

Every run — admitted or rejected — writes a machine-readable artifact to `artifacts/run_<id>/run.json` (`schema_version: 1.1`). Rejections are recorded with the same fidelity as successes.

```json
{
  "schema_version": "1.1",
  "gates": {
    "gate_1_target_test_passed": true,
    "gate_2_no_regressions": true,
    "gate_3_blast_radius_ok": true,
    "gate_4_patch_changed": true
  },
  "admitted": true,
  "diff_hash": "948c1f1f...",
  "blast_radius": {
    "files_changed": 1,
    "lines_added": 3,
    "lines_deleted": 1,
    "unauthorized_files": []
  }
}
```

`diff_hash` is what makes the duplicate-diff abort possible: a patch loop that proposes the same diff twice is stopped rather than allowed to burn its retry budget.

Artifacts under `artifacts/` with `schema_version: 1.0` predate Gate 4 and record only three gates.

`PRPublisher.build_evidence_report()` renders a full human-readable verification report — reproduction trace, iteration count, suite counts, blast radius, unified diff. It is **not reachable from the CLI path**: `main.py:309` passes `pr_body=""`. Wire it up before relying on PR bodies for review evidence.

---

## 12. Repository Layout

```
intelligent-noether/
├── README.md                        ← this file (canonical)
├── .gitignore
├── .github/workflows/ci.yml         tests on ubuntu + windows, secret audit
└── autonomous-pr-fixer/
    ├── main.py                      scripted 8-stage demo
    ├── requirements.txt
    ├── .env.example
    ├── agents/                      triage, localization, reproduction, patch, regression
    ├── harness/                     admission_controller, pipeline_state, docker_sandbox,
    │                                diff_utils, run_artifact, py_interpreter, config
    ├── github/                      webhook_handler, pr_publisher
    ├── retrieval/                   AST index + lexical search
    ├── evaluation/                  swe_bench, baseline, ablation, metrics_reporter
    ├── tests/                       54 tests
    └── artifacts/                   run.json audit trail (gitignored)
```

## 13. Known Limitations & Roadmap

Ordered by what most limits the project today.

1. **No model in the loop.** Patch synthesis and reproduction-test synthesis are the two places a model belongs, and neither is connected. `PatchAgent.run_patch_loop` already accepts a proposal callback, so this is an integration task, not a redesign.
2. **Webhook signature verification fails open** when the secret is unset (§8).
3. **Token leakage on push failure** — `publish_pr` returns raw git stderr (§8).
4. **Evaluation numbers are partly stipulated** (§9.1). Real SWE-bench Lite integration, real token accounting, and ground-truth localization scoring are all outstanding.
5. **Diff-source inconsistency.** `diff_utils.get_workspace_diff` runs plain `git diff` (worktree vs. index) while `RegressionAgent` uses `git diff --name-only HEAD` / `--numstat HEAD`. These agree only while nothing is staged. Anything that stages files will make Gate 3 and Gate 4 read different pictures of the same workspace.
6. **Patch config is inert.** `PATCH_MAX_ATTEMPTS` / `PATCH_MAX_LINES_CHANGED` are parsed and discarded.
7. **`build_evidence_report` is unreachable** from the CLI (§11).
8. **Single-language.** AST analysis is Python-only. The retrieval layer would need per-language grammars to generalize.
9. **Docker mode is untested.** The container flags in §7 are written but have never executed here; every measurement in §9 came from the process-fallback sandbox.

---

## 14. What This Project Actually Demonstrates

Not an agent that fixes bugs. **A verification harness that refuses to let unverified changes through** — and the architecture required to make that refusal principled rather than heuristic:

- Admission authority is a pure function of process exit codes and git output, not of model output.
- Reproduction is a **hard precondition**. A bug that cannot be made to fail on demand is not a bug the harness will attempt.
- Gate 4 exists specifically to catch the empty patch that satisfies every other gate.
- Rejection is a first-class outcome with a named terminal state, not a swallowed exception.
- Every run leaves a machine-readable artifact, whether it succeeded or not.

The parts that remain unbuilt are the parts a model does. The parts that decide whether to trust a model are built and tested.


# Cerberus — Verification-First Autonomous Software Repair Harness

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-103%20passed%20%2F%200%20failed-brightgreen.svg)](autonomous-pr-fixer/tests/)
[![Architecture](https://img.shields.io/badge/architecture-verification--first-orange.svg)](#3-core-verification-first-idea)

**Cerberus** is a research prototype of a *verification-first* software repair harness. Its thesis: a candidate patch should never reach a Pull Request because a model claims the bug is fixed — it should reach a PR only after passing a deterministic, programmatic conjunction of four gates: the issue was **proven reproducible** (Gate 1), the fix **introduces zero regressions** (Gate 2), the change **stays inside an authorized AST blast radius** (Gate 3), and the workspace yields a **real, non-empty, attributable diff** (Gate 4).

The gate machinery, sandbox, state machine, retrieval layer, and audit trail are fully implemented and tested. Model-generated patching is implemented but **opt-in** — the default run path is a deterministic scripted repair, so the demo is reproducible offline. Read [Implementation Status](#1-implementation-status) before the evaluation section.

---

## 1. Implementation Status

This project is a **verification harness first and an agent second**. The table below is the honest split, with the code that backs each claim.

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
| HMAC-SHA256 webhook verification (fail-closed) + idempotency | **Implemented** | [`github/webhook_handler.py`](autonomous-pr-fixer/github/webhook_handler.py) |
| Webhook → pipeline dispatch | **Implemented** | `_run_repair_pipeline()` calls `run_pipeline`; dry-run by default, needs `CERBERUS_REPO_DIR` |
| GitHub PR creation (branch, push, REST call), gated on admission | **Implemented** | [`github/pr_publisher.py`](autonomous-pr-fixer/github/pr_publisher.py) |
| **LLM patch generation** | **Implemented, opt-in** | [`agents/llm_patch_generator.py`](autonomous-pr-fixer/agents/llm_patch_generator.py) via `litellm`. Enabled with `--use-llm` / `CERBERUS_USE_LLM=1`; needs `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. |
| **LLM reproduction-test synthesis** | **Not wired in** | `ReproductionAgent.build_reproduction_prompt()` builds the prompt; nothing sends it. `run_reproduction_gate()` requires the test to be passed in as an argument. |
| **End-to-end run against a real provider** | **Unverified here** | No API key exists in this environment. The generator is tested through an injected `completion_fn` ([`tests/test_llm_patch_generator.py`](autonomous-pr-fixer/tests/test_llm_patch_generator.py), [`tests/test_main_llm_wiring.py`](autonomous-pr-fixer/tests/test_main_llm_wiring.py)) — real code path, fake transport. |
| **`main.py` default path** | **Scripted demo** | Without `--use-llm`, stage 5 writes a known corrected file. It demonstrates the gates on a known bug; it does not synthesize a fix. |

`PatchAgent`'s real retry loop is exercised three ways: by the LLM generator above, by [`evaluation/swe_bench_runner.py`](autonomous-pr-fixer/evaluation/swe_bench_runner.py) (live sandboxes, injected diffs), and by [`tests/test_tdd_loop.py`](autonomous-pr-fixer/tests/test_tdd_loop.py).

---

## 2. Problem Statement

Autonomous coding agents edit code fluently but fail in three characteristic ways once they touch a real CI pipeline:

1. **Silent regressions.** The reported symptom is fixed in one function while a legacy assumption breaks in an unrelated module.
2. **Phantom patches.** Plausible-looking edits are produced for issues that were already fixed, cannot be reproduced, or were misread — pure churn.
3. **Blast-radius leaks.** Unconstrained agents refactor untouched modules, rewrite config, or edit security-critical routines far outside the fault boundary.

All three share a root cause: **the agent is its own judge.** Cerberus removes that authority.

## 3. Core Verification-First Idea

Authority to open a PR is moved out of the model and into a deterministic function. The model may only *propose*. The harness decides.

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
   │                        minimal-patch policy ≤ 200 lines  │
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
- **Fails closed.** An unset `GITHUB_WEBHOOK_SECRET` rejects every delivery with `403`. Without the secret the endpoint cannot distinguish GitHub from anyone else who found the URL, and this handler dispatches code-modifying work. The escape hatch is explicit and loud: `CERBERUS_ALLOW_UNSIGNED_WEBHOOKS=1` accepts unsigned deliveries and logs a warning on each one.
- **Idempotency** via an `X-GitHub-Delivery` seen-set, so GitHub's at-least-once redelivery cannot trigger duplicate repairs.
- **Async dispatch** through `BackgroundTasks` so the webhook returns promptly. The task really runs the pipeline, but only when `CERBERUS_REPO_DIR` points at a usable checkout, and **in dry-run mode unless `CERBERUS_WEBHOOK_DRY_RUN=0`** — a webhook arriving at a fresh deployment should not open pull requests on someone's repository until that is switched on deliberately.

**Secret handling in `pr_publisher.py`:**

- The remote stored in the sandbox's `.git/config` is the **clean** URL; the token is passed only to the single `git push` invocation, so it does not survive on disk next to the run artifacts.
- Every git string that leaves the module passes through `redact_secrets()`, which strips the configured token, any `https://user:pass@` credential pair, and anything matching a GitHub token shape (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`github_pat_`) — including tokens that arrived from somewhere other than the current config.
- The commit message is written to a file and applied with `git commit -F`, never interpolated into a shell string. Branch names derived from issue titles go through `sanitize_ref()` first. `Sandbox.exec` uses `shell=True`, so webhook-supplied text must not be able to terminate one command and start another.

## 9. Evaluation

### 9.1 What is measured vs. what is stipulated

Read this before the numbers.

| Metric | Provenance |
| :--- | :--- |
| Resolution rate, reproduction rate, regression-free rate, PR admission rate | **Measured.** Live `Sandbox`, real `PatchAgent.run_patch_loop`, real `RegressionAgent`, real `AdmissionController.evaluate`. |
| Avg patch attempts, avg patch size (± lines) | **Measured** from `PatchLoopResult` / `StructuralBlastRadius`. |
| Safety-rejection counts | **Measured** — these are the actual terminal states reached. |
| Avg runtime | **Measured**, plus a per-case constant offset. Varies run to run. |
| Safe vs. unsafe admission split | **Measured.** `safe_resolution_rate` counts only PRs where the target passed *and* regressions were clean *and* the blast radius held; `unsafe_pr_rate` counts admitted PRs that failed any of those. Admission rate alone cannot distinguish the two, which is the whole comparison against an ungated agent. |
| **Token consumption** | **Not measured in the offline suites, and labelled as such.** Records carry a `tokens_measured` flag; when it is false the reporter prints `not measured` instead of a number, and `build_evidence_report` prints `not measured (no model in the loop)` instead of inventing a plausible count. Real counts appear only on an `--use-llm` run, accumulated from provider `usage` in `GenerationUsage`. |
| **Top-1 / Top-3 localization accuracy** | **Not measured**, same mechanism (`localization_measured`). The booleans in the scenario records were never compared against ground-truth patch files, so the reporter refuses to print them as percentages. |
| **`lite-25` subset** | **Synthetic.** `generate_swe_bench_lite_25()` fabricates records; it does not download or run SWE-bench Lite. Its title says so. |
| **Baseline & ablation tables** | **Derived from stipulated inputs.** Both are now computed from their scenario records rather than hardcoded — `evaluate_config()` in `ablation_runner.py`, interpolated `MetricsReporter` output in `baseline_runner.py`, both pinned by [`tests/test_evaluation_honesty.py`](autonomous-pr-fixer/tests/test_evaluation_honesty.py). The *inputs* are still authored bug profiles, not measured runs, and the rendered tables state that. |

So: the **gate behaviour** is empirically demonstrated. Efficiency and localization figures are **absent rather than fabricated**, and the comparative tables demonstrate that the gate logic behaves as claimed on the profiles fed to it — not that it behaves that way on SWE-bench.

> An earlier revision of `baseline_runner.py` wrote its percentages out by hand, and four of them disagreed with the records they claimed to summarise. Interpolating from the computed reports both corrected them and made that class of error impossible.

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
| Top-1 / Top-3 localization | `not measured` |
| Tokens per issue | `not measured` |

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

Run the test suite (103 tests, no network, no Docker, and no API key required):

```bash
python -m pytest tests/ -q
```

Run the gate demonstration:

```bash
python main.py --issue 1 --dry-run
```

> The default `main.py` path is a **scripted demonstration**. It exercises triage → localization → RED gate → regression → blast radius → admission → `run.json` on a known bug, but stage 5 writes a known corrected file rather than synthesizing one. It shows that the gates work. The diff hash is stable at `948c1f1fcbec` across runs, which is what makes it useful as a regression check on the harness itself.

Run the same pipeline with a model generating the patch:

```bash
python main.py --issue 1 --dry-run --use-llm
```

This needs `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. Stage 5 then builds an [`LLMPatchGenerator`](autonomous-pr-fixer/agents/llm_patch_generator.py) over the localization boundary and hands it to the real `PatchAgent.run_patch_loop`: the model proposes a unified diff, `git apply` applies it, the reproduction test judges it, and a failure is fed back as the verifier's own output on the next attempt. Every gate downstream is unchanged — the model has no more authority than the scripted path had. Without a key the run prints a warning and falls back to the scripted repair rather than failing.

### Portability

Commands are built against the interpreter running the harness, not a hardcoded launcher. [`harness/py_interpreter.py`](autonomous-pr-fixer/harness/py_interpreter.py) resolves `sys.executable` once (quoting it when the path contains spaces), and `Sandbox.python_cmd` returns `python` instead when the sandbox is a container, where a host path would be meaningless. Windows, macOS, Linux, and CI runners all work without configuration.

### Configuration

Copy `.env.example` to `.env`. Recognized keys:

| Key | Effect |
| :--- | :--- |
| `GITHUB_TOKEN` | PAT used by `publish_pr`. Omit for dry-run. |
| `GITHUB_WEBHOOK_SECRET` | HMAC secret. **Required** — without it every delivery is rejected `403`. |
| `GITHUB_REPO_SLUG` | `owner/repo` target for PR creation. |
| `GITHUB_BASE_BRANCH` | Base branch for the PR. Defaults to `main`; set it if your default branch is `master`, otherwise the API rejects the PR after the push already succeeded. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Provider credential for `--use-llm`. Either one is enough; the model default follows whichever is present. |
| `CERBERUS_USE_LLM` | `1` to generate patches with a model without passing `--use-llm`. Off by default. |
| `CERBERUS_MODEL` | litellm model id, e.g. `anthropic/claude-sonnet-5` or `openai/gpt-4o`. Overrides the key-based default. |
| `CERBERUS_REPO_DIR` | Local checkout the webhook-triggered pipeline operates on. Unset ⇒ the dispatch logs `repair_pipeline_skipped` and does nothing. |
| `CERBERUS_WEBHOOK_DRY_RUN` | `0` to let webhook-triggered runs actually push and open PRs. **Defaults to dry-run.** |
| `CERBERUS_ALLOW_UNSIGNED_WEBHOOKS` | `1` to accept unsigned deliveries. Development only; logs a warning per delivery. |
| `PATCH_MAX_ATTEMPTS`, `PATCH_MAX_LINES_CHANGED` | Applied. `PatchAgent` reads them for anything a caller did not pass explicitly (default 5 / 200). |
| `SANDBOX_TIMEOUT_SECONDS`, `SANDBOX_NETWORK_DISABLED` | Applied by the sandbox. |
| `LOG_LEVEL`, `RUN_ARTIFACTS_DIR` | Applied. |

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

`PRPublisher.build_evidence_report()` renders a full human-readable verification report — reproduction trace, iteration count, suite counts, blast radius, unified diff, token consumption. Every admitted run uses it as the PR body. Rejected runs never reach `publish_pr` at all; that gating is pinned by [`tests/test_publish_gating.py`](autonomous-pr-fixer/tests/test_publish_gating.py), because the claim "a PR appears only when all four gates hold" is only true if the publish call actually consults the decision.

---

## 12. Repository Layout

```
intelligent-noether/
├── README.md                        ← this file (canonical)
├── .gitignore
├── .github/workflows/ci.yml         tests on ubuntu + windows, secret audit
└── autonomous-pr-fixer/
    ├── main.py                      8-stage pipeline; scripted by default, --use-llm for a model
    ├── requirements.txt
    ├── .env.example
    ├── agents/                      triage, localization, reproduction, patch, regression,
    │                                llm_patch_generator
    ├── harness/                     admission_controller, pipeline_state, docker_sandbox,
    │                                diff_utils, run_artifact, py_interpreter, config
    ├── github/                      webhook_handler, pr_publisher
    ├── retrieval/                   AST index + lexical search
    ├── evaluation/                  swe_bench, baseline, ablation, metrics_reporter
    ├── tests/                       103 tests
    └── artifacts/                   run.json audit trail (gitignored)
```

## 13. Known Limitations & Roadmap

Ordered by what most limits the project today.

1. **No model has actually run here.** The LLM patch path is implemented and tested through an injected transport, but this environment has no provider credential, so it has never made a real API call. Prompt quality, retry effectiveness, and cost per fix are all unmeasured.
2. **Reproduction-test synthesis is still scripted.** This matters more than patch synthesis: the reproduction test *is* the specification the whole harness enforces. `build_reproduction_prompt()` exists; nothing sends it. Until it is wired in, the RED gate proves that a supplied test fails, not that the harness understood the issue.
3. **Evaluation is not SWE-bench.** The `lite-25` subset is synthetic, and the baseline/ablation tables are computed from authored bug profiles. Real SWE-bench Lite integration and ground-truth localization scoring are outstanding; the honesty flags currently prevent the missing measurements from being *presented*, which is not the same as having them.
4. **Docker mode is untested.** The container flags in §7 have never executed here — no Docker daemon on this machine. Every measurement came from the process-fallback sandbox, which isolates the working tree but does not contain the code it runs.
5. **Single-language.** AST analysis is Python-only. The retrieval layer would need per-language grammars to generalize.
6. **Webhook idempotency is in-process.** The `X-GitHub-Delivery` seen-set lives in memory, so a restart or a second worker forgets it. Fine for one process; not for a horizontally scaled deployment.
7. **`main.py`'s stage 1–4 shortcuts.** Triage, the reproduction test, and the localization target are chosen by branching on issue text for two known scenarios. The gates downstream are general; the demo's front end is not.

---

## 14. What This Project Actually Demonstrates

Not an agent that fixes bugs. **A verification harness that refuses to let unverified changes through** — and the architecture required to make that refusal principled rather than heuristic:

- Admission authority is a pure function of process exit codes and git output, not of model output.
- Reproduction is a **hard precondition**. A bug that cannot be made to fail on demand is not a bug the harness will attempt.
- Gate 4 exists specifically to catch the empty patch that satisfies every other gate.
- Rejection is a first-class outcome with a named terminal state, not a swallowed exception.
- Publication is gated on the decision, not reported alongside it: a rejected patch never reaches `git push`.
- Unmeasured quantities are labelled `not measured` rather than filled with a plausible constant, because in an evidence artifact a fabricated number is indistinguishable from a real one.
- Every run leaves a machine-readable artifact, whether it succeeded or not.

A model can now propose the patch. It still cannot approve one.


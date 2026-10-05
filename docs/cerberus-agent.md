# Cerberus repair agent (Phase 1)

An agentic software repair layer around the existing Cerberus gate. One model, a fixed set of sandboxed tools,
a bounded retry loop. **The agent proposes; the unchanged Cerberus gate decides.** A run ends `ADMITTED` only when
a gate run's `run.json` says `ADMITTED`; nothing in the agent can mark a patch as accepted.

Code: [`autonomous-pr-fixer/repair/`](../autonomous-pr-fixer/repair). Gate: `main.run_pipeline`, untouched — no file
under `harness/`, `benchmark/`, `github/` or the gate's `agents/` changed in Phase 1.

## 1. Agent architecture

```text
issue + reproduction test
        │
        ▼
 agent workspace  ── Cerberus Sandbox on the base commit (Docker, offline after setup)
        │
        ▼
 RepairAgent loop ── model call → tool calls → … → submit_patch
        │                                               │
        │                       workspace diff ─────────┘
        ▼
 CerberusGate.verify(diff) ── main.run_pipeline on a FRESH clone of the base commit
        │                     RED ×3 → BASELINE → GREEN ×3 → REGRESSION → SCOPE → ADMISSION
        ▼
 run.json ──► GateFeedback ──► ADMITTED: stop
                               REFUSED / INVALID_PATCH: feedback to the model, retry (bounded)
```

| Module | Responsibility |
| --- | --- |
| `repair/agent.py` | The loop, limits, submission handling, artifacts |
| `repair/runner.py` | One run end to end: workspace, gate adapter, agent, `agent.log` |
| `repair/workspace.py` | The agent's sandbox: reuses `Sandbox`, `RepoSetupAgent` and `.cerberus.yml` setup |
| `repair/tools/` | Tool framework and the tools |
| `repair/verification/adapter.py` | `CerberusGate` and `feedback_from_run_json` |
| `repair/llm/` | `ChatModel` protocol, Anthropic adapter, fake model |
| `repair/observability/langfuse.py` | Tracer interface, Langfuse implementation, redaction |
| `repair/metrics/repair_metrics.py` | Per-task results and aggregate metrics |
| `repair/evaluation.py` | The 12-task evaluation runner |
| `repair/cli.py` | `python main.py agent run | evaluate | demo` |

There is one agent. No planner/critic/reviewer sub-agents.

## 2. Tool architecture

Every tool has a Pydantic input schema (sent to the model through native tool calling), returns a JSON object,
and fails with a structured error code (`PERMISSION_DENIED`, `INVALID_ARGUMENT`, `NOT_FOUND`, `TOOL_TIMEOUT`,
`PATCH_APPLY_FAILED`, `TOOL_FAILURE`) instead of an exception. `ToolRegistry` validates arguments, enforces an
allow-list of tool names, applies a timeout, and logs each call.

| Tool | What it does | Runs where |
| --- | --- | --- |
| `search_code` | Literal search (reuses `retrieval/lexical_search.py`), with context lines and the enclosing symbol | host read of workspace files, symlinks never followed |
| `read_file` | Bounded line range (≤ 400 lines, ≤ 1 MiB file) | host read through `Sandbox.read_bytes` (`O_NOFOLLOW`) |
| `find_symbol` | Function / class / method / module definitions (reuses `retrieval/ast_index.py`) | host read |
| `find_references` | Whole-word identifier search, definitions flagged | host read |
| `read_test` | List test files, or read one with its test names | host read |
| `run_test` | `python -B -m pytest <target>` | **`Sandbox.exec`** — offline Docker container |
| `git_status` / `git_diff` | Changes against the base commit (reuses `DiffUtils.workspace_changes`) | `Sandbox.exec`, fixed commands |
| `edit_file` | Replace one unique exact string (or create a file) | host write through `Sandbox.write_bytes` |
| `apply_patch` | Unified diff via `git apply` (reuses `DiffUtils.apply_diff_to_sandbox`) | `Sandbox.exec` |
| `reset_workspace` | Back to the base commit, reproduction test kept (reuses `DiffUtils.rollback`) | `Sandbox.exec` |
| `submit_patch` | Send the workspace diff to Cerberus | intercepted by the loop |

**Permission policy** (`repair/tools/base.py`):

- Reads: repository-relative paths only. Absolute paths, drive letters, `~`, any `..` component, `.git/`,
  `.cerberus/` (except the reproduction test), `.github/` and credential-like names (`.env*`, `*.pem`, `*.key`,
  `id_rsa*`, `credentials.json`, …) are refused.
- Writes: additionally refused for every test file (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`), the
  reproduction test, and `.cerberus.yml`. The agent cannot weaken tests or loosen policy.
- `run_test` targets must match `path.py(::node)*` with no spaces or shell characters, and must be test files.
- No arbitrary command tool exists. No git operation other than the fixed read-only diff/status and the internal
  reset is reachable.

## 3. State model

`repair/state.py` (Pydantic): `RepairState` holds run, repository, commit SHA, issue, iteration and limit,
candidate files and relevant tests (from what the agent read/edited/ran), every `ToolCallRecord` and
`LLMCallRecord`, every `AttemptRecord` (diff, files, ±lines, reasoning, whether the agent's own reproduction run
passed, and the `GateFeedback`), the latest verification result, token and cost totals, timing, `final_status`
and `trace_id`. It is written to `artifacts/repair/<run_id>/agent_run.json`.

`FinalStatus`: `ADMITTED`, `MAX_ATTEMPTS_EXCEEDED`, `NO_PATCH`, `GAVE_UP`, `COST_LIMIT_EXCEEDED`,
`TOKEN_LIMIT_EXCEEDED`, `TOOL_LIMIT_EXCEEDED`, `TIMEOUT`, `LLM_FAILURE`, `SETUP_FAILURE`, `ERROR`.

## 4. Patch generation

The model edits its workspace with `edit_file` / `apply_patch`, checks itself with `run_test` and `git_diff`, then
calls `submit_patch(reasoning)`. The submitted patch is always the measured workspace diff against the base
commit (new files included), never text the model typed. Before a gate run is spent, the loop checks that the diff
is non-empty and that every changed file passes the write policy; failures return `INVALID_PATCH` feedback and
still count as an attempt. Changed files and ±lines are recorded; size is not limited here beyond what the gate's
SCOPE stage enforces.

## 5. Verification integration

`CerberusGate.verify` calls `main.run_pipeline(repo_dir, …, repro_test_code, patch_source=DiffPatchSource([diff]),
base_ref, run_id)` exactly as the CLI does, with console output redirected to a per-run log file, and then reads
`run.json`. The gate clones the base commit into its own sandbox, so nothing the agent did to its workspace other
than the diff can influence the verdict.

`feedback_from_run_json` turns `run.json` into `GateFeedback`: status, stage, refusal code, failing tests (newly
failing for `REGRESSION`, the reproduction test for `GREEN_*`), scope detail (unauthorized files), changed and
allowed files, ±lines, a bounded detail excerpt, whether the patch applied, and a fixed next-action hint per code.
Console text is never parsed.

## 6. Retry loop

Default `MAX_ATTEMPTS=3` gate submissions. The existing gate's inner loop only retries GREEN; the agent's outer
loop retries on **any** refusal — `GREEN_NOT_REACHED`, `REGRESSION`, `SCOPE_VIOLATION`, apply failures, errors —
because the feedback goes back to the model and it can revise and resubmit. The gate itself is not modified to do
this. The loop also stops on tool-call, token, cost and wall-time limits, and after three consecutive replies
without a tool call.

## 7. Security boundaries

- Repository code only executes through `Sandbox.exec`: in the default isolation, the offline, capability-dropped,
  resource-limited Docker container. `--unsafe-local-sandbox` exists only for trusted fixtures, as in the gate.
- The model process runs on the host; it never executes repository code there. Host-side tools only read and write
  workspace files through the sandbox's symlink-refusing I/O.
- API keys are read by the provider SDK from the environment. They are never passed to a sandbox, written to
  `agent_run.json` or `agent.log`, or sent to Langfuse (see redaction below).
- The agent cannot see hidden evaluation tests, cannot edit tests or `.cerberus.yml`, cannot call the gate except
  with its workspace diff, and cannot publish or merge.
- Repository text is untrusted input to the model. The tool boundary, not the prompt, is what limits a
  prompt-injected model: whatever it attempts, it can only produce a diff, and the gate judges the diff.

## 8. Langfuse

Enabled when `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set (`LANGFUSE_HOST` optional); otherwise a no-op
tracer. One repair run is one trace with a deterministic id (`create_trace_id(seed=run_id)`):

| Observation | Type | Content |
| --- | --- | --- |
| `cerberus-repair` | `agent` | run id, repository, commit, issue, model, prompt version, final status, totals |
| `llm-call` | `generation` | model, input/output/total tokens, cost, latency, stop reason, tool names |
| `tool:<name>` | `tool` | redacted arguments (long strings replaced by their length), ok/error code, duration, output size |
| `cerberus-verification` | `chain` | attempt, gate status, refusal code, stage, changed files, duration |

Prompts, completions, file contents and test output are **not** sent. Everything sent passes through `redact`,
which masks secret-named keys, common token formats (Anthropic, OpenAI, GitHub, Slack, AWS, JWT, PEM keys) and the
literal values of secret-named environment variables. Tracing failures are swallowed and never change a run.
Langfuse is observability, not the source of truth: the verdict of record is `run.json`.

## 9. Metrics

`repair/metrics/repair_metrics.py`. Per task: status, attempts, time, tokens, cost, tool usage, patch files and
size, per-attempt gate results, failure stage/code, `agent_claimed_success`, `patch_generated`, `hidden_check`,
`false_admission`, `localization_success`.

| Metric | Definition |
| --- | --- |
| successful repair / fix rate | the gate admitted the patch (the only success) |
| verified correct fix | admitted **and** the independent hidden check passed |
| false admission | admitted **and** the hidden check failed; `NOT_MEASURED` when no hidden check exists — never reported as zero |
| agent-claimed success | the agent's own last reproduction run passed before a submission, whatever the gate decided |
| localization success | the final patch touched every ground-truth bug file; `NOT_MEASURED` without ground truth or patch |
| green / regression / scope failures | gate refusals by code, counted per submission |
| patch application failures | submissions the gate could not `git apply` |

Plus attempts per task and per successful fix, mean/median repair time, total/average cost, cost per successful
fix (only when every task's cost is known), token totals, tool usage, final-status breakdown, LLM failures.

## 10. Evaluation methodology

`python main.py agent evaluate` runs the 12 agent-capable tasks from the audit:

- **9 benchmark v1 bugs** — one per reproducible bug. Issue and reproduction test come from an instance of that bug
  (the `correct-fix` instance where one exists); the fixed candidate patches are ignored. Hidden check: all of
  `benchmark/hidden/test_hidden_<template>.py` must pass with the agent's patch. Localization ground truth: the
  files the seeded bug modifies. Frozen files are only read.
- **3 external upstream bugs** — sqlparse-332, boltons-backoff, more-itertools-last at their pinned parent
  commits, reproduction test = the lines the upstream fix commit added. Hidden check: the full upstream test file
  as of the fix commit, run with the agent's patch; every test that passes with the upstream fix must pass.
  Localization ground truth: source files changed by the upstream fix.

Each experiment writes `results/<experiment_id>/` — `config.json` (git commit and dirty flag, dataset version,
prompt version, model, all budgets, task ids and sources, platform), `task-results.jsonl`, `metrics.json`,
`summary.json`, `report.md`. Existing experiment directories are never overwritten. When the cumulative cost
reaches `MAX_EVALUATION_COST`, remaining tasks are recorded as `SKIPPED_BUDGET`, not dropped.

This is a separate measurement from the gate benchmark (`benchmark/reports/v1-docker.md`), which judges fixed
candidate patches. The two are never combined into one score.

Known biases: the v1 bugs were written by the Cerberus author; two of three v1 templates are single-module, so
localization there is easy; the external fixes are public and may be in a model's training data.

## 11. Configuration

Environment variables (see [`autonomous-pr-fixer/.env.example`](../autonomous-pr-fixer/.env.example)):

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `gemini` (Google AI Studio, OpenAI-compatible endpoint) |
| `LLM_MODEL` | `claude-sonnet-5-5` / `gemini-2.5-flash` | model id; default depends on the provider |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | unset | Google AI Studio key, sent only in the Authorization header |
| `LLM_MIN_SECONDS_BETWEEN_CALLS` | 6 for gemini, else 0 | spacing for free-tier rate limits; 429/5xx are retried with the server's delay |
| `LLM_FREE_TIER` | `true` | gemini only: record cost as $0; set `false` (and the price variables) for a paid key |
| `LLM_INPUT_PRICE_PER_MTOK` / `LLM_OUTPUT_PRICE_PER_MTOK` | unset | override the price table |
| `LLM_EFFORT` | `medium` | Anthropic `output_config.effort` |
| `LLM_SERVER_FALLBACKS` | `true` | Anthropic server-side refusal fallbacks (`fallbacks: "default"`) |
| `MAX_ATTEMPTS` | 3 | gate submissions |
| `MAX_TOOL_CALLS` | 50 | per run |
| `MAX_AGENT_TOKENS` | 400000 | input + output tokens per run |
| `MAX_RUN_TIME` | 1800 | seconds per run |
| `MAX_COST_PER_TASK` | 1.25 | USD; checked before every model call |
| `MAX_EVALUATION_COST` | 15 | USD per evaluation |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | unset | enables tracing |

Model-specific notes for `claude-sonnet-5-5`: no sampling parameters are sent (the model rejects them); thinking
blocks are replayed unchanged in later turns; a `refusal` stop reason ends the run as `LLM_FAILURE`. Cost uses the
published price ($2 / $10 per million input / output tokens); a model without a known price gets no cost estimate
rather than a guessed one, and only the token budget applies to it.

## 12. Running with the fake model

No key, no network, no cost:

```bash
python -m pytest tests/test_repair_unit.py tests/test_repair_gate_integration.py -q
python main.py agent demo                # scripted negative-path demo, real gate in Docker
python main.py agent demo --unsafe-local-sandbox   # same on the host (trusted fixture only)
```

The demo is labelled `SCRIPTED DEMO`: a fixed script applies benchmark instance iv-04's regressing candidate,
Cerberus refuses it with `REGRESSION` naming `test_merge_zero_length_interval`, the feedback goes back, the script
resets and applies iv-01's correct candidate, and Cerberus admits it. The model is scripted; every verdict is real.

## 13. Running with a real model

Free (Google AI Studio), PowerShell:

```powershell
$env:LLM_PROVIDER = "gemini"; $env:GEMINI_API_KEY = "<your key>"
python main.py agent run --repo ./target-repo --issue "percent raises ZeroDivisionError when whole is 0" --repro-test ./test_reproduce.py
python main.py agent evaluate --only v1-intervals-contains-excludes-the-end-point --experiment-id gemini-smoke
python main.py agent evaluate --limit 12 --experiment-id phase1-gemini25flash-run1
```

Paid (Anthropic):

```bash
export ANTHROPIC_API_KEY=...            # or an `ant auth login` profile
python main.py agent run --repo ./target-repo --issue "percent raises ZeroDivisionError when whole is 0" \
    --body "calc/rates.py: percent(5, 0) should return 0.0" --repro-test ./test_reproduce.py
python main.py agent evaluate --limit 12 --experiment-id phase1-sonnet55-run1
```

Output shows the run id, model, issue, each tool used, each attempt's patch, the Cerberus verdict
(`PASS (ADMITTED)` or `FAIL (REFUSED, <stage>, <code>)` with failing tests), the retry, and the final status with
attempts, tool calls, tokens and cost. Artifacts: `artifacts/repair/<run_id>/agent_run.json`, `agent.log`, and
`gate/<run_id>-g<n>/run.json` per submission.

## 14. Known limitations

- A reproduction test is required input; the agent does not write one (the gate's `--use-llm-repro` path is
  separate and unchanged).
- Each submission re-runs the full gate (setup, RED ×3, baseline): about 15–45 s per attempt on these repositories.
- The gate's default scope is the single file it localizes from the issue text; a correct fix in another file is
  refused as `SCOPE_VIOLATION` unless `.cerberus.yml` widens scope. The agent learns this only from feedback.
- Host-side reads and the model loop run on the host; only repository code execution is sandboxed.
- Python/pytest repositories only, as for the gate. Validated on Windows with Docker Desktop.
- Adapters exist for Anthropic and for OpenAI-compatible endpoints (used for Gemini); others need a small `ChatModel`.
- Free-tier quotas can stop an evaluation midway (`LLM_FAILURE` after retries); re-run the missing tasks with `--only`.
- 12 tasks cannot establish a rate with any precision; see the Phase 1 results for intervals.

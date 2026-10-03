# Cerberus 2.0 — upgrade plan

Status: **audit complete, nothing implemented.** Current architecture, component inventory and the exact
touched/untouched file lists are in [`cerberus-current-architecture.md`](cerberus-current-architecture.md).

Guiding rule: Cerberus is the judge, the agent is a patch source. The agent never decides that a patch passed; only
`run_pipeline` reaching `ADMITTED` counts.

## 1. Baseline

See section 10 (filled in from the run on `feature/cerberus-agent` at `daf199e`).

## 2. What already exists (reused, not rebuilt)

| 2.0 requirement | Already in the repo | Reuse |
| --- | --- | --- |
| Sandboxed execution | `harness/docker_sandbox.Sandbox` | every agent tool that executes code goes through `Sandbox.exec` |
| Patch application + rollback | `agents/patch_agent.py`, `harness/diff_utils.py` | unchanged, inside the gate |
| Verification gate | `main.run_pipeline` | called as-is, once per agent attempt |
| Structured failure | `run.json` (refusal code, failing tests, regression ids, scope report) | parsed into agent feedback |
| Provider-independent LLM | litellm in `agents/llm_patch_generator.py` | same layer, with tool calling |
| Token accounting | `GenerationUsage` | extended with cost |
| AST symbols, lexical search, test links | `retrieval/` | backs `find_symbol`, `search_code`, `read_test`, and the localization baseline |
| Run state persistence | `run.json`, SQLite `github/run_store.py` | agent state written next to `run.json`; SQLite kept |
| Evaluation with ground truth | `benchmark/` (hidden tests), `evaluation/external/` | repair tasks derived from them, frozen files untouched |
| Bounded retries | `PATCH_MAX_ATTEMPTS` | outer agent budget added; inner gate loop untouched |

## 3. What is actually missing

1. A tool-using agent (today the model gets one file and no tools).
2. Gate feedback after GREEN reaching the generator (REGRESSION / SCOPE end the run today).
3. Cost per attempt / per fix, fix-rate metrics, attempts per fix.
4. Measured localization accuracy (Top-1/3/5/10).
5. A repair-task dataset (the frozen benchmark ships fixed candidate patches; it judges the gate, not repair).
6. Tracing (Langfuse), code index tied to a SHA, Qdrant, hybrid retrieval, LangGraph — Phases 2–4.
7. Issue-triggered runs in the GitHub App (Phase 4).

**Discrepancy in the brief:** the gate has no STATIC ANALYSIS or SECURITY CHECKS stage. Adding one would change
admission semantics and invalidate the benchmark report of record, so Phase 1 does not add it. If wanted, it should
be a separate, opt-in gate with its own refusal code and its own measurement (open question for the owner).

## 4. Phase 1 design — one native repair agent

New package `autonomous-pr-fixer/repair/` (named `repair` to avoid clashing with the existing `agents/`):

| Module | Responsibility |
| --- | --- |
| `state.py` | Pydantic `RepairState`, `AttemptRecord`, `GateFeedback`, `RepairOutcome` (`ADMITTED`, `REFUSED_EXHAUSTED`, `INVALID_PATCH`, `LLM_FAILURE`, `BUDGET_EXCEEDED`, `TOOL_FAILURE`, `ERROR`) |
| `llm.py` | `ChatModel` protocol; `LiteLLMChatModel` (Anthropic / OpenAI / Google / local via `LLM_PROVIDER`, `LLM_MODEL`); `ScriptedChatModel` for tests; usage + cost from litellm's price table, `None` when unknown |
| `tools.py` | tool registry with JSON schemas and an explicit permission policy |
| `workspace.py` | the agent's own Docker sandbox on the base commit (reuses `Sandbox`) |
| `gate.py` | `verify(diff) -> GateFeedback`: writes the diff, calls `run_pipeline(..., patch_source=DiffPatchSource([diff]))`, reads `run.json` |
| `loop.py` | bounded agent loop: analyze → search → patch → verify → feedback → retry |
| `prompts.py` | versioned prompts (`PROMPT_VERSION`) |
| `metrics.py` | per-run and aggregate metrics |

### Tools

| Tool | Where it runs | Policy |
| --- | --- | --- |
| `search_code(query)` | `retrieval/lexical_search` over the agent workspace | read-only, result cap |
| `read_file(path, start, end)` | `Sandbox.read_file` (symlink-safe) | workspace-relative only, `.git` and `.cerberus` denied, size cap |
| `find_symbol(name)` / `find_references(name)` | `retrieval/ast_index` | read-only |
| `read_test(path)` | `read_file` restricted to test files | read-only |
| `run_test(test_id)` | `Sandbox.exec` inside the offline container | pytest node id validated against a pattern, no shell metacharacters |
| `run_command(argv)` | `Sandbox.exec` | allowlist: `python -m pytest`, `python -c` disallowed, `git diff/status/log` only; everything else refused |
| `git_diff()` / `git_status()` | `Sandbox.exec` | fixed argv |
| `apply_patch(diff)` / `reset_workspace()` | `git apply --check` then `git apply` in the agent workspace | rejects paths outside workspace, test-file edits, `.cerberus*` |
| `submit_patch(reasoning)` | ends the turn; the loop sends `git diff` to the gate | the only way to request verification |

No tool reaches the host filesystem, the network, environment variables or Cerberus's own source. The gate runs in a
**fresh** sandbox from the base commit, so nothing the agent did to its workspace (other than the diff) can influence
the verdict. `run_test` results are advisory and labelled as such in the prompt.

### Pre-gate patch validation

Before a diff is sent to the gate: parseable unified diff, every target file exists at base (or is a declared new
file), `git apply --check` passes, no test/`.cerberus` paths, within `.cerberus.yml` file/line budgets. Recorded:
changed files, +/- lines, agent's stated reasoning. Failures return `INVALID_PATCH` feedback without spending a gate
run.

### Bounded loop

`MAX_AGENT_ATTEMPTS=3` (gate submissions), `MAX_TOOL_CALLS=40`, `MAX_CONTEXT_CHARS`, `MAX_EXECUTION_SECONDS`,
`MAX_TOKEN_BUDGET`, `MAX_COST_PER_RUN`. Every limit ends the run with a machine-readable outcome. Every iteration,
tool call (name, duration, result size), LLM call (tokens, latency, cost) and gate verdict is appended to
`artifacts/<agent_run_id>/agent_run.json`, linked to each gate run's `run.json`.

### Gate feedback to the agent

Built only from `run.json`:

```json
{"status": "REFUSED", "refusal_code": "REGRESSION", "stage": "REGRESSION",
 "newly_failing": ["tests/test_x.py::test_y"], "failure_excerpt": "...(bounded)...",
 "changed_files": ["pkg/mod.py"], "suggested_next_action": "inspect tests/test_x.py::test_y"}
```

`suggested_next_action` is a fixed mapping from refusal code, not model output.

### CLI

`python main.py agent run --repo <path> --issue <n> --title ... --body ... --repro-test <path> [--model ...]`.
Existing flags and behaviour are unchanged; `agent` is a new subcommand detected before the existing parser.

### Cost of verifying per attempt

Each gate call repeats SETUP, RED×3 and BASELINE. On the v1 templates this is ~25 s; on sqlparse ~45 s. Accepted for
Phase 1 because it keeps the gate byte-for-byte untouched. A cached-baseline optimisation is a later, measured change.

## 5. Evaluation methodology

**Repair tasks** (new, `evaluation/repair_tasks/`, derived read-only from frozen files):

- 9 reproducible v1 bugs: issue text + the instance's valid reproduction test; candidate patches are ignored.
  Ground truth for correctness = hidden tests in `benchmark/hidden/`; ground truth for localization = `bug[].file`.
- 3 external bugs (sqlparse-332, boltons, more-itertools) at pinned commits; correctness ground truth = upstream
  fix's tests passing + gate verdict.
- Later (Phase 4): a documented SWE-bench Verified subset, isolated from both.

**Metrics per run:** `raw_agent_success` (the agent's own `run_test` passed before submit), `cerberus_admitted`,
`cerberus_refused` (by code), `invalid_patch`, `agent_exhausted_attempts`, `false_admission` (admitted but hidden
tests fail), attempts, wall time, tokens, cost.

**Aggregate:** fix rate (= admitted and hidden-test-correct / tasks), admission rate, false-admission rate, attempts
per successful fix, mean/median time, cost per attempt, cost per successful fix, LLM failure rate. Each with n and a
Wilson interval (reusing `evaluation/benchmark.wilson`).

**Localization baseline** (`docs/localization-evaluation.md`): existing `LocalizationAgent` with the cap raised to 10
in the evaluator only (production cap untouched), Top-1/3/5/10 at file and function level, on all 12 tasks.

**Experiment record:** experiment id, Cerberus commit, model + version, prompt version, tool set, budgets,
temperature, task set version, results, cost, duration — `evaluation/experiments/<id>.json`.

## 6. Risks

| Risk | Mitigation |
| --- | --- |
| 12 tasks is too few to show improvement | report n and intervals; do not claim deltas inside the interval; SWE-bench subset in Phase 4 |
| v1 localization is trivial (2 of 3 templates are single-module) | report it, weight external/SWE-bench tasks for retrieval decisions |
| Model may have seen sqlparse/boltons fixes | note contamination; prefer the synthetic set for clean signal |
| Gate-per-attempt latency | measured; optimise only after Phase 1 numbers |
| Prompt injection from repository text | tools are capability-limited; the gate is unaffected by agent text |
| API cost | `MAX_COST_PER_RUN`, scripted model for all tests, real runs only on request |
| Breaking the gate | no edits to `harness/` or gate agents; full suite after every commit |

## 7. Security considerations

- Repository code only ever executes in `Sandbox` (Docker, offline). The agent loop process itself never runs repo
  code.
- API keys are read from the environment by litellm in the host process; they are never passed to `Sandbox`, never
  written to `agent_run.json`, and redacted from errors.
- Tool arguments are validated against schemas; paths go through the sandbox's symlink-safe resolver.
- The agent cannot call the gate except by submitting a diff, cannot see the hidden tests, cannot modify tests or
  `.cerberus.yml`, and cannot publish.
- Security tests: path traversal, symlinked paths, shell metacharacters in `run_test`, denied commands, env-secret
  leakage, test-file edits, oversized outputs.

## 8. Expected metrics (hypotheses, not claims)

The only honest expectation is relative: the native agent should reach GREEN on most of the 9 synthetic bugs, and
the gate should refuse some of its patches for the same reasons it refuses the benchmark's fixed candidates. Numbers
will be reported as measured.

## 9. Later phases (unchanged order)

2. Langfuse tracing + metrics dashboard export. 3. SHA-tied code index → dense / sparse / hybrid retrieval in Qdrant
→ retrieval evaluation, kept only if it improves localization. 4. LangGraph wrapper over the same loop, issue-triggered
GitHub App runs with human approval, SWE-bench subset, docs.

## 10. Baseline test results

Run on 2026-10-03, branch `feature/cerberus-agent` at `daf199e`, Windows 11, Python 3.13.0, Docker 29.8.0,
image `cerberus-sandbox:py3.11`.

| Run | Passed | Failed | Skipped | Time |
| --- | --- | --- | --- | --- |
| Full suite (`python -m pytest -q`) | 218 | 0 | 18 | 565 s |
| `tests/test_docker_integration.py` (re-run once Docker was up) | 13 | 0 | 0 | 72 s |
| **Combined** | **231** | **0** | **5** | — |

The 5 remaining skips are the host-mode symlink tests in `tests/test_sandbox.py`: this Windows account cannot
create symlinks. They were previously verified inside a Linux container. Baseline is healthy.

# Cerberus — current architecture (audit before 2.0)

Audited on branch `feature/cerberus-agent`, created from `main` at `daf199e`. Code lives in `autonomous-pr-fixer/`;
paths below are relative to it.

## 1. Pipeline

`main.py::run_pipeline` drives one run through a state machine (`harness/pipeline_state.py`) with three terminal
states: `ADMITTED`, `REFUSED` (with a `RefusalCode`) and `ERROR`. A `finally` guard records `ERROR` if a path exits
without a decision.

```text
[1/9] TRIAGE         agents/triage_agent.py        reads files only, extracts error signatures / referenced files
[2/9] SETUP          agents/repo_setup_agent.py    the only networked phase; image committed afterwards
[3/9] RED ×3         agents/reproduction_agent.py  fails for the right reason, deterministically
[4/9] BASELINE       agents/regression_agent.py    full suite on base, JUnit by test ID
[5/9] LOCALIZATION   agents/localization_agent.py  lexical + AST ranking -> allowed files
[6/9] PATCH / GREEN  agents/patch_agent.py         bounded loop, test SHA-256 checked, GREEN ×3
[7/9] REGRESSION     agents/regression_agent.py    baseline-aware, flaky re-check, extended tests run at base
[8/9] SCOPE          harness/scope_gate.py         measured against base SHA, AST symbols, test-weakening refused
[9/9] ADMISSION      harness/admission_controller.py  conjunction, no discretion
```

Refusal codes: `NO_REPRODUCTION_TEST`, `RED_NOT_FAILING`, `RED_INVALID_TEST`, `RED_WRONG_REASON`,
`RED_UNRELATED_TEST`, `RED_NONDETERMINISTIC`, `RED_UNVERIFIABLE`, `NO_TEST_COMMAND`, `BASELINE_UNVERIFIABLE`,
`NOT_LOCALIZED`, `NO_PATCH_SOURCE`, `GREEN_NOT_REACHED`, `GREEN_NONDETERMINISTIC`, `REPRODUCTION_TEST_TAMPERED`,
`REGRESSION`, `REGRESSION_UNVERIFIABLE`, `SCOPE_VIOLATION`, `EMPTY_PATCH`.

**There is no static-analysis or security-check stage in the gate today.** The 2.0 brief lists both as existing; they
are not. See the upgrade plan for how this is handled.

Every run writes `artifacts/<run_id>/run.json` (schema 1.3): state history, refusal, RED evidence, baseline,
per-attempt patch history, regression results, blast radius, admission decision, base SHA, model config.

## 2. Existing components

| Area | Files | Notes |
| --- | --- | --- |
| Sandbox | `harness/docker_sandbox.py`, `sandbox/Dockerfile` | `Sandbox.exec/read_file/write_file`, Docker by default, `--network none`, `--cap-drop ALL`, limits, kill timeout, symlink-safe host I/O, PID-1 reaper; `host-unsafe` only for trusted fixtures |
| Patch application | `harness/diff_utils.py`, `agents/patch_agent.py` | `git apply` in the sandbox, rollback to base on failure, duplicate-diff abort |
| Test execution | `harness/junit.py`, `agents/regression_agent.py` | JUnit XML only, never exit codes |
| Scope | `harness/scope_gate.py` | files, lines, AST symbols, additions-only test edits |
| Repo policy | `harness/repo_config.py` | `.cerberus.yml` read from the base commit |
| Run artifact | `harness/run_artifact.py`, `harness/logger.py` | `run.json`, JSON event log |
| GitHub App | `github/app_service.py`, `webhook_handler.py`, `app_auth.py`, `client.py`, `run_store.py`, `check_report.py` | FastAPI webhook, HMAC, RS256 JWT, Check Runs, SQLite run store, PR-triggered only |
| PR publishing | `github/pr_publisher.py` | draft PR from a host-side clone, token never in argv/URL |
| Retrieval | `retrieval/ast_index.py`, `lexical_search.py`, `indexer.py`, `retriever.py` | AST symbol index (stdlib `ast`), lexical search, test→source links. In-memory, not tied to a SHA |

## 3. Existing agent integration

`agents/patch_sources.py` defines `PatchSource.generate_patch(PatchRequest) -> diff` with three implementations:

- `DiffPatchSource` — pre-written diffs (`--patch`, `--base/--head`).
- `LLMPatchSource` wrapping `agents/llm_patch_generator.py::LLMPatchGenerator` — **single prompt, no tools**: the
  localized file contents plus the previous structured failure go in, a unified diff comes out. litellm is the
  provider layer (`anthropic/…` default, OpenAI if only an OpenAI key exists). Token usage is recorded
  (`GenerationUsage`); cost is not.
- `ExternalAgentPatchSource` — runs an external CLI agent (`--agent claude-code`, `--agent-command`) on the host in a
  scratch export of the base commit, diffs the tree in Python. Never run for real (CLI not logged in).

`agents/llm_reproduction_synthesizer.py` can write the reproduction test with a model (`--use-llm-repro`).

## 4. Existing patch flow

`generator(attempt, feedback) -> diff` → policy check (lines/files) → `git apply` in sandbox → reproduction test →
on failure, roll back and feed `StructuredFailure` (status, error, traceback, failing test, changed files) to the
next attempt. `PATCH_MAX_ATTEMPTS` defaults to 5.

**Limitation that matters for 2.0:** the retry loop only covers GREEN. A patch that reaches GREEN and then fails
REGRESSION or SCOPE ends the run; the generator never sees that feedback.

## 5. Existing verification flow

RED (3 identical failing runs, right reason, imports repo code, mentions an issue symbol) → BASELINE → GREEN (3 runs,
unmodified test) → REGRESSION by test ID → SCOPE → ADMISSION. Unverifiable evidence is a refusal.

## 6. Existing benchmark

- `benchmark/instances/v1.yml`: 24 frozen instances over 3 templates (textkit, intervals, money), SHA-256 manifest
  (`benchmark/MANIFEST.json`), hidden ground-truth tests in `benchmark/hidden/`. Each instance carries a **fixed
  candidate patch**. It measures the gate, not patch generation.
- They cover **10 distinct bugs**; 9 are reproducible (`tk-06` is non-reproducible by design).
- Ground-truth bug location is the `file` of each `bug` op. intervals and money are single-module templates, so
  localization there is trivial; only textkit has a real choice between files.
- `evaluation/external_repos.py` + `evaluation/external/`: 3 real upstream bugs (sqlparse, boltons, more-itertools)
  at pinned commits, 11 variants.
- Reports of record: `benchmark/reports/v1-docker.md`, `evaluation/external/report.md`.

## 7. Existing limitations

- No tool-using agent; the model sees one file and cannot search, read tests or run anything.
- Gate feedback after GREEN does not reach the generator.
- No cost accounting, no tracing, no fix-rate metric.
- Localization has no measured accuracy; its candidate list is capped at 3.
- Retrieval is in-memory per run and not SHA-tied.
- The GitHub App verifies PRs only; it cannot start a repair from an issue.
- Docker path validated on Windows/Docker Desktop only.
- Repair-evaluation data is small: 9 synthetic + 3 external bugs.

## 8. Files Phase 1 will modify

- `autonomous-pr-fixer/requirements.txt` — no new dependency planned for Phase 1 (litellm and pydantic exist).
- `autonomous-pr-fixer/main.py` — one new opt-in CLI entry (`agent run`) routed to the new package; existing flags
  unchanged.
- New: `autonomous-pr-fixer/repair/` (agent package), `autonomous-pr-fixer/tests/test_repair_*.py`,
  `autonomous-pr-fixer/evaluation/repair_tasks/`, `docs/agent.md`, `docs/tools.md`.

## 9. Files that stay untouched

`harness/` (all gate modules, sandbox, scope, admission, state machine), `agents/reproduction_agent.py`,
`agents/regression_agent.py`, `agents/patch_agent.py`, `agents/localization_agent.py`, `benchmark/` (instances,
manifest, hidden tests, reports), `evaluation/benchmark.py`, `evaluation/external/`, `github/`, every existing test
file.

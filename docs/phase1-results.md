# Phase 1 results: native repair agent

Cerberus 2.0 Phase 1 added a tool-using repair agent in front of the unchanged Cerberus gate. This
report covers what was built, how it was evaluated, and every real-model result recorded. All numbers
come from `results/<experiment>/` run files; nothing is estimated. Branch `feature/cerberus-agent`.

## Summary

- **10 of 12 tasks have a valid real-model result, and all 10 were admitted by Cerberus on the first
  attempt.** All 10 admitted patches also pass the independent hidden tests: **0 false admissions in
  10 measured** (11 admitted runs in total, including a second admitted run on one task).
- **2 of 12 tasks have no valid result yet**, both for infrastructure reasons rather than a model or
  gate decision: `ext-sqlparse-332` (workspace deleted mid-run; bug since fixed) and
  `ext-moreitertools-last` (free-tier daily request limit). Both need a rerun.
- **Every admitted patch edited the correct file** (localization success 11 of 11 runs).
- **Cost: $0.** All runs used free tiers (Google AI Studio; OpenRouter `:free` models).
- **Not yet exercised by a real model:** the retry path. No real-model patch was refused, so feedback
  → revise → resubmit has only been demonstrated with the scripted demo and the fake-model tests.

With 12 small tasks, mostly written by the Cerberus author, these results show the pipeline works end
to end with real models. They are **not** a statistically meaningful fix rate.

## What was built

| Component | Where |
| --- | --- |
| One tool-using repair agent with a bounded loop (attempts, tool calls, tokens, cost, wall time) | `repair/agent.py` |
| Sandboxed tools: search, read, find symbol / references, read and run tests, git diff/status, edit, apply patch, reset | `repair/tools/` |
| Gate adapter: every submission is a normal `run_pipeline` run; feedback is read from `run.json` | `repair/verification/adapter.py` |
| Provider-independent model layer: Anthropic, Gemini (AI Studio), OpenRouter | `repair/llm/` |
| Langfuse tracing with secret redaction | `repair/observability/` |
| Metrics and the 12-task evaluation runner with hidden-test checks | `repair/metrics/`, `repair/evaluation.py` |

The verification gate (RED → BASELINE → GREEN → REGRESSION → SCOPE → ADMISSION) was not modified.

## Evaluation setup

- **Tasks:** 9 bugs from the frozen v1 benchmark (one per reproducible bug; issue and reproduction test
  from the benchmark, fixed candidate patches ignored) and 3 real upstream bugs (sqlparse, boltons,
  more-itertools) at their pinned parent commits.
- **Success** means Cerberus admitted the patch. **Hidden check:** the benchmark's hidden tests (v1) or
  the upstream fix commit's test file (external) must pass with the agent's patch.
- **Budgets per task:** 3 gate submissions, 50 tool calls, 400k tokens, 30 minutes.
- **Environment:** Windows 11, Python 3.13, Docker sandbox (`cerberus-sandbox:py3.11`).
- **Models:** `gemini-3.8-flash` (Google AI Studio free tier) and `poolside/laguna-s-2.1:free`
  (OpenRouter free tier; 15 s between requests, up to 10 retries).

## Results per task

| Task | Model | Result | Attempts | Tool calls | Tokens | Time | Patch | Hidden tests |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v1 textkit: truncate shortens text that fits | Laguna | ADMITTED | 1 | 15 | 90,631 | 17 min | `textkit/wrap.py` +1/−1 | pass |
| v1 textkit: truncate (second model) | Gemini 3.8 Flash | ADMITTED | 1 | 12 | 37,614 | 5 min | `textkit/wrap.py` +1/−1 | pass |
| v1 textkit: slugify ignores separator | Laguna | ADMITTED | 1 | 6 | 12,857 | 3 min | `textkit/slug.py` +1/−1 | pass |
| v1 textkit: camel_to_snake merges acronyms | Laguna | ADMITTED | 1 | 15 | 101,202 | 26 min | `textkit/case.py` +1/−1 | pass |
| v1 intervals: merge shrinks intervals | Laguna | ADMITTED | 1 | 13 | 49,419 | 50 min | `intervals.py` +1/−1 | pass |
| v1 intervals: merge out of order | Laguna | ADMITTED | 1 | 15 | 64,784 | 25 min | `intervals.py` +1/−1 | pass |
| v1 intervals: contains excludes end point | Gemini 3.8 Flash | ADMITTED | 1 | 11 | 34,170 | 14 min | `intervals.py` +1/−1 | pass |
| v1 money: split loses cents | Laguna | ADMITTED | 1 | 14 | 75,695 | 17 min | `money.py` +5/−1 | pass |
| v1 money: thousands separators | Laguna | ADMITTED | 1 | 8 | 24,452 | 8 min | `money.py` +1/−1 | pass |
| v1 money: banker's rounding | Laguna | ADMITTED | 1 | 8 | 15,522 | 12 min | `money.py` +2/−2 | pass |
| ext boltons: backoff with factor 1.0 | Laguna | ADMITTED | 1 | 18 | 264,958 | 17 min | `boltons/iterutils.py` +10/−3 | pass |
| ext sqlparse-332 | Laguna | **no valid result** | — | — | — | — | — | — |
| ext more-itertools: `last()` | Laguna | **no valid result** | — | — | — | — | — | — |

Wall time is dominated by free-tier pacing (15 s between requests plus throttling retries), not by
the gate.

### Per model (admitted runs)

| Model | Admitted runs | Tool calls (median, range) | Tokens (median, range) | Time (median) |
| --- | --- | --- | --- | --- |
| `poolside/laguna-s-2.1:free` | 9 | 14 (6–18) | 64,784 (12,857–264,958) | 17 min |
| `gemini-3.8-flash` | 2 | 11.5 (11–12) | 35,892 (34,170–37,614) | 10 min |

### How the agent searched (all runs with tool calls)

- 18 runs; 16 reached an edit. **Tool calls before the first edit:** median 4 (2–7). **Tokens through
  the first edit:** median 11,986 (6,850–65,930).
- Tool usage: read_file 50, search_code 38, run_test 37, edit_file 31, find_symbol 16, read_test 13,
  submit_patch 11, git_diff 6, git_status 4, apply_patch 4, find_references 1.

## Runs that did not count, and why

| Run | What happened | Counted as |
| --- | --- | --- |
| sqlparse-332 (`p1-laguna-run4`) | Agent found `sqlparse/sql.py` on its first search; later reads of existing files returned NOT_FOUND because a Windows temp cleaner deleted the workspace under `%TEMP%`. Timed out after 36 tool calls. | Invalid (infrastructure). Fixed in `e9b5419`: workspaces now under `artifacts/tmp`. |
| sqlparse-332 (`p1-laguna-run3`), more-itertools (`p1-laguna-run5`) | OpenRouter free-tier daily limit (50 requests) reached mid-task. | No result |
| Several Laguna and Gemini runs | Upstream throttling or daily quota before the agent finished. | No result; tasks rerun later |
| All `nvidia/nemotron-3-ultra-550b-a55b:free` runs | Provider reported "Service temporarily overloaded" on every task. | Excluded |
| `gemini-2.5-flash`, `qwen/qwen3-coder:free` | Model not available to this key (HTTP 404); 0 tokens used. | Excluded |

## Security observations

- The agent tried tools that do not exist 5 times (`shell` ×4, including `ls -la && find ...` and
  `cat -A textkit/case.py`; `grep` ×1). All were refused with `PERMISSION_DENIED`; no shell tool exists.
- With Docker unavailable, runs ended in `SETUP_FAILURE` without executing anything on the host.
- API keys stayed in the environment / a git-ignored `.env`; redaction is tested against key formats and
  secret-named environment variables.

## Engineering fixes made during evaluation

Each surfaced in a real run and is now covered by a test: Gemini/OpenRouter adapters (OpenAI-compatible,
raw turn replay); early stop on non-retryable provider errors; daily-quota detection without long
retries; retry of errors reported inside HTTP 200 bodies; cool-down and one retry on temporary
throttling; `run_test` on test directories; setup errors fail only their task; evaluation checkouts and
agent workspaces moved out of `%TEMP%`.

## Tests

| Run | Passed | Failed | Skipped |
| --- | --- | --- | --- |
| Full suite at the end of the Phase 1 build | 282 | 0 | 5 |
| Repair-agent tests after the evaluation fixes | 61 | 0 | 0 |

The 5 skips are the host-mode symlink tests on this Windows account.

## Limitations

- **Small, easy task set.** The 9 v1 bugs are single-module and author-written; localization there is
  trivial. Only the 3 external tasks resemble real repositories, and only 1 of those has a valid result.
- **No real-model refusal observed**, so the retry loop's value is not yet measured with a real model.
- **Free models only.** Results are for `poolside/laguna-s-2.1:free` and `gemini-3.8-flash`; no paid model
  was run. Free tiers do not guarantee deterministic sampling.
- **One run per task**, so there is no measure of run-to-run variance.

## Next steps

1. Rerun `ext-sqlparse-332` and `ext-moreitertools-last` with the workspace fix in place.
2. Phase 2 (retrieval) is justified by the localization benchmark, not by these 12 tasks: on 120
   SWE-bench Verified instances the Phase 1 localizer reaches Recall@10 = 0.487 (see
   `autonomous-pr-fixer/evaluation/phase2_report.md`).

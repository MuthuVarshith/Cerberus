# Phase 1 results

**Status: the agent, tools, gate integration, retry loop, tracing, metrics and evaluation runner are built and
tested. The real-model 12-task evaluation has not been run yet**, because no Anthropic credential is configured
on the development machine (`ANTHROPIC_API_KEY` unset, no `ant` CLI profile). Every repair-quality number this
document is meant to report is therefore **not yet measured**, and none is estimated or filled in by hand.

The command that produces them:

```bash
cd autonomous-pr-fixer
export ANTHROPIC_API_KEY=...        # plus LANGFUSE_* keys if tracing is wanted
python main.py agent evaluate --limit 12 --experiment-id phase1-sonnet55-run1
```

Budget: at most $1.25 per task and $15 in total (`MAX_COST_PER_TASK`, `MAX_EVALUATION_COST`); the runner stops
starting tasks when the total is reached and records the rest as `SKIPPED_BUDGET`. Output:
`results/phase1-sonnet55-run1/{config,summary,metrics}.json`, `task-results.jsonl`, `report.md`.

## What was measured

### Tests

Run on Windows 11, Python 3.13.0, Docker 29.8.0, branch `feature/cerberus-agent` — see the table at the end of
this document.

### Harness checks against the real gate (scripted model, not repair quality)

These verify that the pipeline works end to end; the patches were scripted, so they say nothing about how well a
model repairs bugs.

| Check | Isolation | Result |
| --- | --- | --- |
| `agent demo`: iv-04's regressing candidate, then iv-01's correct one | Docker | attempt 1 `REFUSED / REGRESSION` (`tests.test_intervals::test_merge_zero_length_interval`), feedback returned, attempt 2 `ADMITTED` |
| Evaluation runner on `v1-textkit-truncate…` with the reference fix | Docker | `ADMITTED`, hidden check passed, localization `textkit/wrap.py` correct, 17 s |
| Evaluation runner on `ext-moreitertools-last` with the upstream fix | Docker | `ADMITTED`, hidden check (upstream test file at the fix commit) passed, 44 s |
| Fixture: GREEN failure → fix; REGRESSION → fix; SCOPE violation → narrowed patch; exhausted attempts | host (trusted fixture) | each refusal code produced by the real gate and fed back; final states as expected |

While the evaluation-runner check was running, Docker Desktop stopped. The agent reported `SETUP_FAILURE` for
both tasks and executed nothing on the host, which is the intended fail-closed behaviour. The results above come
from the re-run after Docker was restored.

## Requested results

| # | Item | Value |
| --- | --- | --- |
| 1 | Fix rate | not yet measured |
| 2 | Admission rate | not yet measured |
| 3 | Failure breakdown | not yet measured |
| 4 | Localization success | not yet measured (ground truth is wired for all 12 tasks) |
| 5 | Average attempts | not yet measured |
| 6 | Cost per successful fix | not yet measured |
| 7 | Median repair time | not yet measured; gate time alone is ~15–45 s per submission on these repositories |
| 8 | Tool usage | not yet measured |
| 9 | Model usage | not yet measured |
| 10 | Failure examples | none from a real model yet; the scripted demo shows the REGRESSION path |
| 11 | Security observations | see below |
| 12 | Is retrieval necessary? | **cannot be decided without the evaluation** |
| 13 | Recommended next step | run the evaluation above, then decide |

## Security observations (from building and testing)

- All repository code the agent runs goes through `Sandbox.exec`. With Docker unavailable the agent stopped with
  `SETUP_FAILURE` rather than running anything on the host.
- Tests confirm refusal of: absolute, home and `..` paths; `.git`, `.github`, `.cerberus`, credential-like files;
  edits to any test file, the reproduction test and `.cerberus.yml`; `run_test` targets carrying shell syntax
  (`;`, `&&`, `|`, `$(…)`); tools not on the allow-list.
- Redaction is tested against secret-named keys, Anthropic/GitHub token formats and the values of secret-named
  environment variables; a planted `ANTHROPIC_API_KEY` value does not appear in anything sent to the (fake)
  Langfuse client.
- Residual: the model loop and the host-side read tools run on the host. Repository text reaches the model and can
  attempt prompt injection; the tool policy, not the prompt, is what bounds the outcome. Whatever the model is
  persuaded to do, it can only change non-test source files in its workspace, and the gate judges the diff.

## How to read the evaluation once it exists

- The decision rule for Phase 2 is set in advance: **retrieval is worth building only if localization is a
  measured bottleneck.** Concretely, if tasks fail with the agent never reading or editing the ground-truth file
  (localization success low, `SCOPE_VIOLATION` or `GREEN_NOT_REACHED` on wrong files), a retrieval phase has
  something to improve. If the agent finds the right file and fails on the fix itself, retrieval will not help,
  and the next step is prompt and feedback quality instead.
- With 12 tasks, a single task moves the fix rate by about 8 points. Report rates with their counts, and do not
  read differences of one or two tasks as an effect.
- On the v1 tasks localization is close to trivial (two of three templates have one source module), so the
  retrieval question is decided mostly by the three external tasks, which is too few. A credible answer needs a
  larger external set (Phase 4's SWE-bench subset) whatever the 12-task result shows.

## Tests

| Run | Passed | Failed | Skipped | Time |
| --- | --- | --- | --- | --- |
| Existing suite (baseline at `daf199e`, Docker tests included) | 231 | 0 | 5 | — |
| New repair tests (`test_repair_unit.py`, `test_repair_gate_integration.py`) | 51 | 0 | 0 | — |
| **Full suite at the end of Phase 1** (`python -m pytest -q`) | **282** | **0** | **5** | 522 s |

The 5 skips are the host-mode symlink tests (`tests/test_sandbox.py`): this Windows account cannot create
symlinks, as at baseline. No existing test was changed or removed.

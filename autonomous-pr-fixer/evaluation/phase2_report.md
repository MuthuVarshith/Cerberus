# Phase 2 report: code retrieval

Status: **Step 1 (baseline) complete.** Steps 2–4 not started; awaiting approval.

## Localization benchmark (`cerberus-localization-v1`)

- **Source:** SWE-bench Verified, test split (500 instances). `source_sha256` and the sample's SHA-256
  are in [`localization/MANIFEST.json`](localization/MANIFEST.json); rebuilding gives the same sample.
- **Sample:** 120 instances, stratified by repository in proportion to its share, minimum 2 per
  repository, seed 20261008. Per repository: django 53, sympy 18, sphinx 11, matplotlib 8,
  scikit-learn 8, astropy 5, pytest 5, xarray 5, pylint 2, requests 2, seaborn 2, flask 1.
- **Task:** given the issue text and the repository at the base commit, rank the repository's non-test
  `.py` files. **Gold:** the non-test `.py` files changed by the reference patch. Checked: every gold
  file is in its instance's candidate set (0 exceptions).
- **Leakage flag:** 34 of 120 issues name a gold file (path, dotted module or file name). Results are
  reported for both subsets.
- **Metrics:** Recall@k = fraction of gold files in the top k, averaged; All@k = every gold file in the
  top k; MRR = mean of 1 / rank of the first gold file (0 if never ranked).
- **Safety:** sources are exported with `git archive` / `git cat-file` as text; nothing is checked out
  through hooks, imported or executed.

No LLM is involved in this benchmark. With 120 instances, differences of a few points between
methods are within noise; the report will say so where it applies.

## Step 1 results

### Baseline A: Phase 1 localizer on the benchmark

The gate's existing lexical + AST localizer (`agents/localization_agent.py`, via
`repair/retrieval/baseline.py`), full ranking instead of the production top 3. Run on Windows 11,
Python 3.13, Cerberus `cbaaa39`.

| Subset | n | R@1 | R@5 | R@10 | All@1 | All@5 | All@10 | MRR | Gold never ranked |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| All | 120 | 0.171 | 0.355 | 0.487 | 0.158 | 0.342 | 0.467 | 0.288 | 40 |
| Issue names a gold file | 34 | 0.353 | 0.516 | 0.654 | 0.353 | 0.500 | 0.618 | 0.482 | 3 |
| Issue does not name one | 86 | 0.099 | 0.291 | 0.421 | 0.081 | 0.279 | 0.407 | 0.211 | 37 |

- Median ranking time 11.1 s per instance over a median of 777 candidate files. The localizer ranks
  only files it has a lexical or symbol hit for (median 29), so in 40 of 120 instances no gold file
  appears at all.
- By repository, Recall@10: pytest 0.0 (5/5 never ranked), pylint 0.0 (2/2), xarray 0.2, django
  0.405 (18/53 never ranked), sphinx 0.5, sympy 0.53, astropy 0.6, matplotlib 0.75, scikit-learn
  0.875; requests, seaborn and flask 1.0 on 1–2 instances each (too few to read anything into).
- Full per-instance results: [`localization/results/phase1-baseline.json`](localization/results/phase1-baseline.json).

### Baseline B: how the Phase 1 agent searches (12-task eval runs)

From the recorded runs (`evaluation/localization/agent_behaviour.py`; models Gemini 3.8 Flash and
`poolside/laguna-s-2.1:free`; Nemotron runs excluded because the provider was overloaded):

- 18 runs with tool calls; 16 reached an edit.
- **Tool calls before the first edit:** median 4 (min 2, max 7).
- **Model tokens through the first edit:** median 11,986 (min 6,850, max 65,930).
- Tool usage: read_file 50, search_code 38, run_test 37, edit_file 31, find_symbol 16, read_test 13,
  submit_patch 11, git_diff 6, git_status 4, apply_patch 4, find_references 1; refused calls to
  non-existent tools: shell 4, grep 1.
- In every run that edited, the first edited file was a correct one; the two runs that never edited
  were both `ext-sqlparse-332` (see below).

### Data-integrity note

`ext-sqlparse-332` in run `p1-laguna-run4` is **invalid**: the agent found `sqlparse/sql.py` on its
first search, then reads of existing files returned NOT_FOUND because the workspace under `%TEMP%`
was deleted mid-run. Fixed in `e9b5419` (workspaces under `artifacts/tmp`). That task and
`ext-moreitertools-last` (cut off by the free-tier daily limit) still need a clean rerun.

## Reading of Step 1

- On the 12 Phase 1 tasks, finding the file was **not** the bottleneck: those repositories are small
  and the agent reached the right file in about 4 tool calls.
- On SWE-bench-sized repositories, the Phase 1 localizer finds a gold file in the top 10 for under
  half the instances, and for 37 of the 86 issues that don't name a file it never ranks one. That is
  the gap retrieval has to close, and the benchmark can measure it.

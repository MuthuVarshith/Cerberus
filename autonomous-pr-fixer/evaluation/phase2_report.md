# Phase 2 report: code retrieval

Status: **Step 1 complete; Step 2 in progress** (chunking and BM25 measured on 120 instances; dense,
hybrid and rerank to be measured on a 40-instance subset).

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

## Step 2 results (in progress)

### Chunking and BM25 on all 120 instances

Chunks: Tree-sitter functions, classes, methods and module-level code (line windows for definitions
over 150 lines and for unparseable files). BM25: Okapi, k1 = 1.2, b = 0.75, identifiers kept whole
and split on snake_case / camelCase, path and symbol name indexed with each chunk. File score: either
the best chunk (`bm25`) or the sum of the file's three best chunks (`bm25-sum3`). Cerberus `d3620df`.

| Method | R@1 | R@5 | R@10 | All@10 | MRR | Gold never ranked | Median query time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Phase 1 localizer | 0.171 | 0.355 | 0.487 | 0.467 | 0.288 | 40 | 11.1 s |
| BM25, best chunk | 0.354 | 0.650 | 0.776 | 0.742 | 0.531 | 0 | see JSON |
| BM25, sum of top 3 | 0.435 | 0.741 | 0.864 | 0.842 | 0.600 | 0 | see JSON |

Per-instance results: `localization/results/{phase1-baseline,bm25,bm25-sum3}.json`.

### Paired significance

`evaluation/localization/significance.py`: exact McNemar test on hit@k (a gold file within the top k)
and a paired bootstrap (10,000 resamples of instances, seed 20261010) for the difference in mean
Recall@k and MRR. "A-only / B-only" counts instances only one method hits.

**BM25 (best chunk) vs Phase 1, n = 120**

| Metric | Diff | 95% CI | A-only / B-only | p (exact) |
| --- | --- | --- | --- | --- |
| Recall@1 | +0.183 | [+0.096, +0.275] | 8 / 32 | 0.00018 |
| Recall@5 | +0.295 | [+0.177, +0.415] | 16 / 53 | 9.1e-06 |
| Recall@10 | +0.289 | [+0.175, +0.404] | 13 / 48 | 7.7e-06 |
| MRR | +0.243 | [+0.157, +0.330] | — | — |

**BM25 (best chunk) vs Phase 1, excluding the 6 instances Phase 1 cannot rank (n = 114)**

| Metric | Diff | 95% CI | A-only / B-only | p (exact) |
| --- | --- | --- | --- | --- |
| Recall@1 | +0.171 | [+0.079, +0.263] | 8 / 29 | 0.00075 |
| Recall@5 | +0.280 | [+0.159, +0.403] | 16 / 49 | 5.1e-05 |
| Recall@10 | +0.269 | [+0.148, +0.387] | 13 / 44 | 4.7e-05 |
| MRR | +0.226 | [+0.136, +0.315] | — | — |

**BM25 sum of top 3 vs best chunk, n = 120**

| Metric | Diff | 95% CI | A-only / B-only | p (exact) |
| --- | --- | --- | --- | --- |
| Recall@1 | +0.081 | [+0.020, +0.147] | 4 / 14 | 0.031 |
| Recall@5 | +0.091 | [+0.038, +0.149] | 2 / 14 | 0.0042 |
| Recall@10 | +0.087 | [+0.037, +0.142] | 1 / 10 | 0.012 |
| MRR | +0.069 | [+0.030, +0.109] | — | — |

Reading: BM25 beats the Phase 1 localizer by a wide, statistically clear margin, and the gain holds
with the 6 structurally unrankable instances removed. Sum-of-top-3 also beats best-chunk, but by a
smaller margin and **it was chosen after seeing these 120 results** (post hoc), so its advantage over
best-chunk should be treated as provisional until confirmed on data not used to choose it. The
p-values are not corrected for the several comparisons made.

### Like-for-like check: Phase 1 vs BM25

| Input | Phase 1 localizer | BM25 |
| --- | --- | --- |
| Query text | full issue text (first line as title, rest as body; both used) | full issue text |
| Candidate files | the commit's non-test `.py` files, exported to disk | the same files, read in memory (same `python_files` filter) |
| Test files | excluded by `is_test_path` before ranking; **additionally** the localizer drops any path containing the substring `test` | excluded by `is_test_path` only |
| Files ranked | only files with a lexical or symbol hit (median 29) | every file with any matching token |

The inputs are identical; the differences in the last two rows are behaviour of the production
localizer itself, measured as it is. The substring filter makes 6 of 120 instances impossible for
Phase 1 (5 pytest files under `src/_pytest/`, and `sympy/utilities/randtest.py`); the n = 114 table
above removes them.

### Aggregation frozen

From here on every retriever (vector, hybrid, hybrid + rerank) uses the same file aggregation,
**sum of top 3** (`repair/retrieval/methods.py: AGGREGATION`), fixed before any of those methods was
scored, so their comparisons are not tuned on the benchmark.

### Dense retrieval: plan and cost

Dense retrieval is measured on a fixed 40-instance stratified subset
(`localization/instances-v1-sub40.jsonl`, sympy and matplotlib excluded, seed 20261009), because
embedding all 120 instances is impractical on the development laptop (Intel i5-1335U, clock capped
at 1.3 GHz during the runs): the 120 need 158,359 distinct chunks embedded; the subset needs 63,372.
Measured embedding throughput: `BAAI/bge-small-en-v1.5` (384-d) about 5–7 chunks/s,
`jinaai/jina-embeddings-v2-base-code` (768-d) 1.68 chunks/s. bge-small runs first; jina-code is
embedded only if bge-small vector search adds measurable value over BM25.

# Phase 2 notes: code retrieval

Status: draft. Sections marked **pending** depend on the dense / hybrid / rerank measurements.
Numbers come from `autonomous-pr-fixer/evaluation/phase2_report.md`; nothing here is estimated.

## Why Phase 2 exists

On the 12 Phase 1 tasks the agent found the right file in a median of 4 tool calls, so localization
was not the bottleneck there. Those repositories are small. On 120 SWE-bench Verified instances the
Phase 1 localizer ranked a gold file in its top 10 for under half the instances (Recall@10 0.487) and
never ranked one in 40. Phase 2 asks whether retrieval closes that gap, and keeps only components
that measurably do.

## What was built

| Component | File | Notes |
| --- | --- | --- |
| Localization benchmark | `evaluation/localization/` | 120 instances from SWE-bench Verified, stratified by repository, fixed seed, SHA-256 manifest; gold = non-test files changed by the reference patch |
| Text-only source export | `repair/retrieval/source.py` | `git archive` / `git cat-file` from bare mirrors; no checkout hooks, no imports, symlinks skipped |
| Code-aware chunking | `repair/retrieval/chunking.py` | Tree-sitter functions, classes, methods (qualified `Class.method`), module-level code; line windows for long definitions and unparseable files |
| BM25 | `repair/retrieval/bm25.py` | Okapi BM25 with an inverted index; identifiers kept whole and split on snake_case / camelCase; path and symbol indexed with each chunk |
| File aggregation | `repair/retrieval/aggregate.py` | chunk scores rolled up to files; frozen at sum of top 3 for every retriever |
| Vector search | `repair/retrieval/embeddings.py`, `vector.py` | fastembed (ONNX, CPU, no remote code) + Qdrant embedded mode; vectors cached in SQLite by content hash |
| Hybrid | `repair/retrieval/fusion.py` | reciprocal rank fusion (k = 60) of the BM25 and vector rankings |
| Reranker | `repair/retrieval/rerank.py` | cross-encoder over the top 30 fused chunks (`jina-reranker-v1-turbo-en`) |
| Agent tool | `repair/tools/retrieve.py`, `repair/retrieval/service.py` | `retrieve_code(query, k)`; index cached per commit, only edited files re-chunked; off by default |
| Statistics | `evaluation/localization/significance.py` | exact McNemar on hit@k, paired bootstrap CIs |

## Design choices and trade-offs

- **Measure before building.** The benchmark and baseline came first, so every component is judged
  against a fixed reference rather than intuition.
- **Paired tests, not just averages.** Every method sees the same instances, so differences are tested
  per instance (McNemar, paired bootstrap). With 120 instances, gaps of a few points are noise.
- **Aggregation frozen early.** Sum-of-top-3 beat best-chunk for BM25 but was chosen after seeing the
  120 results, so it was then fixed for every other retriever to avoid tuning on the test set.
- **Like-for-like baseline.** Both methods get the same issue text and candidate files. The production
  localizer also drops paths containing `test`, which makes 6 instances impossible for it; the
  comparison is reported with and without them.
- **BM25 implemented in-house.** ~70 lines, deterministic, no dependency, and code-aware tokenization
  matters more than the library.
- **ONNX embeddings, no PyTorch.** fastembed runs models on CPU without downloading PyTorch or running
  model-repository code. The cost is speed: on the development laptop bge-small embeds about 5–7
  chunks/s plugged in (under 1/s on battery) and jina-code 1.7/s, so dense retrieval is measured on a
  fixed 40-instance subset.
- **Untrusted repository text.** Indexing only parses text. Snippets are returned between explicit
  `REPOSITORY_DATA` markers and labelled as data, because repository text can contain
  instruction-like content.
- **Tool, not prompt stuffing.** Retrieval is a tool the model chooses to call; nothing is injected into
  every prompt. It is off by default so Phase 1 behaviour is unchanged unless a run opts in.

## Results so far

| Method (120 instances) | R@1 | R@5 | R@10 | MRR |
| --- | --- | --- | --- | --- |
| Phase 1 localizer | 0.171 | 0.355 | 0.487 | 0.288 |
| BM25, best chunk | 0.354 | 0.650 | 0.776 | 0.531 |
| BM25, sum of top 3 | 0.435 | 0.741 | 0.864 | 0.600 |

BM25 vs Phase 1: Recall@10 +0.289 [95% CI +0.175, +0.404], McNemar p = 7.7e-06 (n = 120).

Dense, hybrid and rerank: **pending** (40-instance subset).
End-to-end effect on the agent: **pending** (Step 4).

## Interview questions

1. **Why did you build a localization benchmark instead of just adding RAG to the agent?**
   Because the 12 end-to-end tasks could not show a difference: the agent already found the right file
   in about 4 tool calls there, and 12 tasks are too few to compare methods. A no-LLM benchmark of 120
   SWE-bench instances is free to run, gives per-instance paired results, and showed a real gap
   (Recall@10 0.487) for retrieval to close.

2. **How do you know BM25's improvement is real and not noise or a biased comparison?**
   Paired tests on the same 120 instances: exact McNemar on hit@k and a 10,000-resample bootstrap;
   Recall@10 improved by +0.289 with a 95% CI of [+0.175, +0.404]. The inputs were audited to be
   identical; the baseline's own substring filter made 6 instances impossible, and the result holds
   with those removed (n = 114). The aggregation variant picked after seeing the data is labelled post
   hoc and frozen before scoring the other retrievers.

3. **Why might vector search not help on code, and how would you decide whether to keep it?**
   **Pending the measurement.** The decision rule is fixed in advance: keep dense or hybrid retrieval only
   if it beats BM25 on the same 40 instances under the same aggregation, with a paired test, and is
   worth its indexing cost; otherwise BM25 stays alone.

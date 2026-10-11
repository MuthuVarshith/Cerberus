# Cerberus 2.0 — final notes

Branch `upgrade/final` (not merged into `main`). Every number below was measured; each names its
source. Anything not listed is not yet measured.

## What was built

| Area | What | Where |
| --- | --- | --- |
| Gate (unchanged) | RED×3 → BASELINE → GREEN×3 → REGRESSION → SCOPE → ADMISSION in an offline, capability-dropped Docker sandbox | `harness/`, `main.py` |
| Repair agent (Phase 1) | one tool-calling model, sandboxed tools, bounded retries on any refusal, budgets, Langfuse tracing; Anthropic / Gemini / OpenRouter / OpenAI-compatible providers | `repair/` |
| Frozen interface | `run_repair(task, config) -> RepairResult` (v1.0) | `repair/interface.py` |
| Retrieval (Phase 2) | Tree-sitter chunks, BM25, fastembed + Qdrant vectors, RRF hybrid, cross-encoder rerank (dropped), `retrieve_code` tool (off by default) | `repair/retrieval/`, `repair/tools/retrieve.py` |
| Localization benchmark | 120 SWE-bench Verified instances + fixed 40-instance subset, paired significance tests | `evaluation/localization/`, `evaluation/phase2_report.md` |
| LangGraph (Phase 3) | graph orchestration with typed state, SQLite checkpointer + `agent resume`, optional human review; `orchestrator=loop` stays default | `repair/orchestration/`, `docs/phase-3-langgraph.md` |
| GitHub approval (Phase 4a) | `/cerberus fix` or `cerberus-fix` label → run → issue comment + Check Run → `/cerberus approve` → draft PR; write-permission check, no fork auto-runs, rate limits | `github/issue_*.py`, `github/repair_job.py`, `docs/phase-4-github.md` |
| SWE-bench harness (Phase 4b) | fixed 25-instance subset, gold-patch validation through the gate, fake-model dry run, official scoring path | `evaluation/swebench_e2e.py`, `docs/how-to-run-swebench.md` |
| Live runs | resumable daily free-tier batches, ablation aggregator | `docs/how-to-run-live.md`, `evaluation/agent_ablation.py` |
| Docs / CI / packaging | GitHub Actions (ruff, mypy, pytest, import smoke), `pyproject.toml` + `requirements.lock`, README diagram, security doc | `.github/workflows/ci.yml`, `docs/security.md` |

## Measured results

| Result | Value | Source |
| --- | --- | --- |
| Phase 1, real models | 10 of 10 valid tasks admitted on attempt 1, all passing hidden tests (0 false admissions); `gemini-3.8-flash`, `poolside/laguna-s-2.1:free` | `docs/phase1-results.md` |
| Localization, 120 instances | Recall@10: Phase 1 localizer 0.487 → BM25 0.776 → BM25 sum-of-top-3 0.864 | `evaluation/phase2_report.md` |
| BM25 vs Phase 1 (n = 120) | Recall@10 +0.289 [95% CI +0.175, +0.404], McNemar p = 7.7e-06 | same |
| Sum-of-top-3 vs best chunk (n = 120) | Recall@10 +0.087 [+0.037, +0.142], p = 0.012 — chosen post hoc | same |
| Subset (n = 40), Recall@10 | phase1 0.438, BM25-sum3 0.900, vector bge-small 0.912, hybrid 0.963, hybrid + rerank 0.863 | same |
| Hybrid vs BM25-sum3 (n = 40) | Recall@1 +0.150 [+0.050, +0.263], p = 0.016; R@5/R@10 not significant; uncorrected | same |
| Reranker | no gain (R@5 −0.050, p = 0.62), +14.07 s median per query → dropped | same |
| Cold hybrid index, one Django commit | 51 min on the development laptop → agent tool recommendation `RETRIEVAL=bm25` | same |
| Live run `s4-baseline-r1-d2` (commit b5b00c6, laguna free, T=0, seed 7) | boltons ADMITTED + hidden pass; sqlparse TOKEN_LIMIT_EXCEEDED (409k tokens); more-itertools cut off by daily quota | `docs/how-to-run-live.md` |
| SWE-bench gold-patch validation (5 of 25) | 4 ADMITTED (flask-5014, pytest-10356, pylint-8898, sphinx-10466), 1 REFUSED RED_WRONG_REASON (requests-5414) | `docs/how-to-run-swebench.md` |
| LangGraph smoke (fake model, real gate, fixture) | REGRESSION refusal → diagnose → second patch ADMITTED → review → PR files | `docs/phase-3-langgraph.md` |

Not yet measured: end-to-end effect of the retrieval tool (only 1 of 18 planned runs complete), LangGraph
vs loop with a real model, SWE-bench resolved rate, jina-code embeddings.

## Design choices and trade-offs

- **The gate decides, the agent proposes.** Every orchestrator and the GitHub App go through the same
  unchanged gate; nothing else can admit a patch.
- **Measure before building.** The localization benchmark came before any retriever; the reranker was
  built, measured and dropped.
- **BM25 for the agent despite hybrid winning at the top ranks:** hybrid's indexing cost on CPU exceeds the
  run budget for large repositories.
- **SQLite for state, Qdrant for vectors:** exact-key records and checkpoints vs similarity search; Postgres
  would replace SQLite for a multi-server deployment.
- **Free tiers:** model quota (50 requests/day) dominates the time to finish live experiments.

## Known limitations

- Official SWE-bench scoring cannot run on Windows (`swebench` needs the Unix `resource` module); run it
  from WSL/Linux. No SWE-bench images pulled yet.
- `RepairResult` has no review/PR fields; LangGraph duplicates the loop's model-call accounting because
  the `agent.py` refactor was not applied.
- The lock file was produced from this Windows/Python 3.13 environment; CI resolves Linux-only packages.
- Same-repo PRs that already carry the `cerberus` label may still re-run on new commits without a fresh
  write-permission check (fork PRs are excluded) — see `docs/security.md`.
- Small evaluation sets: 12 repair tasks, 40-instance retrieval subset; several p-values are uncorrected.
- Laptop CPU: embeddings run at ~5–7 chunks/s plugged in, under 1/s on battery.

## Interview questions

1. Why is the verification gate separate from the agent, and how do you guarantee the agent can't admit
   its own patch?
2. How did you show BM25 beats the old localizer, and why paired tests (McNemar, bootstrap) rather than
   comparing averages?
3. Hybrid retrieval scored best on the subset — why does the agent still default to BM25?
4. You chose sum-of-top-3 after seeing the results. Why is that a problem and what did you do about it?
5. Why did the reranker get dropped, and how would you decide differently with a GPU?
6. What does LangGraph add over the plain loop, and how does checkpointing make runs resumable?
7. How does the GitHub App stop an untrusted user or fork PR from running code or opening PRs?
8. What is a false admission, how do you measure it, and why can't the gate alone eliminate it?

# Plan changes (Cerberus 2.0 final phase)

Every deviation from the requested plan, with its reason. Newest last.

## Lead setup

1. **Agents started before the final plan arrived.** Five agents (retrieval eval, live runs, LangGraph,
   GitHub approval, SWE-bench) were already running when the final six-agent plan was received. They
   were kept and re-briefed rather than restarted, to avoid losing work. They map to A–E; agent F
   (docs/CI/packaging) was launched afterwards.
2. **Branch names.** Running agents use `upgrade/phase-2` (A), the main checkout (B, runs only),
   `upgrade/phase-3-langgraph` (C), `upgrade/phase-4-github` (D), `upgrade/phase-4-swebench` (E).
   F uses `agent-f/docs-ci`. They are merged into `upgrade/final` in the order A, C, D, E, F.
3. **Agent A works in the main checkout** (`upgrade/phase-2`), not a separate worktree, because the
   SWE-bench mirrors and the embedding caches (several GB, git-ignored) live there. It is the only
   agent that commits in that checkout.
4. **Agent B runs from the main checkout, not a pinned worktree**, because the model key (`.env`),
   evaluation caches and Docker artifacts live there. Pinning is approximated: every experiment's
   `config.json` records the git commit and a dirty flag, and agent A is barred from changing the
   runtime of the agent, tools and model layer while B runs. B makes no commits.
5. **Frozen interface:** `autonomous-pr-fixer/repair/interface.py` (v1.0) defines
   `run_repair(task, config) -> RepairResult`. It wraps the existing runner so Phase 1 behaviour is
   unchanged; the orchestrator (loop or LangGraph) is selected inside via `config`.
6. **Aggregation frozen:** chunk-to-file aggregation is fixed at sum-of-top-3 for every retriever
   (`repair/retrieval/methods.py: AGGREGATION`). Sum-of-top-3 was chosen **after** seeing the BM25
   results on the 120-instance benchmark (post hoc); it was frozen before vector, hybrid and rerank
   were scored.
7. **File ownership** (to avoid merge conflicts): README.md, dependency manifests/lock files and
   `.github/workflows/` belong to F only; C, D and E put their docs in their own `docs/*.md` files and
   list new dependencies in their reports for F to pin.
8. **Usage-limit interruption (2026-10-11).** All six agents stopped mid-task when the session usage
   limit was reached. They were resumed from their own transcripts after the reset rather than
   restarted. At that point only agent A had produced results (the hybrid + rerank run on the
   40-instance subset); C–F had only merged the frozen interface and B had not launched any run.
   Running six large-model agents in parallel consumes the usage allowance quickly; further
   interruptions are possible and are handled the same way.

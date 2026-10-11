# How to run the live Step 4 experiments

## Prerequisites
- Docker up: `docker version --format "{{.Server.Version}}"`; image `cerberus-sandbox:py3.11` present. If Docker is down every task ends in SETUP_FAILURE (this voided `s4-baseline-r1-d1b`). Fix: stop Docker processes, `wsl --shutdown`, rename (never delete) `%LOCALAPPDATA%\Docker\run` and `%LOCALAPPDATA%\docker-secrets-engine` to `*.stale-<timestamp>`, restart Docker Desktop.
- Laptop on the charger (runs on battery are much slower).
- `autonomous-pr-fixer/.env` holds the OpenRouter key and model (`poolside/laguna-s-2.1:free`). Never print or copy it.
- Free tier: ~50 requests/day, reset 00:00 UTC. One task can use 15-40 requests.

## Commands (from `autonomous-pr-fixer/`, in the background)
```
LLM_TEMPERATURE=0 LLM_SEED=7 python -u main.py agent evaluate --only ext-sqlparse-332 ext-boltons-backoff ext-moreitertools-last --experiment-id <ID> --retrieval none|bm25 > artifacts/<ID>.log 2>&1
```
Task order is fixed by `cases.json` (sqlparse, boltons, moreitertools). Ids must be new.

Next batches:
1. `s4-baseline-r1-d3 --retrieval none --only ext-moreitertools-last` (completes repeat 1 with d2)
2. `s4-bm25-r1-d1 --retrieval bm25` (all 3)
3. `s4-baseline-r2-d1`, `s4-bm25-r2-d1`, then `-r3-`; on a quota stop resume the SKIPPED tasks with `-d2`, `-d3` ...

## Aggregate
```
python evaluation/agent_ablation.py --config baseline=s4-baseline-r1-d2+s4-baseline-r1-d3,s4-baseline-r2-d1,... --config bm25=s4-bm25-r1-d1,...
```
`+` pools one repeat across folders; SKIPPED_* tasks are excluded.

## Void folders
- `s4-baseline-r1-d1`: run killed, no task-results.
- `s4-baseline-r1-d1b`: Docker down, both tasks SETUP_FAILURE, no model calls.

## Results so far (measured)
`s4-baseline-r1-d2` (commit b5b00c65e426, clean; laguna-s-2.1:free, T=0, seed 7, cost $0):

| task | status | attempts | tool calls | tokens | gate | hidden |
| --- | --- | --- | --- | --- | --- | --- |
| ext-sqlparse-332 | TOKEN_LIMIT_EXCEEDED | 0 | 22 | 409273 | - | NOT_MEASURED |
| ext-boltons-backoff | ADMITTED | 1 | 15 | 134592 | ADMITTED | True |
| ext-moreitertools-last | LLM_FAILURE (daily quota 429) | 0 | 4 | 11757 | - | NOT_MEASURED |

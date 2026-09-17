# Cerberus — `autonomous-pr-fixer`

This directory contains the implementation. Documentation lives in the [repository root README](../README.md);
progress, decisions and known limitations are tracked in [`CERBERUS_PROGRESS.md`](../CERBERUS_PROGRESS.md).

Quick start (Python 3.11+):

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python main.py --demo
```

| Path | Contents |
| :--- | :--- |
| `main.py` | CLI and pipeline orchestration |
| `agents/` | triage, reproduction, localization, patch loop, regression, repo setup, model generators |
| `harness/` | sandbox, pipeline state machine, admission controller, diff utilities, run artifacts, config |
| `retrieval/` | Python AST index and lexical search |
| `github/` | webhook handler and PR publisher |
| `examples/` | `rate_calculator` demo fixture; preserved VoteVault scenario |
| `evaluation/` | gate smoke scenarios and metrics reporter |
| `tests/` | automated tests |
| `artifacts/` | `run.json` per run (git-ignored) |

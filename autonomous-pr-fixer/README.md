# Cerberus — `autonomous-pr-fixer`

This directory contains the implementation. **The canonical documentation lives in the [repository root README](../README.md)** — architecture, the four verification gates, sandbox hardening, evaluation provenance, and known limitations.

Quick start (Python 3.11+):

```bash
python -m venv .venv
pip install -r requirements.txt
python -m pytest tests/ -q
```

| Path | Contents |
| :--- | :--- |
| `agents/` | triage, localization, reproduction, patch, regression |
| `harness/` | admission controller, pipeline state machine, sandbox, diff utils, run artifacts, interpreter resolution, config |
| `github/` | webhook handler, PR publisher |
| `retrieval/` | AST index + lexical search |
| `evaluation/` | SWE-bench-style runner, baseline, ablation, metrics reporter |
| `tests/` | 54 tests |
| `artifacts/` | `run.json` audit trail, one directory per run |

Before citing any evaluation figure, read [§9 Evaluation](../README.md#9-evaluation) — the gate behaviour is measured, but token counts and localization accuracy are stipulated constants. CI for this project lives at the repository root, in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

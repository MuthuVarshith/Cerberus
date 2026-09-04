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
| `harness/` | admission controller, pipeline state machine, sandbox, diff utils, run artifacts, config |
| `github/` | webhook handler, PR publisher |
| `retrieval/` | AST index + lexical search |
| `evaluation/` | SWE-bench-style runner, baseline, ablation, metrics reporter |
| `tests/` | 54 tests |
| `artifacts/` | `run.json` audit trail, one directory per run |

Before running outside Windows, or before citing any evaluation figure, read [§10 Setup](../README.md#10-setup--reproduction) and [§9 Evaluation](../README.md#9-evaluation) — several metrics are stipulated rather than measured, and 17 call sites hardcode the Windows `py` launcher.

# How to run the SWE-bench Verified harness

Code: `autonomous-pr-fixer/evaluation/swebench_e2e.py` (CLI) and `evaluation/swebench_tasks.py`
(instance -> work repo, reproduction test, `.cerberus.yml`). Data: `CERBERUS_SWEBENCH_DIR`
(default `...\Cerberus\autonomous-pr-fixer\artifacts\swebench`) holding `verified.parquet` and
`mirrors/<owner>__<repo>.git`. Work dirs: `autonomous-pr-fixer/artifacts/swebench-e2e/` (git-ignored).

## Dataset and subset

- SWE-bench Verified test split, 500 rows; parquet SHA-256 is recorded in
  `evaluation/swebench_e2e/subset-v1.manifest.json`.
- `subset-v1.jsonl` (25 instances) is fixed. Rule: (repo, version) has a Python 3.11 recipe in
  `RECIPES`, and FAIL_TO_PASS tests extract statically; order by `sha256("cerberus-swebench-v1:" + id)`,
  take the first 25 (29 eligible). Regenerate with `python evaluation/swebench_e2e.py select`.
- Excluded repos and reasons: `EXCLUDED_REPOS` in `swebench_tasks.py` (django: runtests.py not
  pytest; sympy; matplotlib/scikit-learn/astropy: need a compiler; seaborn: pinned pandas has no 3.11
  wheels). Old versions of included repos (e.g. requests < 2.26, pytest < 6.2, sphinx 3.x/4.x,
  xarray 0.12, pylint < 2.15) are excluded because they do not run on Python 3.11.

## How a task is built

1. Clone the mirror, check out `base_commit`, apply `test_patch` and a generated `.cerberus.yml`,
   commit both as the new base (tests present, bug present).
2. `.cerberus.yml`: per-repo pip setup, test command = pytest on the FAIL_TO_PASS/PASS_TO_PASS files
   and files touched by `test_patch` (not the whole suite), scope = source package + docs/changelog,
   max 10 files / 600 lines, no test modifications.
3. Reproduction test: the patched test files with every unselected test removed, non-root
   `conftest.py` files inlined, `pytest_plugins` merged, `__file__` rewritten to the original path,
   relative imports made absolute, and the test file's import dir put on `sys.path`.
   `Class::method` ids keep the class with only the selected methods; parametrized ids keep the whole
   base function (all parameters run). Inherited methods, nested classes and conflicting names
   across files are refused with a reason.
4. Ground truth files: non-test files in the gold patch.

## Commands

```
cd autonomous-pr-fixer
# environment / reproduction-test check: gold patch through the unchanged gate (no LLM)
python evaluation/swebench_e2e.py validate --limit 5
# dry run through repair.interface.run_repair with a scripted fake model (no LLM)
python evaluation/swebench_e2e.py run --experiment-id dry-gold --fake gold --only psf__requests-5414
python evaluation/swebench_e2e.py run --experiment-id dry-giveup --fake give-up --limit 2
# official scoring (resolved) with the swebench package; checks disk first
python evaluation/swebench_e2e.py score --experiment-id dry-gold --check-disk-only
python evaluation/swebench_e2e.py score --experiment-id dry-gold
# real agent run (calls the configured model; costs quota)
python evaluation/swebench_e2e.py run --instances evaluation/swebench_e2e/validated-instances.jsonl \
    --only-admitted --limit N --experiment-id ID [--retrieval bm25]
python evaluation/swebench_e2e.py score --experiment-id ID
```

Scoring needs `swebench` (tested 5.0.2) in an interpreter given by `SWEBENCH_PYTHON` (default
`artifacts/swebench-venv/Scripts/python.exe`: `python -m venv artifacts/swebench-venv &&
artifacts/swebench-venv/Scripts/python -m pip install swebench`). It runs
`swebench.harness.run_evaluation` on `results/swebench/<ID>/predictions.jsonl` with a local dataset
file, pulls prebuilt x86_64 images from the `swebench` Docker Hub namespace, and writes
`resolved`, `admitted`, `false_admission` (admitted and not resolved), tokens and cost per instance to
`results.jsonl` and `summary.json`. It refuses to pull images with less than 120 GB free.

## Limitations

- Contamination: SWE-bench fixes are public and may be in model training data.
- Environment mismatch: Cerberus validates in its Python 3.11 sandbox with its own pins; the official
  harness uses SWE-bench's images. An instance can pass one and fail the other.
- Reproduction tests run whole parametrized functions and lose conftest hooks (only fixtures and
  plugins carry over).
- Benchmark quality: Verified was human-screened, but some tests still check incidental behaviour.
- Agent numbers are only meaningful for instances whose gold patch was ADMITTED in `validate`.

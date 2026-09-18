# Cerberus — Autonomous Patch Verification Gate

A verification gate that decides whether a bug-fix patch — from a person, a script or a coding agent — has earned a
pull request. It never writes the fix; it produces evidence and one decision: `ADMITTED`, `REFUSED` with a code, or
`ERROR`.

**Agents write patches; Cerberus decides whether a patch has earned a PR.**

## Technical highlights

- **Evidence-based admission pipeline.** RED (the reproduction test fails on the unpatched commit *for the right
  reason*, three times) → baseline → GREEN (the unmodified test passes, three times) → baseline-aware regression →
  scope → admission. All outcomes come from JUnit XML; an unreadable result is a refusal, never a pass.
- **Baseline-aware regression by test ID.** Tests already failing before the patch are reported, not blamed on it;
  suspected regressions are re-checked on base code and reclassified as flaky if they fail there too; existing test
  files the patch only extended are run in their base form, so added lines cannot change the verdict.
- **Scope measured, not claimed.** Changed files (including untracked and deleted), line counts and the enclosing
  Python function or class of every changed line, computed from the diff against the recorded base SHA. Rewriting the
  tests that judge the patch is a refusal.
- **Hardened Docker sandbox.** One networked setup phase committed to an image, then offline execution with dropped
  capabilities, no host environment, memory/CPU/PID limits and an in-container kill timeout; fails closed when Docker
  is unavailable. Verified from inside real containers, not just configured.
- **Patch-source abstraction.** Diff file, existing change (`base..head`), external coding agent, or a model — all
  judged identically. The agent runs outside the sandbox with file-only tools and cannot run the tests that grade it.
- **Measured against an ungated baseline.** A frozen 24-instance benchmark with hidden ground-truth tests, plus three
  real bugs in open-source repositories, with the false-admission rate published rather than hidden.

## Stack

Python 3.11+, pytest, JUnit XML, Docker, Git plumbing, FastAPI (GitHub App webhook), SQLite (run store),
GitHub REST API with RS256 App JWTs, PyYAML, `cryptography`. No framework for the agent loop; no model required to
run the gates.

## Key engineering contributions

- Designed the gate as a **state machine with three terminal states** and a refusal code per gate, so every run ends
  in a decision that can be audited from `run.json` — including per-attempt patch history.
- Made verification **independent of the patch's author**: the reproduction test is hashed before and after,
  regression is judged against pre-patch results, and scope is computed from the repository, not from the diff header.
- **Found and fixed a sandbox escape class**: host-side harness I/O followed symlinks that repository code (or a
  commit) could plant, which on Linux/macOS turns a harness write into an arbitrary file write. Added symlink-refusing
  path resolution with `O_NOFOLLOW`, plus per-command process cleanup that closes the swap race.
- **Validated on code I did not write**, which exposed two defects my own fixtures could not: real fixes append tests
  to existing files (falsely refused), and a diff ending in a blank context line was corrupted before `git apply`.
- Built a **reproducible evaluation harness** (frozen instance set with a SHA-256 manifest; pinned upstream commits
  for external repositories) so every published number can be re-run.

## Results

**Benchmark** — 24 seeded instances, hidden tests as ground truth, Docker sandbox
([`benchmark/reports/v1-docker.md`](autonomous-pr-fixer/benchmark/reports/v1-docker.md)):

| | Cerberus | Ungated baseline (admit when the reproduction test passes) |
| --- | --- | --- |
| Admitted | 11 | 20 |
| Wrong patches approved | 4 (36%) | 13 (65%) |
| Correct fixes admitted | 7 of 7 | 7 of 7 |
| Rejection accuracy | 12 of 17 | — |

**External repositories** — real upstream bugs in sqlparse, boltons and more-itertools
([`evaluation/external/report.md`](autonomous-pr-fixer/evaluation/external/report.md)): **11 of 11** runs ended in
the expected state and refusal code, over test suites of 492, 445 and 679 tests.

## Security

Untrusted repository code runs with no network, no capabilities, no host environment variables, memory and PID
limits, a kill timeout, and no process surviving its command; the host never follows a workspace symlink; containers
orphaned by a killed process stop and remove themselves; GitHub tokens never enter the sandbox and never appear in
argv or a remote URL; the sandbox fails closed rather than falling back to the host; Cerberus never merges.

## Links

Source: [github.com/MuthuVarshith/Cerberus](https://github.com/MuthuVarshith/Cerberus) ·
summary page: [`site/index.html`](site/index.html) (static, runs nothing) ·
recording script: [`DEMO.md`](DEMO.md) · technical Q&A: [`INTERVIEW.md`](INTERVIEW.md) ·
engineering log: [`CERBERUS_PROGRESS.md`](CERBERUS_PROGRESS.md)

## Scope and next steps

A research project for Python/pytest repositories. In the benchmark, every bad patch the tests could expose was
refused (13 of 13); a patch that passes every visible test can still be wrong, which the benchmark measures rather
than hides. Next: live runs with a
real coding agent (the external coding-agent integration is built), the GitHub App on a hosted server, and a larger
evaluation set.

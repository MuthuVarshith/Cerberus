# Cerberus — recording script

A 8–10 minute screen recording. Every command below is real and was run on this machine; the outputs quoted are
what it actually printed. Nothing here is staged.

**Thesis to say out loud at the start and the end:** *Agents write patches. Cerberus decides whether a patch has
earned a pull request.*

---

## Before you hit record

```bash
docker info --format "{{.ServerVersion}}"
```

Docker must answer with a version (start Docker Desktop first). Then, once:

```bash
docker build -t cerberus-sandbox:py3.11 sandbox/
```

Setup checklist:

- Terminal in `autonomous-pr-fixer/`, font large enough to read at 1080p, window about 100 columns wide.
- A second tab or editor window ready for `run.json` and `benchmark/reports/v1-docker.md`.
- Close anything noisy: no notifications, no other Docker containers running.
- Total runtime of the live commands below is about 4 minutes; the rest is narration.

---

## 1. Title and problem — 45 s, no commands

Say, in your own words:

> A coding agent can produce a plausible patch for almost any bug report in seconds. The bottleneck is not writing
> the patch, it is deciding whether it deserves a human's attention. An agent that judges its own work is the weakest
> possible evidence. Cerberus never writes the fix — it decides whether someone else's patch has earned a PR.

Show the pipeline diagram at the top of `README.md` while you say it.

---

## 2. A full verified run — 2 min

```bash
python main.py --demo
```

Runs in about 25 seconds. Pause on the output and walk through the nine stages:

```text
[3/9] REPRODUCTION / RED GATE
  [OK] RED confirmed in 3/3 runs: .cerberus.test_reproduce::test_zero_total_returns_zero (ZeroDivisionError)
[4/9] BASELINE
  [OK] 2 tests, 0 already failing
[6/9] PATCH LOOP / GREEN GATE
  [OK] GREEN confirmed in 3/3 runs
[7/9] REGRESSION
  -> 2/2 passing; newly failing 0, pre-existing 0, flaky 0
[8/9] SCOPE
  -> files ['rate_calculator.py'] (+2/-0); symbols ['rate_calculator.py::calculate_rate']
RESULT ... Final state: ADMITTED
```

Points worth making, one sentence each:

- **RED ran three times**, and the failure is a `ZeroDivisionError` raised inside repository code — not an import
  error, not a typo in the test.
- **The baseline was recorded before the patch**, so tests that were already failing cannot be blamed on it.
- **GREEN also ran three times**, on the *unmodified* reproduction test.
- **Scope** reports the actual changed file and the enclosing function, computed from the diff, not from what the
  patch claimed to touch.
- Everything after dependency installation ran **offline inside a container**.

---

## 3. The evidence file — 1 min

The last line of the previous output prints the artifact path (`artifacts/run_<id>/run.json`; `--demo` names the run
itself and ignores `--run-id`). This prints the evidence from the newest run:

```bash
python -c "import json,os,glob; p=max(glob.glob('artifacts/*/run.json'), key=os.path.getmtime); d=json.load(open(p)); print(p); print(json.dumps({k: d[k] for k in ('final_state','red_gate','baseline','regression_results','blast_radius','sandbox')}, indent=1)[:1600])"
```

Then open that file in the editor and scroll through it once.

Say: every run writes this, admitted or refused. It carries the base commit SHA, the reproduction test and its
output, per-attempt patch history, the sandbox mode, and the gate-by-gate decision. This is what a reviewer reads
instead of trusting a summary.

---

## 4. Real repositories, admitted and refused — 3 min

```bash
python evaluation/external_repos.py --only sqlparse-332
```

About three minutes for four runs against the real sqlparse repository at the commit before an upstream fix (the
first run also clones it). Talk while it runs; the summary at the end is the payload:

| Variant | Result |
| --- | --- |
| `upstream-fix` | `ADMITTED` — the real maintainer fix, 492 tests, none newly failing |
| `upstream-commit` | `ADMITTED` — the unmodified upstream commit, test additions and all |
| `regressing` | `REFUSED / REGRESSION` |
| `test-weakening` | `REFUSED / SCOPE_VIOLATION` |

Pause on the two refusals — they are the interesting half:

- The **regressing** patch fixes the reported bug and also strips identifier quoting. Cerberus names the three real
  upstream tests it breaks: `test_parse_access_symbol`, `test_parse_square_brackets_notation_isnt_too_greedy`,
  `test_sqlite_identifiers`.
- The **test-weakening** patch is the same change plus edits to those three tests so they pass. That is refused as a
  `SCOPE_VIOLATION`: a patch may add tests, but changing the tests that judge it is not allowed. And because the
  regression gate runs existing test files in their base form, editing them could not have helped anyway.

If you are short on time, skip the live run and show the committed report instead:

```bash
cat evaluation/external/report.md
```

---

## 5. The benchmark, including what it gets wrong — 1.5 min

```bash
cat benchmark/reports/v1-docker.md
```

Do not run the benchmark live; it takes about seven minutes. Show the committed report of record and read the
headline honestly:

- Cerberus admitted **11** of 24; **4 of those 11 were wrong**.
- The ungated baseline — admit whenever the reproduction test passes, which is what an agent that trusts itself does
  — admitted 20, of which 13 were wrong.
- Both admitted **7 of 7 correct fixes**: the gate is not simply strict.

Then say the important sentence:

> All four false admissions are plausible-but-wrong patches that pass every visible test, including the reproduction
> test. Visible tests cannot establish semantic correctness, so this is a limit of the approach, not a bug I have
> not got round to fixing. The hidden tests that catch them are ground truth the gate never sees.

---

## 6. Security model — 1 min

```bash
python -m pytest tests/test_docker_integration.py --collect-only -q
```

Instant, and it lists what is actually verified against a real Docker engine. Optionally run one:

```bash
python -m pytest tests/test_docker_integration.py -q -k symlinks
```

Say: untrusted repository code runs with no network, no capabilities, no host environment variables, memory and PID
limits, and a kill timeout. The host never follows a symlink inside the workspace — a repository could otherwise
redirect a harness write outside it. No process outlives its command, and a container whose driver is killed stops
and removes itself. If Docker is missing, the run fails closed rather than falling back to the host.

---

## 7. Optional: a real coding agent — 2 min, requires login

Only if you have run `claude` → `/login` first (see [`README.md`](README.md), *Real coding agents*).

```bash
git clone --quiet https://github.com/andialbrecht/sqlparse.git artifacts/agent-demo
```
```bash
git -C artifacts/agent-demo checkout --quiet --detach f66d12c245412f28c58f045b646eb53c0e691b8b^
```
```bash
python main.py --repo artifacts/agent-demo --issue 332 --run-id agent_sqlparse --title "get_real_name returns an intermediate component for names with more than two dotted parts" --body "For a fully-qualified identifier such as db.schema.tbl.col, Identifier.get_real_name() returns 'schema' instead of 'col'. get_name() is affected too. Two-part names like a.b are correct." --repro-test evaluation/external/tests/sqlparse-332-reproduce.py --agent claude-code
```

The expected verdict is `ADMITTED` if the agent finds the same one-line anchoring bug the maintainer did, and a
refusal with a named gate if it does not. Compare its diff with the upstream fix afterwards:

```bash
git -C artifacts/agent-demo diff --stat f66d12c245412f28c58f045b646eb53c0e691b8b^ f66d12c245412f28c58f045b646eb53c0e691b8b -- sqlparse/
```

The agent gets the issue, the reproduction test and file-only tools in a throwaway copy of the base commit — no
shell, so it cannot run the tests and cannot decide that its own patch is good. Cerberus judges the diff it leaves
behind exactly like any other patch. **Record whatever happens**, including a refusal; a refusal of an agent patch
is a better demo than a staged success.

---

## 8. Close — 30 s

> Cerberus is an admission layer for changes produced by coding agents. It does not write code and it does not merge
> anything. It produces evidence and one decision: admitted, refused with a reason, or error. It is a research
> prototype — the Docker path has only run on Windows, the GitHub App has only been tested against a fake API, and
> four out of eleven admissions in my own benchmark were wrong, which I can show you.

---

## If something goes wrong on camera

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Docker daemon is not reachable` | Docker Desktop not started | start it, wait for the whale icon, rerun |
| `Sandbox image 'cerberus-sandbox:py3.11' is not available` | image not built | `docker build -t cerberus-sandbox:py3.11 sandbox/` |
| First external run is slow | it clones sqlparse | run it once before recording; the clone is cached under `artifacts/` |
| `Not logged in` from the agent segment | headless Claude Code not authenticated | run `claude`, then `/login`, then retry — or skip segment 7 |
| A run ends in `ERROR` | the sandbox could not give the isolation it promises | that is fail-closed behaviour; say so and show `run.json` |

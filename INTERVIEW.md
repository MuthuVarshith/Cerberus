# Cerberus — technical Q&A

Answers describe what the code in this repository does. Where a claim is untested, it says so.

---

### 1. What problem does Cerberus solve?

Coding agents produce plausible patches fast; what is scarce is a reason to believe one. Cerberus is the reviewer's
side of that exchange: it takes a patch from anywhere — person, script, coding agent, model — and decides whether it
has earned a pull request, with evidence the patch's author does not control. It never writes the fix. Every run
ends in exactly one terminal state: `ADMITTED`, `REFUSED` with a code naming the gate that stopped it, or `ERROR`
(`harness/pipeline_state.py`), and writes `artifacts/<run_id>/run.json`.

### 2. Why can't we simply trust Claude Code?

Not because agents are bad at patches, but because self-assessment is the weakest evidence available: the agent
chose the change, chose which tests to run, and "the test I just wrote passes" says nothing about the tests that
already existed. My benchmark measures that directly. The ungated baseline — admit whenever the reproduction test
passes, which is what an agent trusting itself does — admitted 20 of 24 candidates, 13 of them wrong. The same
candidates through the gates: 11 admitted, 4 wrong. The gate is not magic; it removes a specific class of mistakes
(regressions, scope creep, weakened tests, invalid reproductions) and it still admits plausible-but-wrong patches.

### 3. What is RED → GREEN?

RED is proof the bug exists: the reproduction test must fail on the unpatched base commit *for the right reason*.
Not an import error, not a collection error, not a typo — it must fail on an assertion, a `pytest.fail`/`DID NOT
RAISE`, an exception type the issue named, or an exception raised inside repository code, and it must import
repository code and mention a symbol the issue refers to (`agents/reproduction_agent.py`). GREEN is proof the patch
fixes that bug: the *unmodified* test then passes — the test's SHA-256 is compared before and after, so a patch that
rewrites the test is refused as `REPRODUCTION_TEST_TAMPERED`. Both sides come from JUnit XML, never from an exit
code.

### 4. Why run RED multiple times?

Because a test that fails once may be failing for a reason that has nothing to do with the bug — time, ordering,
randomness, a flaky fixture. RED runs three times by default (`budgets.red_runs`) and the outcomes must be identical;
if they are not, the run is refused as `RED_NONDETERMINISTIC`. GREEN is re-verified three times for the same reason:
a fix that works two runs out of three is not a fix. One benchmark instance is exactly this case and is refused on
that code.

### 5. How does baseline-aware regression work?

Before the patch is applied, the repository's own test command runs on the base commit and the JUnit results are
recorded (`agents/regression_agent.py`). After the patch, the suite runs again and the two are compared by test ID:
a test that passed at baseline and now fails, errors, or has disappeared is a regression; a test that was already
failing is reported but not blamed on the patch. Suspected regressions get one re-check with the patch stashed — if
they fail on base too, they are recorded as flaky rather than counted. Existing test files that the patch only added
lines to are checked out at the base commit for this run, so an added skip marker, early `return` or import-time
monkeypatch cannot change how the pre-existing tests judge the patch. Unreadable or missing JUnit output is
`REGRESSION_UNVERIFIABLE`: no evidence is never a pass.

### 6. How does scope enforcement work?

Scope is measured from the workspace against the recorded base SHA, not from what the patch claims
(`harness/scope_gate.py`). Untracked files are included via intent-to-add, so a new file counts; deletions count;
line counts and the enclosing Python function or class of every changed line are computed with `ast` inside the
sandbox. Policy: every changed file must be in the allowed scope (the localized file, or globs from `.cerberus.yml`),
at most 3 files and 200 lines by default, and no line of an existing test file may be changed or deleted — adding
tests is fine, rewriting the ones that judge you is not. That last rule is what refuses the classic "make the test
match the bug" patch, and it fired on a real repository during external validation.

### 7. How does the Docker sandbox work?

Two phases (`harness/docker_sandbox.py`). Dependency installation needs the network, so it runs in a setup container
with `--network bridge`, whose filesystem is then committed to an image. Everything after that — reproduction,
patching, tests — runs in a fresh container from that image with `--network none`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, 2 GiB memory, 2 CPUs, 256 PIDs, a tmpfs `/tmp` and a `timeout -s KILL` inside the
container. If Docker or the image is unavailable, construction fails; there is no host fallback. `--unsafe-local-sandbox`
exists for trusted local fixtures, prints a warning, and can never publish. The container is verified, not just
configured: `tests/test_docker_integration.py` asserts these properties from inside real containers.

### 8. How are credentials protected?

No host environment variable reaches a container — the container gets four fixed variables, and a test plants a
secret in the host environment and asserts it does not appear in `env` inside the container. Host-unsafe mode passes
an allowlist that drops anything matching token/secret/password/key patterns. The GitHub token never enters the
sandbox at all: publishing happens on a separate host-side clone, and the token travels through
`GIT_CONFIG_COUNT/KEY/VALUE` as a base64 extraheader — never in argv, never in a remote URL — with redaction applied
to any git error text (`github/pr_publisher.py`).

### 9. Why are symlinks a security concern?

The harness reads and writes files inside the workspace *from the host*: the candidate patch, the reproduction test,
JUnit reports, `pyproject.toml`, `.cerberus.yml`. A repository can commit a symlink, and code running in the
container can create one on the bind mount; on Linux and macOS the host sees a real symlink and follows it. That
turns a harness write into an arbitrary file write as the host user — with partly attacker-controlled content, since
in App mode the patch comes from the PR. The fix is that host-side access refuses any symlinked path component and
opens with `O_NOFOLLOW`, scanners skip links, and `.cerberus.yml` may not be a link. I also closed the race: every
command ends by killing the processes it left behind, so nothing can swap a path between the check and the open. The
test for this fails against the previous code on Linux — the write escaped the workspace — and passes now.

### 10. What is the admission controller?

A conjunction with no discretion (`harness/admission_controller.py`): GREEN reached, regression-free, scope
acceptable, and a substantive change against the base commit. All four must hold; the first that does not becomes
the refusal code. It exists as its own module so the decision is one readable function rather than a chain of
`if` statements spread through the pipeline, and so the reasons it prints are the reasons recorded in `run.json`.

### 11. What happens when Cerberus is uncertain?

It refuses, and says which evidence was missing. There is no "probably fine" path: no reproduction test, a test that
cannot be parsed, a test command that produces no readable JUnit, a suite that collects zero tests, a missing base
commit, an unavailable sandbox — each maps to a refusal code or `ERROR`. A `finally` guard records `ERROR` if any
path exits without a decision, so a crash cannot be mistaken for a pass. The design rule is that absent evidence is
never evidence of absence: unverifiable is a refusal, not a shrug.

### 12. Why do false admissions still exist?

Because a patch can pass every test that exists and still be wrong. Four of the eleven patches Cerberus admitted in
the benchmark are "plausible-but-wrong": they fix the reported symptom, pass the reproduction test and the whole
existing suite, and fail hidden specification tests the gate never sees. No amount of gate engineering closes that —
visible tests define the visible contract. The honest framing is that Cerberus removes mistakes tests *can* see
(regressions, scope creep, weakened tests, tests that never reproduced the bug) and cannot remove mistakes tests
cannot see. Measuring it was the point of the benchmark; hiding it would have made the project worthless.

### 13. What did external-repository testing reveal?

Two defects the synthetic benchmark could not, both found on real upstream commits. First, the scope gate refused
the unmodified upstream sqlparse fix, because real fixes append their test to an existing test file — my rule was
"existing test files are untouchable", which is right in spirit and wrong in practice. The fix allows additions and
makes the regression gate run those files in their base form, so added lines cannot influence the verdict. Second, a
diff whose last context line is an empty source line was corrupted before `git apply`, because the diff was stripped
of trailing whitespace; the real boltons commit was refused as a corrupt patch for that reason. Moving to a real
Docker engine also prompted the review that found the symlink issue. That is the argument for testing against code
you did not write: my own fixtures agreed with my own assumptions.

### 14. Why isn't Cerberus production-ready?

Four things. The Docker path has only run on Windows with Docker Desktop; the symlink and permission defences matter
most on Linux, where they are covered by unit tests run inside a container rather than by a Linux host. The GitHub
App is written and tested against a fake GitHub API, never installed on a real repository. No real coding agent has
been run through it — the adapter is exercised by a scripted stand-in. And the evaluation is 24 author-written
instances plus three external bugs: enough to show the gates behave as designed, not enough to estimate how often
they help. Separately, a deliberately malicious repository could forge its own JUnit results, because repository code
runs in the same process as the test runner; Cerberus verifies patches to trusted-but-buggy repositories.

### 15. What would you build next?

In evidence order, not excitement order: run a real coding agent end to end; exercise the sandbox on a Linux host;
install the GitHub App on a real repository; teach the App to verify PRs that append a test to an existing file (the
CLI already does, the App still wants exactly one new test file); then a larger and less biased evaluation set. Only
after that would I touch repair quality — improving a repair loop while verification is still unproven optimises the
wrong end of the system, which is why `Phase 3` is deliberately deferred in `CERBERUS_PROGRESS.md`.

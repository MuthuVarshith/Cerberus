# Cerberus security model

Describes what the code does today. Items marked **(in progress, agent D)** are being built on
`upgrade/phase-4-github` and are not yet on this branch.

## Threat model

- **Repository code is untrusted.** Tests, `conftest.py`, setup scripts and source files may be
  buggy or hostile. They execute only inside the Docker sandbox.
- **Repository text is untrusted input to the model.** Issue text, code and retrieved snippets can
  contain instruction-like content (prompt injection). The tool boundary, not the prompt, limits what
  a misled model can do: it can only produce a diff, and the unchanged gate judges the diff.
- **The model is untrusted.** It cannot mark its own patch admitted; only a gate `run.json` with
  `ADMITTED` counts (`repair/interface.py`).
- Out of scope: a deliberately malicious repository can forge its own test results, because its code
  runs in the same process as its test runner. Cerberus targets trusted-but-buggy repositories.

## Sandbox (`harness/docker_sandbox.py`)

- Docker by default. `--unsafe-local-sandbox` / `CERBERUS_SANDBOX=host-unsafe` is for trusted
  fixtures only and can never publish.
- **Offline after setup:** dependencies install in a separate setup container that is committed to an
  image; every later container runs with `--network none`.
- `--cap-drop ALL`, `--security-opt no-new-privileges:true`, `--memory 2g` (no extra swap),
  `--cpus 2`, `--pids-limit 256`, a 512 MB tmpfs `/tmp`.
- Fixed four-variable environment (`HOME`, `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`,
  `GIT_TERMINAL_PROMPT`); no host variable, token or key enters the container.
- Every command runs under `timeout -s KILL` inside the container and leftover processes are killed
  afterwards; a PID-1 reaper removes a container whose host process died
  (`CERBERUS_SANDBOX_MAX_LIFETIME_SECONDS`, default 4 h).
- Host-side reads and writes of workspace files refuse symlinks (`O_NOFOLLOW`), so planted links
  cannot redirect harness I/O.
- **Fails closed:** without Docker, the daemon or the image, the run ends in `ERROR`/`SETUP_FAILURE`;
  there is no silent fallback to the host.
- Asserted from inside real containers by `tests/test_docker_integration.py` (skipped when Docker or
  the image is unavailable).

## Agent tool policy (`repair/tools/base.py`)

- Only registered tools can be called; unknown names return `PERMISSION_DENIED`. **There is no shell
  tool** and no git operation beyond fixed read-only diff/status and the internal reset.
- Paths must be repository-relative: absolute paths, drive letters, `~` and any `..` component are
  refused; `.git/`, `.github/` and the harness directory are not readable; credential-like names
  (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials.json`, ...) are refused.
- Writes are additionally refused for test files (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`),
  the reproduction test and `.cerberus.yml`.
- `run_test` targets must match `path.py(::node)*` with no spaces or shell characters and must be
  test files; they run in the offline sandbox.
- The submitted patch is the measured workspace diff, re-checked against the write policy before a
  gate run.

**Observed in Phase 1 real-model runs** ([phase1-results.md](phase1-results.md)): the models tried
non-existent tools 5 times: `shell` 4 times (including `ls -la && find ...` and
`cat -A textkit/case.py`) and `grep` once. All were refused with `PERMISSION_DENIED`.

## Retrieval safety (`repair/retrieval/`, `repair/tools/retrieve.py`)

- Sources are exported as text with `git archive` / `git cat-file`; no checkout hooks, filters,
  imports or execution; symlinks are skipped. Indexing only parses text (Tree-sitter, BM25,
  ONNX embeddings via fastembed, no remote model code).
- `retrieve_code` returns snippets between `<<<REPOSITORY_DATA` / `REPOSITORY_DATA>>>` markers,
  labelled as untrusted repository content, not instructions. Retrieval is off by default.

## Secrets

- Keys come only from the environment or a git-ignored `.env` (loaded by `main.py agent`, never
  overriding existing variables). `.env.example` holds no values.
- Provider keys are sent only in the `Authorization` header, never in URLs or argv; the generic
  `openai-compatible` provider requires an `https://` base URL.
- Keys are never passed to the sandbox, written to `agent_run.json` / `agent.log`, or sent to Langfuse.
  Langfuse receives no prompts, completions, file contents or test output; everything sent passes
  `redact` (secret-named keys, Anthropic/OpenAI/GitHub/Slack/AWS/JWT/PEM token formats, literal values
  of secret-named environment variables).
- The GitHub token never enters the sandbox, a command line or a remote URL (`github/pr_publisher.py`).
- CI scans for token-shaped strings and needs no secrets.

## GitHub App (`github/`)

Implemented:

- Every webhook is verified with HMAC-SHA256 (`X-Hub-Signature-256`, constant-time compare); a
  missing secret fails closed.
- Short-lived installation tokens from an RS256 App JWT; the private key is read from env or a file.
- Commands (`/cerberus verify`, `/cerberus repair`) and the `cerberus` label require the actor to have
  write, maintain or admin permission; bot senders are ignored; duplicate deliveries are dropped.
- Runs per repository are serialized, capped per hour (`CERBERUS_MAX_RUNS_PER_HOUR`, default 20), and
  killed at a wall-clock timeout (`CERBERUS_RUN_TIMEOUT_SECONDS`, default 3600). The App refuses to run
  without the Docker sandbox.
- Output is a Check Run or a draft PR. **Cerberus never merges.**

Known gap today: new commits on a PR that already carries the `cerberus` label are re-verified
without a fresh permission check, including PRs from forks.

**(in progress, agent D)**: issue-triggered `/cerberus fix` runs, `/cerberus approve` human approval
before any PR is opened, and no automatic runs on fork PRs. See
[phase-4-github.md](phase-4-github.md).

"""
Cerberus on real repositories written by other people.

Each bug in evaluation/external/cases.json is pinned to an upstream fix commit in
an open-source project. The run checks out the commit before the fix, uses the
test the fix commit added as the reproduction test, and runs the full CLI in the
Docker sandbox on several variants: the upstream fix, the unmodified upstream
commit as an existing change, and hand-made wrong patches or bad tests.

    python evaluation/external_repos.py                   # all bugs and variants
    python evaluation/external_repos.py --only sqlparse-332
    python evaluation/external_repos.py --record          # also write evaluation/external/report.md

Docker only: this runs third-party code, so there is no host-unsafe option.
Nothing in the report is typed in by hand; every value comes from a run.json.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from harness.docker_sandbox import _remove_tree  # noqa: E402

SPEC_DIR = os.path.join(ROOT, "evaluation", "external")
DEFAULT_OUTPUT = os.path.join(ROOT, "artifacts", "external-repos")
RUN_TIMEOUT_SECONDS = 1800


def _git(cwd: Optional[str], *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def mirror(cache: str, name: str, url: str) -> str:
    """A local no-checkout clone; cloning reads data and runs no repository code."""
    path = os.path.join(cache, name)
    if not os.path.isdir(os.path.join(path, ".git")):
        _git(None, "clone", "--quiet", "--no-checkout", "-c", "core.autocrlf=false", url, path)
    return path


def checkout(source: str, dest: str, sha: str) -> None:
    if os.path.exists(dest):
        _remove_tree(dest)
    _git(None, "clone", "--quiet", "--no-checkout", "--no-hardlinks", "-c", "core.autocrlf=false", source, dest)
    _git(dest, "checkout", "--quiet", "--detach", sha)


def upstream_inputs(repo: str, bug: Dict[str, Any]) -> Dict[str, str]:
    fix = bug["fix_commit"]
    base = _git(repo, "rev-parse", f"{fix}^").strip()
    fix_diff = _git(repo, "-c", "core.autocrlf=false", "show", "--no-color", "--format=", fix, "--", *bug["source_paths"])
    test_diff = _git(repo, "-c", "core.autocrlf=false", "show", "--no-color", "--format=", fix, "--", bug["test_path"])
    added = [line[1:] for line in test_diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    if not fix_diff.strip() or not added:
        raise RuntimeError(f"{bug['id']}: fix commit has no source change or no added test lines")
    return {"base": base, "fix_diff": fix_diff, "repro": bug["test_header"] + "\n".join(added).strip("\n") + "\n"}


def _write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def run_variant(bug: Dict[str, Any], variant: Dict[str, Any], workdir: str, inputs: Dict[str, str], out: str) -> Dict[str, Any]:
    run_id = f"ext-{bug['id']}-{variant['id']}"
    issue = bug["issue"]
    repro = inputs["repro"] if "repro" not in variant else open(os.path.join(SPEC_DIR, variant["repro"]), encoding="utf-8").read()
    cmd = [sys.executable, "main.py", "--repo", workdir, "--issue", str(issue["number"]), "--run-id", run_id,
           "--title", issue["title"], "--body", issue["body"],
           "--repro-test", _write(os.path.join(out, "inputs", run_id, "test_reproduce.py"), repro)]
    if variant.get("change") == "commit":
        cmd += ["--base", inputs["base"], "--head", bug["fix_commit"]]
    elif variant.get("patch") == "fix":
        cmd += ["--patch", _write(os.path.join(out, "inputs", run_id, "fix.diff"), inputs["fix_diff"])]
    else:
        cmd += ["--patch", os.path.join(SPEC_DIR, variant["patch"])]

    env = dict(os.environ, CERBERUS_SANDBOX="docker", RUN_ARTIFACTS_DIR=os.path.join(out, "artifacts"))
    start = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=RUN_TIMEOUT_SECONDS)
        output, exit_code = proc.stdout + "\n--- stderr ---\n" + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        output, exit_code = f"timed out after {RUN_TIMEOUT_SECONDS}s", None
    wall = round(time.time() - start, 1)
    _write(os.path.join(out, "logs", run_id + ".log"), output)

    record: Dict[str, Any] = {}
    record_path = os.path.join(out, "artifacts", run_id, "run.json")
    if os.path.exists(record_path):
        with open(record_path, encoding="utf-8") as f:
            record = json.load(f)
    refusal = record.get("refusal") or {}
    red = record.get("red_gate") or {}
    regression = record.get("regression_results") or {}
    actual = [record.get("final_state", "NO_RECORD"), refusal.get("code")]
    return {
        "bug": bug["id"], "variant": variant["id"], "note": variant.get("note", ""),
        "expected": variant["expect"], "actual": actual, "match": actual == list(variant["expect"]),
        "exit_code": exit_code, "wall_sec": wall, "base_commit": inputs["base"], "fix_commit": bug["fix_commit"],
        "sandbox": (record.get("sandbox") or {}).get("isolation"),
        "red_failure_types": sorted(set((red.get("failure_types") or {}).values())),
        "baseline_tests": (record.get("baseline") or {}).get("total"),
        "newly_failing": regression.get("newly_failing") or [],
        "test_files_run_at_base": regression.get("test_files_run_at_base") or [],
        "changed_files": (record.get("blast_radius") or {}).get("changed_files") or [],
        "refusal_detail": " ".join((refusal.get("message") or "").split())[:300],
    }


def environment() -> Dict[str, str]:
    def _out(*args: str) -> str:
        try:
            return subprocess.run(list(args), capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
    return {
        "cerberus_commit": _out("git", "-C", ROOT, "rev-parse", "HEAD"),
        "cerberus_dirty": "yes" if _out("git", "-C", ROOT, "status", "--porcelain", "--", ".") else "no",
        "docker_server": _out("docker", "version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"),
        "sandbox_image": _out("docker", "image", "inspect", "--format", "{{.Id}}", "cerberus-sandbox:py3.11")[:19],
        "host": f"{platform.system()} {platform.release()}, Python {platform.python_version()}",
        "finished_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def render_report(rows: List[Dict[str, Any]], env: Dict[str, str], spec: Dict[str, Any]) -> str:
    matched = sum(r["match"] for r in rows)
    lines = [
        "# Cerberus on external repositories",
        "",
        spec["description"],
        "",
        f"**{matched} of {len(rows)} runs ended in the expected state and refusal code.**",
        "",
        "| Environment | |", "| --- | --- |",
        *[f"| {k} | `{v}` |" for k, v in env.items()],
        "",
        "| Bug | Variant | Expected | Actual | Match | RED failure | Baseline tests | Newly failing | Test files run at base | Wall (s) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        exp = " / ".join(x for x in r["expected"] if x)
        act = " / ".join(x for x in r["actual"] if x)
        lines.append(
            f"| {r['bug']} | {r['variant']} | {exp} | {act} | {'yes' if r['match'] else '**no**'} | "
            f"{', '.join(r['red_failure_types']) or '-'} | {r['baseline_tests'] if r['baseline_tests'] is not None else '-'} | "
            f"{', '.join(r['newly_failing']) or '-'} | {', '.join(r['test_files_run_at_base']) or '-'} | {r['wall_sec']} |"
        )
    lines += ["", "## Bugs", ""]
    for bug in spec["bugs"]:
        repo = spec["repos"][bug["repo"]]
        lines.append(f"- **{bug['id']}**: {repo['url']} ({repo['license']}), fix `{bug['fix_commit']}`, "
                     f"reproduction test from `{bug['test_path']}` in that commit.")
    lines += ["", "## Refusal details", ""]
    for r in rows:
        if r["actual"][0] != "ADMITTED":
            lines.append(f"- **{r['bug']} / {r['variant']}** ({r['note'] or 'no note'}): {r['refusal_detail'] or '-'}")
    lines += [
        "",
        "## Limits",
        "",
        "- Three bugs in three small pure-Python libraries, chosen by the Cerberus author for fast test suites and a",
        "  test added in the fix commit. This shows the gates run on code Cerberus was not written against; it is not a",
        "  sample of real-world bugs and says nothing about repair ability.",
        "- The wrong variants were written by the Cerberus author to exercise specific gates.",
        "- Issue text is paraphrased from the fix commit, not copied from the upstream issue tracker.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", default=[], help="bug ids to run")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--record", action="store_true", help="write evaluation/external/report.md and results.json")
    args = parser.parse_args(argv)

    with open(os.path.join(SPEC_DIR, "cases.json"), encoding="utf-8") as f:
        spec = json.load(f)
    out = os.path.abspath(args.output)
    rows: List[Dict[str, Any]] = []
    for bug in spec["bugs"]:
        if args.only and bug["id"] not in args.only:
            continue
        repo = mirror(os.path.join(out, "mirrors"), bug["repo"], spec["repos"][bug["repo"]]["url"])
        inputs = upstream_inputs(repo, bug)
        workdir = os.path.join(out, "work", bug["id"])
        checkout(repo, workdir, inputs["base"])
        for variant in bug["variants"]:
            row = run_variant(bug, variant, workdir, inputs, out)
            rows.append(row)
            print(json.dumps({k: row[k] for k in ("bug", "variant", "expected", "actual", "match", "wall_sec")}), flush=True)
        if os.path.exists(workdir):
            _remove_tree(workdir)

    report = render_report(rows, environment(), spec)
    _write(os.path.join(out, "report.md"), report)
    _write(os.path.join(out, "results.json"), json.dumps(rows, indent=2) + "\n")
    if args.record:
        if args.only:
            print("--record needs the full set; not recording a partial run.")
            return 2
        _write(os.path.join(SPEC_DIR, "report.md"), report)
        _write(os.path.join(SPEC_DIR, "results.json"), json.dumps(rows, indent=2) + "\n")
    print(f"{sum(r['match'] for r in rows)} of {len(rows)} runs matched; report: {os.path.join(out, 'report.md')}")
    return 0 if all(r["match"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())

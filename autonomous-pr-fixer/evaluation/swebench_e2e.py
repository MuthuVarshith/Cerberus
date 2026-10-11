"""
SWE-bench Verified end-to-end harness for the Cerberus repair agent.

    python evaluation/swebench_e2e.py select                      # write the fixed subset + manifest
    python evaluation/swebench_e2e.py validate [--limit N] [--only ID ...]   # gold patch through the gate (no LLM)
    python evaluation/swebench_e2e.py run --experiment-id ID [--fake gold|give-up] [--only-admitted]
                                          [--limit N] [--retrieval bm25]   # interface.run_repair per instance
    python evaluation/swebench_e2e.py score --experiment-id ID [--check-disk-only]  # official swebench harness

`run` without --fake calls the configured model (LLM_PROVIDER etc.). Scoring "resolved" uses only the
official `swebench.harness.run_evaluation` (SWEBENCH_PYTHON points at an interpreter with swebench).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from evaluation.swebench_tasks import (  # noqa: E402
    EXCLUDED_REPOS, RECIPES, WORK_ROOT, ExtractionError, issue_title, parquet_path, prepare, sha256_file,
)

SPEC_DIR = os.path.join(ROOT, "evaluation", "swebench_e2e")
SUBSET = os.path.join(SPEC_DIR, "subset-v1.jsonl")
MANIFEST = os.path.join(SPEC_DIR, "subset-v1.manifest.json")
VALIDATED = os.path.join(SPEC_DIR, "validated-instances.jsonl")
RESULTS = os.path.join(ROOT, "results", "swebench")
SEED = "cerberus-swebench-v1"
SUBSET_SIZE = 25
#: Minimum free disk (GB) before the official harness may pull images; per-image sizes are in the docs.
MIN_FREE_GB = 120


def load_instances() -> Dict[str, Dict[str, Any]]:
    import pandas as pd
    df = pd.read_parquet(parquet_path())
    return {r["instance_id"]: r for r in df.to_dict("records")}


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def write_jsonl(path: str, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in rows)


# ------------------------------------------------------------------ select

def cmd_select(args: argparse.Namespace) -> int:
    insts = load_instances()
    eligible, skipped = [], []
    for iid, inst in insts.items():
        recipe = RECIPES.get(inst["repo"])
        if recipe is None or inst["version"] not in recipe["versions"]:
            continue
        try:
            prep = prepare(inst)  # extraction is checked statically here; nothing runs
            eligible.append(iid)
        except (ExtractionError, RuntimeError) as exc:
            skipped.append({"instance_id": iid, "reason": str(exc)[:300]})
        finally:
            from harness.docker_sandbox import _remove_tree
            d = os.path.join(WORK_ROOT, "repos", iid)
            if os.path.exists(d):
                _remove_tree(d)
    order = sorted(eligible, key=lambda i: hashlib.sha256(f"{SEED}:{i}".encode()).hexdigest())
    chosen = order[:SUBSET_SIZE]
    rows = [{"instance_id": i, "repo": insts[i]["repo"], "version": insts[i]["version"]} for i in chosen]
    write_jsonl(SUBSET, rows)
    manifest = {
        "dataset": "SWE-bench Verified (princeton-nlp/SWE-bench_Verified), test split, 500 rows",
        "parquet_sha256": sha256_file(parquet_path()),
        "subset_file": os.path.basename(SUBSET), "subset_sha256": sha256_file(SUBSET),
        "selection_rule": ("instances whose (repo, version) has a Python-3.11 recipe in evaluation/swebench_tasks.py "
                           "RECIPES and whose FAIL_TO_PASS tests extract into a standalone module; ordered by "
                           f"sha256('{SEED}:' + instance_id); first {SUBSET_SIZE}"),
        "seed": SEED, "eligible": len(eligible), "extraction_skipped": skipped,
        "excluded_repos": EXCLUDED_REPOS,
    }
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2)
    print(f"{len(eligible)} eligible, {len(skipped)} skipped by extraction; wrote {len(rows)} to {SUBSET}")
    return 0


def subset_ids(args: argparse.Namespace) -> List[str]:
    src = getattr(args, "instances", None) or SUBSET
    rows = read_jsonl(src)
    if getattr(args, "only_admitted", False):
        rows = [r for r in rows if r.get("outcome") == "ADMITTED"]
    ids = [r["instance_id"] for r in rows]
    if args.only:
        ids = [i for i in ids if i in set(args.only)]
    return ids[: args.limit] if args.limit else ids


# ---------------------------------------------------------------- validate

def cmd_validate(args: argparse.Namespace) -> int:
    import main
    from agents.patch_sources import DiffPatchSource
    insts = load_instances()
    done = {r["instance_id"]: r for r in read_jsonl(VALIDATED)} if os.path.exists(VALIDATED) else {}
    for iid in subset_ids(args):
        inst = insts[iid]
        row: Dict[str, Any] = {"instance_id": iid, "repo": inst["repo"], "version": inst["version"]}
        start = time.time()
        try:
            prep = prepare(inst)
        except (ExtractionError, RuntimeError) as exc:
            row.update(outcome="SKIPPED", code="EXTRACTION", detail=str(exc)[:300])
        else:
            art = os.path.join(WORK_ROOT, "validate-runs")
            os.environ["RUN_ARTIFACTS_DIR"] = art
            run_id = f"val-{iid}"
            try:
                main.run_pipeline(prep.repo_dir, 1, issue_title(inst), inst["problem_statement"],
                                  repro_test_code=prep.reproduction_test, run_id=run_id, base_ref=prep.head,
                                  patch_source=DiffPatchSource([inst["patch"]], name="swebench gold patch"))
            except Exception as exc:  # recorded in run.json as ERROR
                row["exception"] = f"{type(exc).__name__}: {exc}"[:300]
            rec_path = os.path.join(art, run_id, "run.json")
            rec = json.load(open(rec_path, encoding="utf-8")) if os.path.exists(rec_path) else {}
            ref = rec.get("refusal") or {}
            row.update(outcome=rec.get("final_state", "NO_RECORD"), code=ref.get("code"), stage=ref.get("stage"),
                       detail=" ".join((ref.get("message") or "").split())[:300],
                       baseline_tests=(rec.get("baseline") or {}).get("total"), run_json=rec_path)
        row["wall_sec"] = round(time.time() - start, 1)
        row["validated_utc"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        done[iid] = row
        write_jsonl(VALIDATED, list(done.values()))
        print(json.dumps({k: row.get(k) for k in ("instance_id", "outcome", "code", "stage", "wall_sec")}), flush=True)
    return 0


# --------------------------------------------------------------------- run

def gold_model(diff: str):
    """Scripted fake model (no LLM): apply the gold patch and submit it."""
    from repair.llm.fake import FakeChatModel, call, respond
    return FakeChatModel([respond(call("apply_patch", diff=diff)),
                          respond(call("submit_patch", reasoning="dry run: SWE-bench gold patch"))], model="fake-gold")


def give_up_model():
    from repair.llm.fake import FakeChatModel, respond
    return FakeChatModel([respond(text="I give up.")] * 5, model="fake-give-up")


def cmd_run(args: argparse.Namespace) -> int:
    from repair.config import RepairConfig
    from repair.interface import RepairTask, run_repair
    insts = load_instances()
    out = os.path.join(RESULTS, args.experiment_id)
    if os.path.exists(out):
        print(f"{out} exists; experiments are never overwritten.")
        return 2
    os.makedirs(out)
    config = RepairConfig.from_env(retrieval=None if args.retrieval in (None, "none") else args.retrieval)
    ids = subset_ids(args)
    with open(os.path.join(out, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"experiment_id": args.experiment_id, "fake": args.fake, "instances": ids,
                   "config": config.model_dump(), "parquet_sha256": sha256_file(parquet_path()),
                   "started_utc": _dt.datetime.now(_dt.timezone.utc).isoformat()}, f, indent=2)
    preds, rows = [], []
    for iid in ids:
        inst = insts[iid]
        prep = prepare(inst)
        task = RepairTask(repo_dir=prep.repo_dir, issue_id=iid, title=issue_title(inst),
                          body=inst["problem_statement"], reproduction_test=prep.reproduction_test, base_ref=prep.head)
        opts: Dict[str, Any] = {"run_id": f"{args.experiment_id}-{iid}", "out_root": os.path.join(out, "runs"), "printer": print}
        if args.fake == "gold":
            opts["model"] = gold_model(inst["patch"])
        elif args.fake == "give-up":
            opts["model"] = give_up_model()
        res = run_repair(task, config, **opts)
        model_name = f"cerberus-{args.fake or config.model}"
        preds.append({"instance_id": iid, "model_name_or_path": model_name, "model_patch": res.patch})
        rows.append({"instance_id": iid, "admitted": res.admitted, "final_status": res.final_status,
                     "failure_code": res.failure_code, "attempts": res.attempts, "total_tokens": res.total_tokens,
                     "estimated_cost": res.estimated_cost, "duration_seconds": res.duration_seconds,
                     "patch_files": res.changed_files, "ground_truth_files": prep.ground_truth_files,
                     "resolved": None, "false_admission": None})
        write_jsonl(os.path.join(out, "predictions.jsonl"), preds)
        write_jsonl(os.path.join(out, "results.jsonl"), rows)
        print(json.dumps({k: rows[-1][k] for k in ("instance_id", "admitted", "final_status", "total_tokens")}), flush=True)
    return 0


# ------------------------------------------------------------------- score

def free_gb(path: str = ROOT) -> float:
    return round(shutil.disk_usage(path).free / 1e9, 1)


def cmd_score(args: argparse.Namespace) -> int:
    out = os.path.join(RESULTS, args.experiment_id)
    rows = read_jsonl(os.path.join(out, "results.jsonl"))
    free = free_gb()
    print(f"free disk: {free} GB (minimum {MIN_FREE_GB} GB before pulling SWE-bench images)")
    if args.check_disk_only:
        return 0
    if free < MIN_FREE_GB:
        print("Not enough disk space; not pulling images.")
        return 3
    insts = load_instances()
    ds_path = os.path.join(out, "swebench-instances.json")
    with open(ds_path, "w", encoding="utf-8") as f:
        json.dump([{k: (v if not hasattr(v, "item") else v.item()) for k, v in insts[r["instance_id"]].items()}
                   for r in rows], f, default=str)
    py = os.environ.get("SWEBENCH_PYTHON") or os.path.join(ROOT, "artifacts", "swebench-venv", "Scripts", "python.exe")
    run_id = f"cerberus-{args.experiment_id}"
    cmd = [py, "-m", "swebench.harness.run_evaluation", "--dataset_name", ds_path, "--split", "test",
           "--predictions_path", os.path.join(out, "predictions.jsonl"), "--max_workers", str(args.workers),
           "--run_id", run_id, "--timeout", "1800",
           "--instance_ids", *[r["instance_id"] for r in rows]]
    print(" ".join(cmd))
    proc = subprocess.run(cmd, cwd=out)
    model = read_jsonl(os.path.join(out, "predictions.jsonl"))[0]["model_name_or_path"]
    report_path = os.path.join(out, f"{model.replace('/', '__')}.{run_id}.json")
    if not os.path.exists(report_path):
        print(f"official report not found ({report_path}); exit {proc.returncode}")
        return 1
    report = json.load(open(report_path, encoding="utf-8"))
    resolved = set(report.get("resolved_ids", []))
    evaluated = set(report.get("completed_ids", [])) | set(report.get("empty_patch_ids", []))
    for r in rows:
        if r["instance_id"] in evaluated:
            r["resolved"] = r["instance_id"] in resolved
            r["false_admission"] = bool(r["admitted"] and not r["resolved"])
    write_jsonl(os.path.join(out, "results.jsonl"), rows)
    scored = [r for r in rows if r["resolved"] is not None]
    summary = {"instances": len(rows), "scored": len(scored), "resolved": sum(r["resolved"] for r in scored),
               "admitted": sum(r["admitted"] for r in rows),
               "false_admissions": sum(r["false_admission"] for r in scored),
               "total_tokens": sum(r["total_tokens"] for r in rows),
               "cost": sum(r["estimated_cost"] or 0 for r in rows), "official_report": report_path}
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select"); s.set_defaults(func=cmd_select)
    for name, fn in (("validate", cmd_validate), ("run", cmd_run)):
        sp = sub.add_parser(name)
        sp.add_argument("--instances", help="JSONL of instance ids (default: subset-v1.jsonl)")
        sp.add_argument("--only", nargs="*")
        sp.add_argument("--limit", type=int)
        sp.set_defaults(func=fn)
    r = sub.choices["run"]
    r.add_argument("--experiment-id", required=True)
    r.add_argument("--only-admitted", action="store_true", help="with --instances validated-instances.jsonl")
    r.add_argument("--fake", choices=["gold", "give-up"], help="scripted fake model; no LLM calls")
    r.add_argument("--retrieval", choices=["none", "bm25", "hybrid-bge-small", "hybrid-jina-code"])
    sc = sub.add_parser("score")
    sc.add_argument("--experiment-id", required=True)
    sc.add_argument("--workers", type=int, default=1)
    sc.add_argument("--check-disk-only", action="store_true")
    sc.set_defaults(func=cmd_score)
    args = p.parse_args(argv)
    import tempfile
    tmp = os.path.join(ROOT, "artifacts", "tmp")
    os.makedirs(tmp, exist_ok=True)
    tempfile.tempdir = tmp
    # Per-command sandbox timeout for the gate's own git commands (default 60 s). On large repos and a slow
    # CPU, `git diff` against the base exceeded it and the gate saw an empty diff (EMPTY_PATCH).
    os.environ.setdefault("SANDBOX_TIMEOUT_SECONDS", "600")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

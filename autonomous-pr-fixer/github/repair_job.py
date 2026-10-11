"""
Subprocess entry point for one issue-triggered repair: a single call to the frozen
`repair.interface.run_repair`, with the RepairResult written as JSON.

    python -m github.repair_job --spec SPEC.json --result RESULT.json

The spec file carries the issue text, so issue content never reaches argv. No GitHub
credential is given to this process; it needs only the model settings (server env/.env).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_dotenv() -> None:
    path = os.path.join(ROOT, ".env")
    if os.path.isfile(path):
        try:
            from dotenv import load_dotenv
        except ImportError:
            return
        load_dotenv(path, override=False)


def _write(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="github.repair_job")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)
    _load_dotenv()
    if spec.get("tmp_dir"):
        os.makedirs(spec["tmp_dir"], exist_ok=True)
        tempfile.tempdir = spec["tmp_dir"]

    from github.pr_publisher import redact_secrets
    from repair.interface import RepairConfig, RepairTask, run_repair

    failure = {"run_id": spec["run_id"], "admitted": False, "repository": spec["repo_dir"],
               "issue_id": f"#{spec['issue_number']}"}
    try:
        with open(spec["repro_test_path"], encoding="utf-8") as f:
            repro = f.read()
        task = RepairTask(repo_dir=spec["repo_dir"], issue_id=f"#{spec['issue_number']}", title=spec["issue_title"],
                          body=spec["issue_body"] or spec["issue_title"], reproduction_test=repro,
                          base_ref=spec["base_sha"], issue_number=int(spec["issue_number"]))
        config = RepairConfig.from_env()
        retrieval = (spec.get("retrieval") or "none").lower()
        config = config.model_copy(update={"retrieval": None if retrieval == "none" else retrieval})
        result = run_repair(task, config, run_id=spec["run_id"], out_root=spec["out_root"])
    except Exception as exc:
        status = "LLM_FAILURE" if type(exc).__name__ == "LLMError" else "ERROR"
        _write(args.result, {**failure, "final_status": status,
                             "message": redact_secrets(f"{type(exc).__name__}: {exc}")[:1000]})
        return 1
    _write(args.result, result.model_dump())
    return 0 if result.admitted else 1


if __name__ == "__main__":
    sys.exit(main())

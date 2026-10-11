"""
SWE-bench Verified instance -> Cerberus repair task.

Per instance:
  * work repo: clone of the local bare mirror at base_commit, plus ONE commit on top holding the
    instance's test_patch (tests exist, source bug remains) and a generated .cerberus.yml;
  * reproduction test: the FAIL_TO_PASS tests extracted with `ast` from the patched test files
    (whole module minus unselected tests, non-root conftest.py files inlined, `__file__` rewritten
    to the original path, relative imports made absolute); parametrized ids keep the base function;
  * ground truth files: non-test files changed by the gold patch.
Extraction failures raise ExtractionError with a reason; callers record and skip the instance.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_DATA_DIR = r"C:\Users\thumm\Documents\antigravity\Cerberus\autonomous-pr-fixer\artifacts\swebench"
WORK_ROOT = os.path.join(ROOT, "artifacts", "swebench-e2e")


def data_dir() -> str:
    return os.environ.get("CERBERUS_SWEBENCH_DIR") or DEFAULT_DATA_DIR


def parquet_path() -> str:
    return os.path.join(data_dir(), "verified.parquet")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# Per-repo environment recipes for the cerberus-sandbox:py3.11 image (no compiler, pip only).
# `versions`: SWE-bench `version` values believed to run on Python 3.11; others are excluded.
RECIPES: Dict[str, Dict[str, Any]] = {
    "psf/requests": {"versions": {"2.26", "2.27"}, "src": ["requests/*"],
                     "setup": ["python -m pip install -e . pytest==7.4.4 pytest-httpbin==2.0.0 pytest-mock trustme"]},
    "pallets/flask": {"versions": {"2.3"}, "src": ["src/flask/*"],
                      "setup": ["python -m pip install -e . pytest==7.4.4 Werkzeug==2.3.7"]},
    "pytest-dev/pytest": {"versions": {"7.2", "6.3", "6.2"}, "src": ["src/_pytest/*", "src/pytest/*"],
                          "setup": ["python -m pip install -e . hypothesis xmlschema attrs==23.1.0"]},
    "sphinx-doc/sphinx": {"versions": {"5.0", "5.1", "5.2", "7.1", "7.2"}, "src": ["sphinx/*"],
                          "setup": ["python -m pip install -e .[test] 'Jinja2<3.2' 'alabaster<0.7.14' 'sphinxcontrib-applehelp<1.0.5' "
                                    "'sphinxcontrib-devhelp<1.0.6' 'sphinxcontrib-htmlhelp<2.0.5' 'sphinxcontrib-qthelp<1.0.7' "
                                    "'sphinxcontrib-serializinghtml<1.1.10'"]},
    "pydata/xarray": {"versions": {"2022.03", "2022.06", "2022.09"}, "src": ["xarray/*"],
                      "setup": ["python -m pip install numpy==1.23.5 pandas==1.5.3 pytest==7.4.4 -e ."]},
    "pylint-dev/pylint": {"versions": {"2.15", "3.0"}, "src": ["pylint/*"],
                          "setup": ["python -m pip install -e . pytest==7.4.4 pytest-timeout"]},
}
EXCLUDED_REPOS = {
    "django/django": "tests run with tests/runtests.py, not pytest; the gate needs pytest JUnit output",
    "sympy/sympy": "uses its own bin/test runner conventions and very large test modules; deferred",
    "matplotlib/matplotlib": "needs a compiler and pinned freetype for image tests; sandbox image has no compiler",
    "scikit-learn/scikit-learn": "C/Cython extensions need a compiler (not in the sandbox image)",
    "astropy/astropy": "C extensions need a compiler (not in the sandbox image)",
    "mwaskom/seaborn": "pinned SWE-bench env (pandas 1.3.5) has no Python 3.11 wheels",
}
DOC_PATHS = ["doc/*", "docs/*", "changelog/*", "CHANGES*", "CHANGELOG*", "AUTHORS*"]


class ExtractionError(ValueError):
    pass


def _git(cwd: Optional[str], *args: str, input_text: Optional[str] = None) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", input=input_text)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {res.stderr.strip()[:500]}")
    return res.stdout


def diff_files(diff: str) -> List[str]:
    return sorted(set(re.findall(r"^diff --git a/(\S+) b/", diff, re.M)))


def is_test_file(path: str) -> bool:
    parts = path.split("/")
    return any(p in ("tests", "test", "testing") for p in parts[:-1]) or parts[-1].startswith("test_") \
        or parts[-1] == "conftest.py"


def ground_truth_files(inst: Dict[str, Any]) -> List[str]:
    return [p for p in diff_files(inst["patch"]) if not is_test_file(p)]


def test_ids(inst: Dict[str, Any], key: str) -> List[str]:
    v = inst[key]
    return json.loads(v) if isinstance(v, str) else list(v)


# ------------------------------------------------------------- reproduction test

def _split_id(tid: str) -> Tuple[str, List[str]]:
    path, _, rest = tid.partition("::")
    parts = [re.sub(r"\[.*$", "", p) for p in rest.split("::")] if rest else []
    return path, [p for p in parts if p]


def _selection(f2p: List[str]) -> Dict[str, Dict[str, set]]:
    """file -> {top-level name -> set of method names (empty set = whole function)}."""
    sel: Dict[str, Dict[str, set]] = {}
    for tid in f2p:
        path, parts = _split_id(tid)
        if not path.endswith(".py") or not parts:
            raise ExtractionError(f"unsupported test id {tid!r}")
        if len(parts) > 2:
            raise ExtractionError(f"nested test classes are not supported: {tid!r}")
        names = sel.setdefault(path, {})
        if len(parts) == 1:
            names.setdefault(parts[0], set())
        else:
            names.setdefault(parts[0], set()).add(parts[1])
    return sel


def _is_test_def(node: ast.AST) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name.startswith("test")
    return isinstance(node, ast.ClassDef) and node.name.startswith("Test")


def _segment(lines: List[str], node: ast.AST) -> str:
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return "".join(lines[start - 1:node.end_lineno])


def _absolute_imports(text: str, rel_path: str) -> str:
    pkg = rel_path.split("/")[:-1]

    def repl(m: "re.Match[str]") -> str:
        dots, mod = len(m.group(2)), m.group(3)
        base = pkg[: len(pkg) - (dots - 1)]
        target = ".".join(base + ([mod] if mod else []))
        return f"{m.group(1)}from {target} import"
    return re.sub(r"^(\s*)from\s+(\.+)([\w.]*)\s+import", repl, text, flags=re.M)


def _filtered_module(source: str, rel_path: str, keep: Optional[Dict[str, set]], file_var: str) -> Tuple[str, set]:
    """Module source minus unselected tests; returns (text, top-level names defined)."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    out: List[str] = []
    names: set = set()
    found: set = set()
    for node in tree.body:
        if keep is not None and _is_test_def(node):
            if node.name not in keep:
                continue
            found.add(node.name)
            if isinstance(node, ast.ClassDef) and keep[node.name]:
                methods = keep[node.name]
                body_lines = lines[:]
                drop = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name.startswith("test") and n.name not in methods]
                missing = methods - {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                if missing:
                    raise ExtractionError(f"{rel_path}: {node.name} has no method(s) {sorted(missing)} (inherited?)")
                for n in drop:
                    s = min([n.lineno] + [d.lineno for d in n.decorator_list])
                    for i in range(s - 1, n.end_lineno):
                        body_lines[i] = ""
                text = _segment(body_lines, node)
                out.append(text if text.endswith("\n") else text + "\n")
                names.add(node.name)
                continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        seg = _segment(lines, node)
        out.append(seg if seg.endswith("\n") else seg + "\n")
    if keep is not None and set(keep) - found:
        raise ExtractionError(f"{rel_path}: test(s) not found at module level: {sorted(set(keep) - found)}")
    text = "".join(out)
    text = re.sub(r"\b__file__\b", file_var, text)
    return _absolute_imports(text, rel_path), names


def build_reproduction_test(repo_dir: str, inst: Dict[str, Any]) -> str:
    """Standalone FAIL_TO_PASS test module from the work repo (test_patch already committed)."""
    sel = _selection(test_ids(inst, "FAIL_TO_PASS"))
    header = ["# Generated by Cerberus from SWE-bench FAIL_TO_PASS tests of " + inst["instance_id"],
              "import os as _cerberus_os", "import sys as _cerberus_sys",
              "_CERBERUS_ROOT = _cerberus_os.path.dirname(_cerberus_os.path.dirname(_cerberus_os.path.abspath(__file__)))"]
    body: List[str] = []
    seen_names: Dict[str, str] = {}
    conftests_done: set = set()
    plugins: List[str] = []
    for n, (rel, keep) in enumerate(sorted(sel.items())):
        full = os.path.join(repo_dir, rel)
        if not os.path.isfile(full):
            raise ExtractionError(f"test file {rel} does not exist after test_patch")
        # emulate pytest's rootdir-less 'prepend' import mode: first dir up without __init__.py
        d = os.path.dirname(rel)
        while d and os.path.isfile(os.path.join(repo_dir, d, "__init__.py")):
            d = os.path.dirname(d)
        if d:
            header.append(f"_cerberus_sys.path.insert(0, _cerberus_os.path.join(_CERBERUS_ROOT, {d!r}))")
        # non-root conftest.py files, outermost first
        chain, d = [], os.path.dirname(rel)
        while d:
            c = f"{d}/conftest.py"
            if os.path.isfile(os.path.join(repo_dir, c)):
                chain.append(c)
            d = os.path.dirname(d)
        sources = [(c, None) for c in reversed(chain) if c not in conftests_done] + [(rel, keep)]
        for i, (path, k) in enumerate(sources):
            var = f"__cerberus_file_{n}_{i}__"
            header.append(f"{var} = _cerberus_os.path.join(_CERBERUS_ROOT, {path!r})")
            with open(os.path.join(repo_dir, path), encoding="utf-8") as f:
                src = f.read()
            try:
                text, names = _filtered_module(src, path, k, var)
            except SyntaxError as exc:
                raise ExtractionError(f"{path} does not parse: {exc}") from None
            for name in names:
                if name in seen_names and seen_names[name] != path and k is not None and not path.endswith("conftest.py"):
                    if seen_names[name].endswith("conftest.py"):
                        continue  # a test module overriding a conftest fixture is normal pytest behaviour
                    raise ExtractionError(f"name {name!r} defined in both {seen_names[name]} and {path}")
                seen_names[name] = path
            m = re.search(r"^pytest_plugins\s*=\s*(.+)$", text, re.M)
            if m:
                plugins.append(m.group(1).strip())
                text = text.replace(m.group(0), "", 1)
            if path.endswith("conftest.py"):
                conftests_done.add(path)
                text = re.sub(r"^collect_ignore\w*\s*=.*$", "", text, flags=re.M)
            body.append(f"\n# ---- from {path}\n{text}")
    plugin_line = []
    if plugins:
        items: List[str] = []
        for p in plugins:
            try:
                v = ast.literal_eval(p)
            except (ValueError, SyntaxError):
                raise ExtractionError(f"pytest_plugins is not a literal: {p}") from None
            items += [v] if isinstance(v, str) else list(v)
        plugin_line = [f"pytest_plugins = {sorted(set(items))!r}"]
    code = "\n".join(header + plugin_line) + "\n" + "".join(body)
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise ExtractionError(f"generated module does not parse: {exc}") from None
    if "from __future__" in code:
        # __future__ imports must come first: hoist them.
        futures = sorted(set(re.findall(r"^from __future__ import .+$", code, re.M)))
        code = "\n".join(futures) + "\n" + re.sub(r"^from __future__ import .+$", "", code, flags=re.M)
    return code


# ----------------------------------------------------------------- work repo

def cerberus_yml(inst: Dict[str, Any]) -> str:
    recipe = RECIPES[inst["repo"]]
    files = sorted({_split_id(t)[0] for t in test_ids(inst, "FAIL_TO_PASS") + test_ids(inst, "PASS_TO_PASS")}
                   | {p for p in diff_files(inst["test_patch"]) if p.endswith(".py") and "conftest" not in p})
    files = [f for f in files if f.endswith(".py")]
    lines = ["version: 1", "setup:"] + [f"  - {json.dumps(c)}" for c in recipe["setup"]] + [
        "test:", f"  command: {json.dumps('python -m pytest -q -p no:randomly ' + ' '.join(files))}",
        "scope:", "  allowed_paths:"] + [f"    - {json.dumps(p)}" for p in recipe["src"] + DOC_PATHS] + [
        "  max_files: 10", "  max_lines: 600", "  allow_test_modifications: false",
        "budgets:", "  patch_attempts: 1", "  red_runs: 2", "  green_runs: 2", "  command_timeout_seconds: 2400", ""]
    return "\n".join(lines)


def prepare_work_repo(inst: Dict[str, Any], dest: str) -> str:
    """Clone mirror at base_commit, commit test_patch + .cerberus.yml. Returns the new HEAD sha."""
    from harness.docker_sandbox import _remove_tree
    mirror = os.path.join(data_dir(), "mirrors", inst["repo"].replace("/", "__") + ".git")
    if os.path.exists(dest):
        _remove_tree(dest)
    _git(None, "clone", "--quiet", "--no-checkout", "-c", "core.autocrlf=false", mirror, dest)
    _git(dest, "checkout", "--quiet", "--detach", inst["base_commit"])
    _git(dest, "apply", "--whitespace=nowarn", "-", input_text=inst["test_patch"])
    with open(os.path.join(dest, ".cerberus.yml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(cerberus_yml(inst))
    _git(dest, "add", "-A")
    _git(dest, "-c", "user.name=Cerberus", "-c", "user.email=cerberus@localhost", "commit", "-q", "-m",
         f"SWE-bench {inst['instance_id']}: test_patch + .cerberus.yml")
    return _git(dest, "rev-parse", "HEAD").strip()


@dataclass
class PreparedInstance:
    instance_id: str
    repo_dir: str
    head: str
    reproduction_test: str
    ground_truth_files: List[str] = field(default_factory=list)


def prepare(inst: Dict[str, Any], work_root: str = WORK_ROOT) -> PreparedInstance:
    if inst["repo"] not in RECIPES:
        raise ExtractionError(f"repo excluded: {EXCLUDED_REPOS.get(inst['repo'], 'no recipe')}")
    dest = os.path.join(work_root, "repos", inst["instance_id"])
    head = prepare_work_repo(inst, dest)
    return PreparedInstance(inst["instance_id"], dest, head, build_reproduction_test(dest, inst), ground_truth_files(inst))


def issue_title(inst: Dict[str, Any]) -> str:
    first = next((l.strip() for l in inst["problem_statement"].splitlines() if l.strip()), inst["instance_id"])
    return first[:200]

"""
Reproduction agent: the RED gate and the GREEN check.

RED contract — a reproduction test is accepted only when all of these hold:
  1. It is valid Python.
  2. It imports code from the repository and references a symbol that the issue
     names and the repository defines (or, when the issue names no repository
     symbol, it calls something imported from the repository).
  3. Run under pytest with a JUnit report, it has at least one test, no setup,
     teardown or collection errors, and at least one test *fails*.
  4. Every failure is for an accepted reason: an assertion (including
     pytest.fail / DID NOT RAISE), an exception type named in the issue, or an
     exception raised from inside repository code. Import and syntax errors are
     never accepted.
  5. Repeated runs (default 3) produce the same failing tests with the same
     exception types.
Anything that cannot be validated is refused, with a RefusalCode.

GREEN contract — the unmodified reproduction test runs with a JUnit report, has
no failures, errors or skips, and every test that failed at RED now passes.

Harness files live under `.cerberus/`, which is excluded from git in the
workspace, so the reproduction test never appears in a diff or a commit.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from harness.docker_sandbox import HARNESS_DIR, Sandbox
from harness.junit import ERROR, FAILED, PASSED, JUnitReportError, TestReport, read_junit_report
from harness.pipeline_state import RefusalCode

REPRO_TEST_PATH = f"{HARNESS_DIR}/test_reproduce.py"

#: Failures of these types mean the test itself is broken, never the bug.
FORBIDDEN_FAILURE_TYPES = frozenset({"ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError", "TabError"})
#: Failures a test author raises deliberately to express an expectation.
ASSERTION_FAILURE_TYPES = frozenset({"AssertionError", "Failed"})

_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_ERROR_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception)\b")
_SKIP_DIRS = {".git", ".cerberus", "__pycache__", "node_modules", "venv", ".venv", "env", "build", "dist", ".tox"}
MAX_SYMBOL_SCAN_FILES = 5000


@dataclass
class ReproductionResult:
    reproduced: bool
    test_code: str
    error_message: str
    returncode: int
    raw_output: str
    refusal_code: Optional[str] = None
    failing_test_ids: List[str] = field(default_factory=list)
    failure_types: Dict[str, str] = field(default_factory=dict)
    runs: int = 0
    test_sha256: str = ""


@dataclass
class GreenResult:
    passed: bool
    message: str
    raw_output: str
    refusal_code: Optional[str] = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _module_candidates(workspace_dir: str, dotted: str) -> List[str]:
    rel = dotted.replace(".", os.sep)
    out = []
    for base in ("", "src"):
        root = os.path.join(workspace_dir, base) if base else workspace_dir
        out.append(os.path.join(root, rel + ".py"))
        out.append(os.path.join(root, rel, "__init__.py"))
    return out


def _local_module_file(workspace_dir: str, dotted: str) -> Optional[str]:
    """Path of a repository module, or None when the module is not part of the repo."""
    for candidate in _module_candidates(workspace_dir, dotted):
        if os.path.isfile(candidate):
            return candidate
    first = dotted.split(".")[0]
    for base in ("", "src"):
        root = os.path.join(workspace_dir, base) if base else workspace_dir
        if os.path.isdir(os.path.join(root, first)) and not first.startswith("."):
            return os.path.join(root, first)
    return None


def _defined_symbols(path: str) -> Set[str]:
    """Top-level functions/classes and class methods defined in a Python file."""
    if not path.endswith(".py") or not os.path.isfile(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except (SyntaxError, ValueError):
        return set()
    names: Set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.add(item.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def code_identifiers(issue_text: str) -> Set[str]:
    """Identifiers the issue writes as code, not prose.

    Backticked spans, names followed by '(', snake_case, camelCase and dotted
    names. Plain English words are ignored so that "value" in prose does not
    collide with a function called `value`.
    """
    found: Set[str] = set()
    for span in re.findall(r"`([^`]+)`", issue_text):
        found.update(_IDENTIFIER_RE.findall(span))
    found.update(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", issue_text))
    for token in _IDENTIFIER_RE.findall(issue_text):
        if "_" in token.strip("_") or re.search(r"[a-z][A-Z]", token):
            found.add(token)
    for dotted in re.findall(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b", issue_text):
        found.update(part for part in dotted.split(".") if part not in ("py",))
    return {f for f in found if len(f) >= 3}


def repository_symbols(workspace_dir: str) -> Set[str]:
    """Module names and top-level/method names defined anywhere in the repository's Python files."""
    names: Set[str] = set()
    scanned = 0
    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            scanned += 1
            if scanned > MAX_SYMBOL_SCAN_FILES:
                return names
            names.add(fn[:-3])
            names.update(_defined_symbols(os.path.join(root, fn)))
    return names


def check_test_relevance(
    test_code: str,
    workspace_dir: str,
    issue_text: str,
    repo_symbols: Optional[Set[str]] = None,
) -> Optional[str]:
    """Return None when the test exercises repository code the issue is about, else a reason."""
    tree = ast.parse(test_code)
    local_modules: Set[str] = set()
    bound_from_local: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _local_module_file(workspace_dir, alias.name):
                    local_modules.add(alias.name)
                    bound_from_local.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _local_module_file(workspace_dir, node.module):
                local_modules.add(node.module)
                for alias in node.names:
                    bound_from_local.add(alias.asname or alias.name)

    if not local_modules:
        return "the test does not import any module from the repository"

    referenced: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.alias):
            referenced.update(node.name.split("."))
            if node.asname:
                referenced.add(node.asname)
    for module in local_modules:
        referenced.update(module.split("."))

    symbols = repo_symbols if repo_symbols is not None else repository_symbols(workspace_dir)
    issue_repo_symbols = code_identifiers(issue_text) & symbols
    if issue_repo_symbols:
        if referenced & issue_repo_symbols:
            return None
        return (
            "the test does not reference any repository symbol named in the issue "
            f"({', '.join(sorted(issue_repo_symbols))})"
        )

    calls_local = any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in bound_from_local)
            or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in bound_from_local
            )
        )
        for node in ast.walk(tree)
    )
    if calls_local:
        return None
    return "the test imports repository code but never calls it"


def _location_kind(location: Optional[str], workspace_dir: str) -> str:
    """Classify an innermost traceback frame path as 'test', 'repo', 'external' or 'unknown'."""
    if not location:
        return "unknown"
    norm = location.replace("\\", "/")
    if f"{HARNESS_DIR}/" in norm:
        return "test"
    if "site-packages/" in norm or "dist-packages/" in norm:
        return "external"
    if norm.startswith("/workspace/"):
        rel = norm[len("/workspace/"):]
    elif os.path.isabs(location):
        rel = os.path.relpath(location, workspace_dir)
        if rel.startswith(".."):
            return "external"
    else:
        rel = norm
    return "repo" if os.path.isfile(os.path.join(workspace_dir, rel)) else "external"


def issue_error_names(issue_text: str, extra: Optional[Iterable[str]] = None) -> Set[str]:
    names = set(_ERROR_NAME_RE.findall(issue_text))
    names.update(extra or [])
    return names


class ReproductionAgent:
    """Writes and validates the reproduction test (RED) and re-checks it after a patch (GREEN)."""

    def __init__(
        self,
        sandbox: Sandbox,
        error_signatures: Optional[Iterable[str]] = None,
        runs: int = 3,
        timeout: int = 120,
    ):
        self.sandbox = sandbox
        self.error_signatures = list(error_signatures or [])
        self.runs = max(1, runs)
        self.timeout = timeout
        self._repo_symbols: Optional[Set[str]] = None

    def _pytest_command(self, report_name: str) -> str:
        return (
            f"{self.sandbox.python_cmd} -m pytest {REPRO_TEST_PATH} -p no:cacheprovider -q "
            f"--junitxml={HARNESS_DIR}/{report_name}"
        )

    def _run_once(self, report_name: str) -> Tuple[Optional[TestReport], str, int, Optional[str]]:
        report_path = os.path.join(self.sandbox.workspace_dir, HARNESS_DIR, report_name)
        if os.path.exists(report_path):
            os.remove(report_path)
        res = self.sandbox.exec(self._pytest_command(report_name), timeout=self.timeout)
        if res.timed_out:
            return None, res.output, res.exit_code, f"reproduction test timed out after {self.timeout}s"
        try:
            return read_junit_report(report_path), res.output, res.exit_code, None
        except JUnitReportError as exc:
            return None, res.output, res.exit_code, str(exc)

    def _refuse(self, code: RefusalCode, message: str, test_code: str, output: str = "", returncode: int = -1, runs: int = 0) -> ReproductionResult:
        return ReproductionResult(
            reproduced=False,
            test_code=test_code,
            error_message=message,
            returncode=returncode,
            raw_output=output,
            refusal_code=code.value,
            runs=runs,
        )

    def _classify(self, report: TestReport, issue_text: str) -> Tuple[Optional[RefusalCode], str]:
        if report.total == 0:
            return RefusalCode.RED_INVALID_TEST, "no tests were collected from the reproduction test"
        errors = [c for c in report.cases if c.outcome == ERROR]
        if errors:
            first = errors[0]
            detail = first.exception_type or first.message[:200]
            return RefusalCode.RED_INVALID_TEST, (
                f"the reproduction test errored outside its test body ({first.test_id}: {detail}); "
                "setup, fixture and collection errors are not reproductions"
            )
        failures = [c for c in report.cases if c.outcome == FAILED]
        if not failures:
            return RefusalCode.RED_NOT_FAILING, "the reproduction test passed on the unpatched code, so it does not reproduce the bug"
        named = issue_error_names(issue_text, self.error_signatures)
        for case in failures:
            exc = case.exception_type
            if exc in FORBIDDEN_FAILURE_TYPES:
                return RefusalCode.RED_INVALID_TEST, f"{case.test_id} failed with {exc}, which means the test is broken, not that the bug reproduced"
            if exc in ASSERTION_FAILURE_TYPES or (exc and exc in named):
                continue
            if exc and _location_kind(case.failure_location, self.sandbox.workspace_dir) == "repo":
                continue
            return RefusalCode.RED_WRONG_REASON, (
                f"{case.test_id} failed with {exc or 'an unidentified exception'} raised outside repository code, "
                "which is neither an assertion nor an exception named in the issue"
            )
        return None, ""

    def run_reproduction_gate(
        self,
        issue_title: str,
        issue_body: str,
        generated_test_code: Optional[str] = None,
    ) -> ReproductionResult:
        test_code = generated_test_code or ""
        issue_text = f"{issue_title}\n{issue_body}"
        if not test_code.strip():
            return self._refuse(RefusalCode.NO_REPRODUCTION_TEST, "No test code provided or generated.", test_code)

        try:
            ast.parse(test_code)
        except SyntaxError as exc:
            return self._refuse(RefusalCode.RED_INVALID_TEST, f"SyntaxError in reproduction test: {exc}", test_code)

        if self._repo_symbols is None:
            self._repo_symbols = repository_symbols(self.sandbox.workspace_dir)
        reason = check_test_relevance(test_code, self.sandbox.workspace_dir, issue_text, self._repo_symbols)
        if reason:
            return self._refuse(RefusalCode.RED_UNRELATED_TEST, f"Reproduction test rejected: {reason}.", test_code)

        self.sandbox.ensure_harness_dir()
        self.sandbox.write_file(REPRO_TEST_PATH, test_code)

        signatures = []
        outputs = []
        last_code = -1
        failing: List[str] = []
        failure_types: Dict[str, str] = {}
        for run in range(1, self.runs + 1):
            report, output, returncode, problem = self._run_once(f"red-{run}.xml")
            outputs.append(output)
            last_code = returncode
            if report is None:
                return self._refuse(RefusalCode.RED_UNVERIFIABLE, f"RED gate could not be verified: {problem}", test_code, output, returncode, run)
            if run == 1:
                code, message = self._classify(report, issue_text)
                if code is not None:
                    return self._refuse(code, f"RED gate not satisfied: {message}.", test_code, output, returncode, run)
                failing = report.ids_with(FAILED)
                failure_types = {c.test_id: c.exception_type or "" for c in report.cases if c.outcome == FAILED}
            signatures.append(sorted((c.test_id, c.outcome, c.exception_type or "") for c in report.cases))
            if signatures[-1] != signatures[0]:
                return self._refuse(
                    RefusalCode.RED_NONDETERMINISTIC,
                    f"RED gate not satisfied: run {run} of the reproduction test disagreed with run 1, so the failure is not deterministic.",
                    test_code, output, returncode, run,
                )

        return ReproductionResult(
            reproduced=True,
            test_code=test_code,
            error_message="",
            returncode=last_code,
            raw_output=outputs[0],
            failing_test_ids=failing,
            failure_types=failure_types,
            runs=self.runs,
            test_sha256=_sha256(test_code),
        )

    def check_green(self, red: ReproductionResult, report_name: str = "green.xml") -> GreenResult:
        """One GREEN check of the unmodified reproduction test."""
        try:
            current = self.sandbox.read_file(REPRO_TEST_PATH)
        except FileNotFoundError:
            current = ""
        if _sha256(current) != red.test_sha256:
            return GreenResult(False, "The reproduction test was modified or removed after RED.", "", RefusalCode.REPRODUCTION_TEST_TAMPERED.value)

        report, output, _, problem = self._run_once(report_name)
        if report is None:
            return GreenResult(False, f"GREEN could not be verified: {problem}", output, RefusalCode.GREEN_NOT_REACHED.value)
        by_id = report.by_id
        not_passing = [c.test_id for c in report.cases if c.outcome != PASSED]
        if report.total == 0 or not_passing:
            return GreenResult(False, f"Reproduction test is not passing: {', '.join(not_passing) or 'no tests collected'}", output, RefusalCode.GREEN_NOT_REACHED.value)
        missing = [tid for tid in red.failing_test_ids if tid not in by_id]
        if missing:
            return GreenResult(False, f"Tests that failed at RED did not run: {', '.join(missing)}", output, RefusalCode.GREEN_NOT_REACHED.value)
        return GreenResult(True, "", output)

    def verify_green(self, red: ReproductionResult, runs: Optional[int] = None) -> GreenResult:
        """Repeat the GREEN check; every run must pass."""
        total = runs or self.runs
        last = GreenResult(False, "GREEN not checked.", "")
        for run in range(1, total + 1):
            last = self.check_green(red, report_name=f"green-verify-{run}.xml")
            if not last.passed:
                if run > 1 and last.refusal_code == RefusalCode.GREEN_NOT_REACHED.value:
                    last.refusal_code = RefusalCode.GREEN_NONDETERMINISTIC.value
                    last.message = f"GREEN run {run} of {total} failed after earlier runs passed: {last.message}"
                return last
        return last

    def green_verifier(self, red: ReproductionResult) -> Callable[[], Tuple[bool, str]]:
        """Adapter for PatchAgent: returns (passed, output-for-feedback)."""
        def _verify() -> Tuple[bool, str]:
            res = self.check_green(red)
            return res.passed, (res.message + "\n" + res.raw_output).strip()
        return _verify

    def synthesize_and_verify(
        self,
        issue_title: str,
        issue_body: str,
        synthesizer_fn: Callable[[int, str], str],
        max_attempts: int = 3,
    ) -> ReproductionResult:
        """Runs an iterative test synthesis loop until RED gate passes or max_attempts is reached."""
        feedback = "Initial attempt: please provide a minimal test_reproduce.py reproducing the bug."
        last_res: Optional[ReproductionResult] = None
        for attempt in range(1, max_attempts + 1):
            candidate_code = synthesizer_fn(attempt, feedback)
            if not candidate_code.strip():
                feedback = f"Attempt #{attempt} provided empty test code."
                continue
            res = self.run_reproduction_gate(issue_title, issue_body, candidate_code)
            if res.reproduced:
                return res
            last_res = res
            feedback = f"Attempt #{attempt} failed RED gate: {res.error_message}\nOutput:\n{res.raw_output}"

        return last_res or self._refuse(
            RefusalCode.NO_REPRODUCTION_TEST,
            f"Reproduction synthesis exhausted {max_attempts} attempts without passing RED gate.",
            "",
        )

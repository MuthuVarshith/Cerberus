"""
JUnit XML test results.

Every gate that depends on a test run reads its outcome from a JUnit XML report
rather than from console text. A report that is missing, malformed, or
suspicious is treated as "no evidence", never as a pass.

Parsing notes (pytest):
  - A test that raised during its call phase has a <failure> child; its last
    traceback line is "<path>:<line>: <ExceptionType>", which identifies both the
    exception type and the innermost frame's file.
  - Setup, teardown and collection problems are <error> children.
  - Collection errors produce a testcase with an empty classname.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MAX_REPORT_BYTES = 20 * 1024 * 1024

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"

_LAST_FRAME_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<exc>[A-Za-z_][\w.]*)\s*$")
_MESSAGE_EXC_RE = re.compile(r"^(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)|Failed)\b")


class JUnitReportError(ValueError):
    """The report does not exist or cannot be trusted as evidence."""


@dataclass
class TestCaseResult:
    __test__ = False  # not a pytest test class
    test_id: str
    outcome: str
    message: str = ""
    exception_type: Optional[str] = None
    failure_location: Optional[str] = None
    details: str = ""


@dataclass
class TestReport:
    __test__ = False  # not a pytest test class
    cases: List[TestCaseResult] = field(default_factory=list)

    @property
    def by_id(self) -> Dict[str, TestCaseResult]:
        return {c.test_id: c for c in self.cases}

    def ids_with(self, *outcomes: str) -> List[str]:
        return [c.test_id for c in self.cases if c.outcome in outcomes]

    @property
    def total(self) -> int:
        return len(self.cases)

    def count(self, outcome: str) -> int:
        return sum(1 for c in self.cases if c.outcome == outcome)

    @property
    def has_collection_errors(self) -> bool:
        return any(c.outcome == ERROR and c.test_id.startswith("::") for c in self.cases)


def _exception_details(message: str, text: str) -> tuple[Optional[str], Optional[str]]:
    """Return (exception_type, innermost_frame_path) from a failure element."""
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    if lines:
        match = _LAST_FRAME_RE.match(lines[-1].strip())
        if match:
            return match.group("exc").split(".")[-1], match.group("path")
    msg = (message or "").strip()
    if msg.startswith("assert "):
        return "AssertionError", None
    match = _MESSAGE_EXC_RE.match(msg)
    if match:
        return match.group("exc").split(".")[-1], None
    return None, None


def parse_junit_xml(data: bytes) -> TestReport:
    """Parse a JUnit XML document. Raises JUnitReportError when it is unusable."""
    if len(data) > MAX_REPORT_BYTES:
        raise JUnitReportError(f"JUnit report exceeds {MAX_REPORT_BYTES} bytes.")
    head = data[:4096].upper()
    # The report file lives in a workspace the repository under test can write
    # to, so entity declarations are refused outright rather than expanded.
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise JUnitReportError("JUnit report contains a DTD or entity declarations.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise JUnitReportError(f"JUnit report is not valid XML: {exc}") from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise JUnitReportError(f"Unexpected JUnit root element <{root.tag}>.")

    report = TestReport()
    for tc in root.iter("testcase"):
        classname = tc.get("classname") or ""
        name = tc.get("name") or ""
        test_id = f"{classname}::{name}"
        outcome = PASSED
        message = ""
        exc_type: Optional[str] = None
        location: Optional[str] = None
        details = ""
        for child in tc:
            if child.tag in ("failure", "error"):
                outcome = FAILED if child.tag == "failure" else ERROR
                message = child.get("message") or ""
                details = (child.text or "")[-4000:]
                exc_type, location = _exception_details(message, child.text or "")
                if child.get("type") and not exc_type:
                    exc_type = child.get("type").split(".")[-1]
                break
            if child.tag == "skipped":
                outcome = SKIPPED
                message = child.get("message") or ""
        report.cases.append(
            TestCaseResult(
                test_id=test_id,
                outcome=outcome,
                message=message,
                exception_type=exc_type,
                failure_location=location,
                details=details,
            )
        )
    return report


def read_junit_report(path: str) -> TestReport:
    if not os.path.isfile(path):
        raise JUnitReportError(f"JUnit report was not written: {path}")
    with open(path, "rb") as f:
        return parse_junit_xml(f.read(MAX_REPORT_BYTES + 1))


def read_workspace_junit_report(sandbox, rel_path: str) -> TestReport:
    """Read a report the sandboxed test run wrote, without following symlinks."""
    try:
        data = sandbox.read_bytes(rel_path, MAX_REPORT_BYTES + 1)
    except FileNotFoundError:
        raise JUnitReportError(f"JUnit report was not written: {rel_path}") from None
    except (OSError, RuntimeError) as exc:
        raise JUnitReportError(f"JUnit report could not be read safely: {exc}") from exc
    return parse_junit_xml(data)

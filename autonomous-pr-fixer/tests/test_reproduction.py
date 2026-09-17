"""
Tests for the RED gate and the GREEN check.

Each test pins one rule of the reproduction contract, including the ways a
generated test can fail for the wrong reason.
"""
import pytest

from agents.reproduction_agent import REPRO_TEST_PATH, ReproductionAgent, check_test_relevance
from harness.docker_sandbox import Sandbox

BUGGY_DISCOUNT = (
    "def calculate_discount(price, rate):\n"
    "    # Bug: adds discount instead of subtracting\n"
    "    return price + (price * rate)\n"
)
ISSUE_TITLE = "calculate_discount increases price"
ISSUE_BODY = "calculate_discount(100, 0.10) gives 110 instead of 90"


@pytest.fixture
def sb():
    with Sandbox() as sandbox:
        sandbox.write_file("calculator.py", BUGGY_DISCOUNT)
        yield sandbox


def _gate(sb, code, title=ISSUE_TITLE, body=ISSUE_BODY, runs=3):
    return ReproductionAgent(sb, runs=runs).run_reproduction_gate(title, body, code)


def test_valid_assertion_failure_is_reproduced(sb):
    res = _gate(sb, (
        "from calculator import calculate_discount\n\n"
        "def test_discount():\n"
        "    result = calculate_discount(100, 0.10)\n"
        "    assert result == 90.0, f'Expected 90.0 but got {result}'\n"
    ))
    assert res.reproduced is True
    assert res.runs == 3
    assert res.failure_types == {".cerberus.test_reproduce::test_discount": "AssertionError"}
    assert "Expected 90.0 but got 110.0" in res.raw_output


def test_passing_test_is_refused(sb):
    res = _gate(sb, "from calculator import calculate_discount\ndef test_ok():\n    assert calculate_discount(100, 0) == 100\n")
    assert res.reproduced is False
    assert res.refusal_code == "RED_NOT_FAILING"


def test_syntax_error_is_refused_before_running(sb):
    res = _gate(sb, "def test_syntax_err(:\n    assert True\n")
    assert res.reproduced is False
    assert res.refusal_code == "RED_INVALID_TEST"
    assert "SyntaxError" in res.error_message
    assert res.runs == 0


def test_test_without_repository_import_is_refused(sb):
    res = _gate(sb, "def test_math():\n    assert 1 + 1 == 3\n")
    assert res.reproduced is False
    assert res.refusal_code == "RED_UNRELATED_TEST"


def test_test_ignoring_the_symbol_named_in_the_issue_is_refused(sb):
    sb.write_file("other.py", "def helper():\n    return 1\n")
    res = _gate(sb, "from other import helper\ndef test_helper():\n    assert helper() == 2\n")
    # other.py defines nothing the issue names, and calculator isn't imported.
    assert res.reproduced is False
    assert res.refusal_code == "RED_UNRELATED_TEST"


def test_import_error_is_never_a_reproduction(sb):
    """A module-level ImportError is a collection error, reported as an <error>."""
    res = _gate(sb, (
        "from calculator import calculate_discount\n"
        "from calculator import missing_function\n\n"
        "def test_discount():\n"
        "    assert calculate_discount(100, 0.10) == 90.0\n"
    ))
    assert res.reproduced is False
    assert res.refusal_code == "RED_INVALID_TEST"


def test_import_error_inside_test_body_is_refused(sb):
    res = _gate(sb, (
        "from calculator import calculate_discount\n\n"
        "def test_discount():\n"
        "    import not_installed_dependency\n"
        "    assert calculate_discount(100, 0.10) == 90.0\n"
    ))
    assert res.reproduced is False
    assert res.refusal_code == "RED_INVALID_TEST"
    assert "ModuleNotFoundError" in res.error_message


def test_fixture_error_is_refused(sb):
    res = _gate(sb, (
        "import pytest\n"
        "from calculator import calculate_discount\n\n"
        "@pytest.fixture\n"
        "def price():\n"
        "    raise RuntimeError('fixture broke')\n\n"
        "def test_discount(price):\n"
        "    assert calculate_discount(price, 0.10) == 90.0\n"
    ))
    assert res.reproduced is False
    assert res.refusal_code == "RED_INVALID_TEST"


def test_name_error_from_the_test_itself_is_the_wrong_reason(sb):
    """A hallucinated call fails in the test file with an exception the issue never mentions."""
    res = _gate(sb, (
        "from calculator import calculate_discount\n\n"
        "def test_discount():\n"
        "    assert calculate_discount(100, 0.10) == apply_rounding(90.0)\n"
    ))
    assert res.reproduced is False
    assert res.refusal_code == "RED_WRONG_REASON"


def test_exception_raised_inside_repository_code_is_accepted(sb):
    sb.write_file("rates.py", "def rate(amount, total):\n    return amount / total\n")
    res = _gate(
        sb,
        "from rates import rate\n\ndef test_zero_total():\n    assert rate(10, 0) == 0.0\n",
        title="rate crashes on zero total",
        body="rate(10, 0) should return 0.0",
    )
    assert res.reproduced is True
    assert res.failure_types[".cerberus.test_reproduce::test_zero_total"] == "ZeroDivisionError"


def test_exception_named_in_issue_is_accepted_even_from_test_code(sb):
    res = _gate(
        sb,
        (
            "from calculator import calculate_discount\n\n"
            "def test_discount():\n"
            "    if calculate_discount(100, 0.10) != 90.0:\n"
            "        raise ValueError('wrong discount')\n"
        ),
        body="calculate_discount(100, 0.10) returns 110; callers raise ValueError downstream",
    )
    assert res.reproduced is True


def test_nondeterministic_failure_is_refused(sb):
    """A test that fails only on its first run cannot prove the bug."""
    res = _gate(sb, (
        "import os\n"
        "from calculator import calculate_discount\n\n"
        "MARKER = os.path.join(os.path.dirname(__file__), 'ran_once')\n\n"
        "def test_discount():\n"
        "    first = not os.path.exists(MARKER)\n"
        "    open(MARKER, 'w').close()\n"
        "    assert not first or calculate_discount(100, 0.10) == 90.0\n"
    ))
    assert res.reproduced is False
    assert res.refusal_code == "RED_NONDETERMINISTIC"


def test_green_passes_after_fix_and_detects_tampering(sb):
    agent = ReproductionAgent(sb, runs=1)
    red = agent.run_reproduction_gate(ISSUE_TITLE, ISSUE_BODY, (
        "from calculator import calculate_discount\n\n"
        "def test_discount():\n"
        "    assert calculate_discount(100, 0.10) == 90.0\n"
    ))
    assert red.reproduced

    assert agent.check_green(red).passed is False

    sb.write_file("calculator.py", "def calculate_discount(price, rate):\n    return price - (price * rate)\n")
    assert agent.verify_green(red, runs=2).passed is True

    sb.write_file(REPRO_TEST_PATH, "def test_discount():\n    assert True\n")
    tampered = agent.check_green(red)
    assert tampered.passed is False
    assert tampered.refusal_code == "REPRODUCTION_TEST_TAMPERED"


def test_relevance_accepts_calls_when_issue_names_no_symbol(tmp_path):
    (tmp_path / "svc.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    code = "from svc import run\ndef test_it():\n    assert run() == 2\n"
    assert check_test_relevance(code, str(tmp_path), "the service returns the wrong value") is None

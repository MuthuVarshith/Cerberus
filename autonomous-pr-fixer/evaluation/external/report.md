# Cerberus on external repositories

Real bug fixes from open-source Python projects not written by the Cerberus author, verified in the Docker sandbox. Each bug is pinned to its upstream fix commit; the reproduction test is the test that commit added. Wrong variants are hand-made from the same bug.

**11 of 11 runs ended in the expected state and refusal code.**

| Environment | |
| --- | --- |
| cerberus_commit | `e394d03098f2659f596d8d9fa34dec7bd8f31e18` |
| cerberus_dirty | `no` |
| docker_server | `29.8.0 linux/amd64` |
| sandbox_image | `sha256:83efba55adad` |
| host | `Windows 11, Python 3.13.0` |
| finished_utc | `2026-09-17T14:41:57Z` |

| Bug | Variant | Expected | Actual | Match | RED failure | Baseline tests | Newly failing | Test files run at base | Wall (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sqlparse-332 | upstream-fix | ADMITTED | ADMITTED | yes | AssertionError | 492 | - | - | 32.1 |
| sqlparse-332 | upstream-commit | ADMITTED | ADMITTED | yes | AssertionError | 492 | - | tests/test_parse.py | 39.0 |
| sqlparse-332 | regressing | REFUSED / REGRESSION | REFUSED / REGRESSION | yes | AssertionError | 492 | tests.test_parse::test_parse_access_symbol, tests.test_parse::test_parse_square_brackets_notation_isnt_too_greedy, tests.test_parse::test_sqlite_identifiers | - | 33.1 |
| sqlparse-332 | test-weakening | REFUSED / SCOPE_VIOLATION | REFUSED / SCOPE_VIOLATION | yes | AssertionError | 492 | - | - | 36.7 |
| boltons-backoff | upstream-fix | ADMITTED | ADMITTED | yes | ZeroDivisionError | 445 | - | - | 29.8 |
| boltons-backoff | upstream-commit | ADMITTED | ADMITTED | yes | ZeroDivisionError | 445 | - | tests/test_iterutils.py | 25.6 |
| boltons-backoff | incomplete | REFUSED / GREEN_NOT_REACHED | REFUSED / GREEN_NOT_REACHED | yes | ZeroDivisionError | 445 | - | - | 19.4 |
| moreitertools-last | upstream-fix | ADMITTED | ADMITTED | yes | ValueError | 679 | - | - | 39.4 |
| moreitertools-last | upstream-commit | ADMITTED | ADMITTED | yes | ValueError | 679 | - | tests/test_more.py | 43.7 |
| moreitertools-last | invalid-reproduction | REFUSED / RED_INVALID_TEST | REFUSED / RED_INVALID_TEST | yes | - | - | - | - | 7.3 |
| moreitertools-last | non-reproducing-test | REFUSED / RED_NOT_FAILING | REFUSED / RED_NOT_FAILING | yes | - | - | - | - | 7.1 |

## Bugs

- **sqlparse-332**: https://github.com/andialbrecht/sqlparse.git (BSD-3-Clause), fix `f66d12c245412f28c58f045b646eb53c0e691b8b`, reproduction test from `tests/test_parse.py` in that commit.
- **boltons-backoff**: https://github.com/mahmoud/boltons.git (BSD-3-Clause), fix `ead236e278ca0466bf468de746b5960fb12d7e5b`, reproduction test from `tests/test_iterutils.py` in that commit.
- **moreitertools-last**: https://github.com/more-itertools/more-itertools.git (MIT), fix `cca32949f12d473fd823e37a5530c30d2faa1332`, reproduction test from `tests/test_more.py` in that commit.

## Refusal details

- **sqlparse-332 / regressing** (fixes the bug but also strips identifier quoting): The patch introduced regressions: tests.test_parse::test_parse_access_symbol, tests.test_parse::test_parse_square_brackets_notation_isnt_too_greedy, tests.test_parse::test_sqlite_identifiers
- **sqlparse-332 / test-weakening** (the regressing patch plus edits to the three existing tests it breaks): Existing test files were modified or deleted: tests/test_parse.py (set scope.allow_test_modifications in .cerberus.yml to permit this)
- **boltons-backoff / incomplete** (raises ValueError for every factor of 1.0, including start == stop): No candidate patch made the reproduction test pass after 1 attempt(s). Last attempt: FAIL: ValueError.
- **moreitertools-last / invalid-reproduction** (the upstream test plus an import that does not exist): RED gate not satisfied: the reproduction test errored outside its test body (::.cerberus.test_reproduce: collection failure); setup, fixture and collection errors are not reproductions.
- **moreitertools-last / non-reproducing-test** (a test that already passes on the buggy code): RED gate not satisfied: the reproduction test passed on the unpatched code, so it does not reproduce the bug.

## Limits

- Three bugs in three small pure-Python libraries, chosen by the Cerberus author for fast test suites and a
  test added in the fix commit. This shows the gates run on code Cerberus was not written against; it is not a
  sample of real-world bugs and says nothing about repair ability.
- The wrong variants were written by the Cerberus author to exercise specific gates.
- Issue text is paraphrased from the fix commit, not copied from the upstream issue tracker.

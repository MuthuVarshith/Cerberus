# Cerberus benchmark v1 results

- Date (UTC): 2026-09-17T14:36:42+00:00
- Cerberus commit: `e394d03098f2659f596d8d9fa34dec7bd8f31e18`
- Benchmark manifest digest: `836fb2e9e603888a62d1b3fc81defe39a307c212b03d8ce3154f25260606230d`
- Sandbox: `docker`; Python 3.13.0; Windows-11-10.0.26200-SP0
- Patch source: pre-written candidate diffs (no model in the loop)

> Instances are small synthetic libraries with seeded bugs, authored by the Cerberus developer. This measures
> the gate's decisions on known cases, not repair ability or performance on real-world repositories.

## Decisions

| Metric | Cerberus | Ungated baseline |
| --- | --- | --- |
| Admitted | 11 | 20 |
| False-admission rate (admitted but should be refused) | 4/11 = 36% (95% CI 15%–65%) | 13/20 = 65% (95% CI 43%–82%) |
| Admitted code failing hidden tests | 4 | 7 |
| Admitted-fix rate (correct fixes admitted) | 7/7 = 100% (95% CI 65%–100%) | 7/7 = 100% (95% CI 65%–100%) |

## Cerberus only

- Rejection accuracy (refused with an acceptable code): 12/17 = 71% (95% CI 47%–87%)
- Refused when it should refuse (any code): 13/17 = 76% (95% CI 53%–90%)
- False-rejection rate: 0/7 = 0% (95% CI 0%–35%)
- Reproduction validity (RED-passing tests that pass on the reference fix): 19/19 = 100% (95% CI 83%–100%)
- Refusals by code: {"GREEN_NOT_REACHED": 1, "RED_INVALID_TEST": 1, "RED_NONDETERMINISTIC": 1, "RED_NOT_FAILING": 1, "RED_UNRELATED_TEST": 1, "RED_WRONG_REASON": 1, "REGRESSION": 3, "REGRESSION_UNVERIFIABLE": 1, "SCOPE_VIOLATION": 3}
- Mean patch attempts: 1
- Wall time per instance: median 10.04s, p90 12.99s (includes sandbox setup and repeated RED/GREEN runs)
- Cost per admitted fix: not measured (no model in the loop)

## Per instance

| Instance | Category | Ideal | Cerberus | Code | Hidden tests | Ungated |
| --- | --- | --- | --- | --- | --- | --- |
| tk-01 | correct-fix | ADMITTED | ADMITTED |  | pass | admit |
| tk-02 | plausible-wrong-fix | REFUSED | ADMITTED |  | fail | admit |
| tk-03 | regression | REFUSED | REFUSED | REGRESSION | fail | admit |
| tk-04 | scope-leak | REFUSED | REFUSED | SCOPE_VIOLATION | pass | admit |
| tk-05 | test-weakening | REFUSED | REFUSED | SCOPE_VIOLATION | pass | admit |
| tk-06 | non-reproducible | REFUSED | REFUSED | RED_NOT_FAILING | pass | admit |
| tk-07 | invalid-reproduction | REFUSED | REFUSED | RED_INVALID_TEST | pass | refuse |
| tk-08 | invalid-reproduction | REFUSED | REFUSED | RED_WRONG_REASON | pass | refuse |
| iv-01 | correct-fix | ADMITTED | ADMITTED |  | pass | admit |
| iv-02 | plausible-wrong-fix | REFUSED | ADMITTED |  | fail | admit |
| iv-03 | correct-fix | ADMITTED | ADMITTED |  | pass | admit |
| iv-04 | regression | REFUSED | REFUSED | REGRESSION | fail | admit |
| iv-05 | scope-leak | REFUSED | REFUSED | SCOPE_VIOLATION | pass | admit |
| iv-06 | invalid-reproduction | REFUSED | REFUSED | RED_UNRELATED_TEST | pass | refuse |
| iv-07 | ineffective-patch | REFUSED | REFUSED | GREEN_NOT_REACHED | fail | refuse |
| iv-08 | correct-fix | ADMITTED | ADMITTED |  | pass | admit |
| mn-01 | correct-fix | ADMITTED | ADMITTED |  | pass | admit |
| mn-02 | plausible-wrong-fix | REFUSED | ADMITTED |  | fail | admit |
| mn-03 | correct-fix | ADMITTED | ADMITTED |  | pass | admit |
| mn-04 | plausible-wrong-fix | REFUSED | ADMITTED |  | fail | admit |
| mn-05 | regression | REFUSED | REFUSED | REGRESSION | fail | admit |
| mn-06 | correct-fix | ADMITTED | ADMITTED |  | pass | admit |
| mn-07 | test-weakening | REFUSED | REFUSED | REGRESSION_UNVERIFIABLE | pass | admit |
| mn-08 | invalid-reproduction | REFUSED | REFUSED | RED_NONDETERMINISTIC | pass | admit |

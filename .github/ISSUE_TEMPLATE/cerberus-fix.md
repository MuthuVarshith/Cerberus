---
name: Bug with reproduction test (Cerberus)
about: Report a bug with a failing test so Cerberus can attempt a verified fix
labels: ""
---

## What is wrong

<!-- Describe the bug: expected vs actual behaviour. -->

## Reproduction test

<!--
Cerberus holds every candidate patch to this test. It must FAIL on the current
default branch and PASS once the bug is fixed. Keep exactly this shape:
a fenced ```python block with at least one function named test_...
It runs under pytest from the repository root; import code the way the repo's tests do.
-->

```python
from mypackage.module import function_under_test

def test_bug_is_fixed():
    assert function_under_test(1) == 2
```

<!--
A maintainer (write access) then comments `/cerberus fix` or adds the `cerberus-fix` label.
Cerberus reports back here; if the gate ADMITS a patch, a maintainer comments
`/cerberus approve` to open a draft PR, or `/cerberus reject`. Cerberus never merges.
-->

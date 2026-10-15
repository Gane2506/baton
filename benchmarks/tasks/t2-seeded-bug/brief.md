# T2: make the failing test suite green

The repo contains a tiny calculator (`calc.py`) with a test suite under
`tests/` (`pytest`). One of the tests fails — there is a bug in the source.

Find and fix the bug so the whole suite passes:

```
$ python -m pytest tests/ -q
```

Do **not** modify, move or delete anything under `tests/` — fix the source,
not the tests. The grader re-runs the suite itself, checks that the diff does
not touch `tests/`, and runs extra hidden edge-case tests against the fixed
functions.

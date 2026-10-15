"""Grader for T2 (seeded bugfix). Protocol: `--result DIR --run-json FILE --diff FILE`;
prints one JSON report on stdout; exits 0 (passed), 1 (graded failure) or 2
(harness error). Self-contained: stdlib + pytest only, no Baton internals.

Grading: the fixture's own suite must pass (collected from the result dir),
the hidden edge-case tests must pass, and the diff must not touch `tests/`
(fix the source, not the tests).
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from jev_agent.capbench.grading import grade_main, tests_dir_untouched
except ImportError:  # grade.py run outside the installed package: use the repo's src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    from jev_agent.capbench.grading import grade_main, tests_dir_untouched

HIDDEN_TESTS = {
    "test_bugfix_hidden.py": '''\
"""Hidden edge-case tests for T2 (the agent never sees these)."""
from calc import add, mul  # the result dir is on sys.path via the harness conftest


def test_add_negative():
    assert add(-1, -2) == -3


def test_add_mixed_sign():
    assert add(-5, 8) == 3


def test_add_floats():
    assert add(1.5, 2.5) == 4.0


def test_add_identity():
    assert add(0, 7) == 7
    assert add(7, 0) == 7


def test_mul_still_works():
    assert mul(-3, 4) == -12
    assert mul(0, 99) == 0
''',
}


def _tests_untouched(diff: str, result_dir: Path) -> list[str]:
    if not tests_dir_untouched(diff):
        return ["diff touches tests/: fix the source, not the tests"]
    return []


def main() -> int:
    return grade_main(
        "t2-seeded-bug",
        HIDDEN_TESTS,
        max_added=150,
        max_files=6,
        extra_collect=("tests",),  # the fixture's own suite must go green too
        extra_checks=(_tests_untouched,),
    )


if __name__ == "__main__":
    sys.exit(main())

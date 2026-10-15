"""Visible test suite for T2 (the agent sees and may run these; not modify them)."""

from calc import add, mul


def test_add_positive():
    assert add(2, 3) == 5


def test_add_zero():
    assert add(0, 0) == 0


def test_mul():
    assert mul(3, 4) == 12

"""Errors raised by `jev ship` (user-facing messages)."""

from __future__ import annotations


class ShipError(Exception):
    """`jev ship` cannot (or did not) go ahead. The message is user-facing."""


class GhTimeoutError(ShipError):
    """`gh` did not finish in time: what it was doing may or may not have happened."""

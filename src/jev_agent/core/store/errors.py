"""Store errors."""

from __future__ import annotations


class StoreError(Exception):
    """Base class for store errors."""


class NotFoundError(StoreError, LookupError):
    """A run, task or attempt id does not exist."""

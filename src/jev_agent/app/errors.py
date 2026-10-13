"""User-facing errors shared by the front ends (issue #92)."""

from __future__ import annotations


class JevError(Exception):
    """A command cannot go ahead; the message is for the user. The CLI prints it and exits 1
    (`cli.wiring.fail`); other front ends (the dashboard) show `str(exc)` (issue #51)."""

"""User-facing error text for the dashboard (standard #4: actionable errors in the UI).

Backend exceptions keep their CLI-flavoured text (the CLI needs it); `friendly_error`
translates the known jargon into what the dashboard user can actually do, in the UI,
wherever an exception reaches a flash, a status page, or a live fragment. A rule is
(match, replacement): a match at the start of the message replaces the whole message,
otherwise the matched part is swapped in place. New jargon goes in `_ERROR_REWRITES`
so every call site benefits at once. Messages stay to one or two lines and name the
page where the fix lives.
"""

_ERROR_REWRITES: tuple[tuple[str, str], ...] = (
    (
        "No workers are available",
        "No workers are available — turn on Claude Code, Codex, or the API backend "
        "on the Connections page, then try again.",
    ),
    (
        "no model for the api backend",
        "The API backend needs a default model — pick one with the model picker, then try again.",
    ),
    (
        "; set it or use the claude_code backend",
        " — add it on the Connections page, or pick the Claude Code backend instead.",
    ),
    (
        " CLI is not on PATH; install it or use the api backend",
        " CLI wasn't found — install it, then retry, or switch to the API backend.",
    ),
    (
        "`jev chat --list` shows saved chats",
        "the Chats page lists your saved chats.",
    ),
    (
        "--learning and --no-learning cannot be used together",
        "Pick one learning option, not both, then try again.",
    ),
    (
        "set `[ship] remote`",
        "set a ship remote under Settings → Ship",
    ),
    (
        "database has schema version",
        "This state database was written by a newer jev — upgrade jev to open it. "
        "Your runs, queue and history are safe; do not delete state.db.",
    ),
    (
        "needs shipping on: set `[ship] enabled = true` (see docs/ship.md)",
        "needs shipping turned on — enable it under Settings → Ship, then try again.",
    ),
)


def friendly_error(exc: BaseException, hint: str = "") -> str:
    """User-facing text for a caught exception: backend jargon rewritten into dashboard
    actions, plus an optional `hint` naming where the fix lives ("check the Settings
    page"). Never raises; falls back to the exception class name on an empty message."""
    message = str(exc).strip() or type(exc).__name__
    for match, replacement in _ERROR_REWRITES:
        if message.startswith(match):
            message = replacement
            break
        if match in message:
            message = message.replace(match, replacement)
            break
    if hint and hint not in message:
        message = f"{message} — {hint}"
    return message

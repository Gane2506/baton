"""`friendly_error`: dashboard flash text rewrites CLI jargon into UI actions (issue #169)."""

from __future__ import annotations

from jev_agent.dashboard.errors import friendly_error


def test_no_workers_points_at_connections() -> None:
    message = friendly_error(
        Exception("No workers are available: enable `[workers.api]` in jev.toml")
    )
    assert message.startswith("No workers are available — turn on")
    assert "Connections page" in message
    assert "jev.toml" not in message


def test_missing_api_key_points_at_connections() -> None:
    message = friendly_error(
        Exception("OPENROUTER_API_KEY is not set; set it or use the claude_code backend")
    )
    assert message == (
        "OPENROUTER_API_KEY is not set — add it on the Connections page, "
        "or pick the Claude Code backend instead."
    )


def test_missing_cli_names_the_fix() -> None:
    message = friendly_error(
        Exception("the claude CLI is not on PATH; install it or use the api backend")
    )
    assert "wasn't found" in message
    assert "PATH" not in message


def test_chat_list_jargon_becomes_chats_page() -> None:
    message = friendly_error(Exception("no chat 'c-1'; `jev chat --list` shows saved chats"))
    assert message == "no chat 'c-1'; the Chats page lists your saved chats."


def test_ship_remote_points_at_settings() -> None:
    message = friendly_error(
        Exception("this repo has no remote 'origin'; add one or set `[ship] remote`")
    )
    assert "Settings → Ship" in message


def test_ship_enable_points_at_settings() -> None:
    message = friendly_error(
        Exception("merge needs shipping on: set `[ship] enabled = true` (see docs/ship.md)")
    )
    assert "enable it under Settings → Ship" in message


def test_unknown_messages_pass_through_with_hint() -> None:
    assert friendly_error(Exception("something odd")) == "something odd"
    assert friendly_error(Exception("something odd"), hint="check the Settings page") == (
        "something odd — check the Settings page"
    )


def test_empty_message_falls_back_to_class_name() -> None:
    assert friendly_error(Exception("")) == "Exception"

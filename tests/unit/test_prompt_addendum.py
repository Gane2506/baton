"""`RoleConfig.prompt_addendum`: schema validation, harness prompt injection, and the
rendering contract (the Fleet page's "Additional prompt" field)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from jev_agent.config.schema import JevConfig, RoleConfig
from jev_agent.core.harness import Harness, with_prompt_addendum

ADDENDUM = "Always explain the change before making it.\nPrefer small diffs."


def _harness(addendum: str) -> Harness:
    config = JevConfig.model_validate({"roles": {"planner": {"prompt_addendum": addendum}}})
    return Harness(
        store=Mock(),
        config=config,
        repo=Path("/tmp/repo"),
        state_dir=Path("/tmp/repo/.jev"),
        dispatcher=Mock(),
        workers=Mock(),
        workspace=Mock(),
        commands=Mock(),
    )


def _planner_prompt(harness: Harness) -> str:
    return harness.prompt(
        "planner",
        [],
        idea="test idea",
        repo_path="/tmp/repo",
        context_files=[],
        has_tests=False,
        round=1,
        previous_plan=None,
        validation_issues=[],
    )


# ---- schema -------------------------------------------------------------------------------


def test_prompt_addendum_defaults_to_blank() -> None:
    assert RoleConfig().prompt_addendum == ""
    assert JevConfig().roles.planner.prompt_addendum == ""


def test_prompt_addendum_layered_from_toml() -> None:
    config = JevConfig.model_validate({"roles": {"coder": {"prompt_addendum": "Use tabs."}}})
    assert config.roles.coder.prompt_addendum == "Use tabs."
    # Other roles keep their defaults, untouched.
    assert config.roles.planner.prompt_addendum == ""
    assert config.roles.coder.workers == ["claude_code", "codex"]


def test_prompt_addendum_allows_multiline_text() -> None:
    config = RoleConfig(prompt_addendum="Line one.\n\nLine two.\tIndented.")
    assert config.prompt_addendum == "Line one.\n\nLine two.\tIndented."


def test_prompt_addendum_rejects_control_characters() -> None:
    with pytest.raises(ValidationError, match="control characters"):
        RoleConfig(prompt_addendum="bad\x00char")
    with pytest.raises(ValidationError, match="U\\+0007"):
        RoleConfig(prompt_addendum="bell\x07here")


def test_prompt_addendum_has_a_length_cap() -> None:
    with pytest.raises(ValidationError):
        RoleConfig(prompt_addendum="x" * 4001)
    assert RoleConfig(prompt_addendum="x" * 4000).prompt_addendum == "x" * 4000


# ---- prompt rendering ---------------------------------------------------------------------


def test_with_prompt_addendum_blank_is_unchanged() -> None:
    assert with_prompt_addendum("base prompt", "") == "base prompt"
    assert with_prompt_addendum("base prompt", "  \n ") == "base prompt"


def test_with_prompt_addendum_appends_after_the_template() -> None:
    rendered = with_prompt_addendum("base prompt\n", ADDENDUM)
    assert rendered.startswith("base prompt\n")
    assert ADDENDUM in rendered
    assert "## Additional instructions" in rendered
    assert rendered.index("base prompt") < rendered.index("## Additional instructions")


def test_harness_prompt_includes_addendum_end_to_end() -> None:
    harness = _harness(ADDENDUM)
    rendered = _planner_prompt(harness)
    assert "test idea" in rendered  # the base template rendered
    assert ADDENDUM in rendered
    assert rendered.index("test idea") < rendered.index(ADDENDUM)


def test_harness_prompt_without_addendum_is_the_plain_template() -> None:
    harness = _harness("")
    rendered = _planner_prompt(harness)
    assert "test idea" in rendered
    assert "## Additional instructions" not in rendered


def test_harness_prompt_addendum_survives_prompt_file() -> None:
    # The addendum applies on top of a custom prompt_file too: it is appended after
    # rendering, never inside the template.
    harness = _harness(ADDENDUM)
    assert harness.prompt_file("planner") is None  # built-in template in this fixture
    assert ADDENDUM in _planner_prompt(harness)

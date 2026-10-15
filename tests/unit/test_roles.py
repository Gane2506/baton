from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from jev_agent.core.learning import Lesson
from jev_agent.core.models import DesignDoc, Plan, TaskCard, ValidationResult
from jev_agent.roles import (
    CODER_SUMMARY_MARKER,
    ROLE_SCHEMAS,
    CoderSummary,
    LearnerOutput,
    PromptError,
    QualityReview,
    ReviewResult,
    builtin_template,
    extract_coder_summary,
    load_template,
    render_prompt,
)

CARD = TaskCard(
    id="T1",
    title="Add search",
    description="Implement search.",
    files_hint=["src/search.py"],
    acceptance_criteria=["returns matches"],
    test_command="pytest tests/test_search.py",
)

VARS: dict[str, dict[str, Any]] = {
    "planner": {
        "idea": "add search",
        "repo_path": "/repo",
        "context_files": ["docs/a.md"],
        "has_tests": True,
        "round": 2,
        "previous_plan": '{"summary": "old"}',
        "validation_issues": ["split T1"],
        "lessons": [],
    },
    "validator": {
        "idea": "add search",
        "plan_json": '{"summary": "p"}',
        "has_tests": True,
        "lessons": [],
    },
    "coder": {
        "idea": "add search",
        "plan_summary": "plan",
        "task": CARD,
        "attempt": 2,
        "feedback": "tests failed",
        "quality_findings": ["major bug src/search.py:3: off by one (ai-review)"],
        "lessons": [],
    },
    "reviewer": {"task": CARD, "diff": "+ new line", "test_output": "1 passed", "lessons": []},
    "code_reviewer": {
        "level": "production",
        "task": CARD,
        "idea": "add search",
        "plan_summary": "plan",
        "diff": "+ new line",
        "checks": [{"name": "ruff", "status": "pass", "detail": "clean"}],
        "security": True,
        "scalability": False,
        "lessons": [],
    },
    "learner": {
        "run_card": '{"outcome": "DONE"}',
        "profile": "default",
        "max_adds": 5,
        "lessons": [],
    },
}
ROLES = ["planner", "validator", "coder", "reviewer", "code_reviewer", "learner"]


def _lesson(number: int, text: str) -> Lesson:
    return Lesson(
        id=f"L{number}",
        profile="default",
        text=text,
        roles=("planner", "coder"),
        tags=("src",),
        source_run=None,
        source_task=None,
        created_at=datetime(2026, 10, 12, tzinfo=UTC),
        uses=0,
        score=1.5,
        retired=False,
    )


@pytest.mark.parametrize("role", ROLES)
def test_builtin_prompts_render(role: str) -> None:
    text = render_prompt(role, **VARS[role])  # type: ignore[arg-type]
    assert text.strip()
    assert "{{" not in text and "{%" not in text


def test_prompts_include_key_content() -> None:
    planner = render_prompt("planner", **VARS["planner"])
    assert "split T1" in planner and '"depends_on"' in planner and "test_command" in planner
    coder = render_prompt("coder", **VARS["coder"])
    assert f"```{CODER_SUMMARY_MARKER}" in coder
    assert "pytest tests/test_search.py" in coder and "tests failed" in coder
    reviewer = render_prompt("reviewer", **VARS["reviewer"])
    assert "+ new line" in reviewer and '"verdict"' in reviewer


def test_optional_sections_can_be_none() -> None:
    first = render_prompt(
        "planner", **{**VARS["planner"], "previous_plan": None, "validation_issues": []}
    )
    assert "Revision" not in first
    coder = render_prompt("coder", **{**VARS["coder"], "feedback": None, "quality_findings": []})
    assert "Previous attempt" not in coder and "Quality gate findings" not in coder


def test_coder_prompt_shows_quality_findings() -> None:
    coder = render_prompt("coder", **VARS["coder"])
    assert "Quality gate findings to fix" in coder and "off by one (ai-review)" in coder


def test_code_reviewer_prompt_sections_follow_the_level() -> None:
    production = render_prompt("code_reviewer", **VARS["code_reviewer"])
    assert "Security checklist" in production and "Path traversal" in production
    assert "Scalability checklist" not in production and "ruff: pass" in production
    scalable = render_prompt(
        "code_reviewer", **{**VARS["code_reviewer"], "level": "scalable", "scalability": True}
    )
    assert "Scalability checklist" in scalable and "Threat model" in scalable
    basic = render_prompt(
        "code_reviewer",
        **{**VARS["code_reviewer"], "level": "basic", "security": False, "task": None},
    )
    assert "Security checklist" not in basic and "The whole run" in basic


@pytest.mark.parametrize("role", ROLES)
def test_missing_variable_is_an_error(role: str) -> None:
    variables = dict(VARS[role])
    variables.pop(next(iter(variables)))
    with pytest.raises(PromptError, match="UndefinedError"):
        render_prompt(role, **variables)  # type: ignore[arg-type]


def test_user_prompt_file(tmp_path: Path) -> None:
    custom = tmp_path / "planner.md"
    custom.write_text("Plan {{ idea }} with marker {{ summary_marker }}")
    assert (
        render_prompt("planner", custom, idea="x") == f"Plan x with marker {CODER_SUMMARY_MARKER}"
    )
    with pytest.raises(PromptError, match=r"planner prompt \(prompt_file .*planner\.md\)"):
        render_prompt("planner", custom)


def test_missing_prompt_file_is_a_prompt_error(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="file not found"):
        load_template("coder", tmp_path / "nope.md")


def test_syntax_error_is_a_prompt_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("ok\n{% if idea %}\nno endif\n")
    with pytest.raises(PromptError, match="syntax error on line") as info:
        load_template("planner", bad)
    assert info.value.role == "planner"
    assert info.value.prompt_file == bad


def test_builtin_template_is_package_data() -> None:
    assert "planner" in builtin_template("planner")


def test_role_schemas() -> None:
    assert {
        "planner": Plan,
        "validator": ValidationResult,
        "coder": CoderSummary,
        "reviewer": ReviewResult,
        "code_reviewer": QualityReview,
        "learner": LearnerOutput,
        "designer": DesignDoc,
    } == ROLE_SCHEMAS


@pytest.mark.parametrize("role", ["planner", "validator", "coder", "reviewer"])
def test_lessons_section_only_with_lessons(role: str) -> None:
    without = render_prompt(role, **VARS[role])  # type: ignore[arg-type]
    assert "Lessons from earlier runs" not in without
    lessons = [
        _lesson(1, "Run `uv run pytest -q` from the repo root."),
        _lesson(2, "Keep it small."),
    ]
    with_lessons = render_prompt(role, **{**VARS[role], "lessons": lessons})  # type: ignore[arg-type]
    assert "## Lessons from earlier runs in this repo" in with_lessons
    assert "- Run `uv run pytest -q` from the repo root.\n- Keep it small.\n\n## " in with_lessons


def test_learner_prompt_shows_lesson_ids_and_card() -> None:
    lessons = [_lesson(3, "Prefer small tasks.")]
    text = render_prompt("learner", **{**VARS["learner"], "lessons": lessons})
    assert "`L3` (roles: planner, coder; tags: src; score 1.5): Prefer small tasks." in text
    assert '{"outcome": "DONE"}' in text and "At most 5 new lessons" in text
    assert "None yet." in render_prompt("learner", **VARS["learner"])


def test_extract_coder_summary() -> None:
    text = (
        'did things\n```jev-summary\n{"summary": "first"}\n```\nmore\n'
        '```jev-summary\n{"summary": "last", "files_changed": ["a.py"]}\n```\n'
    )
    assert extract_coder_summary(text) == CoderSummary(summary="last", files_changed=["a.py"])
    assert extract_coder_summary("no block") is None
    assert extract_coder_summary("```jev-summary\nnot json\n```") is None


def test_validator_prompt_asks_why_x_not_y() -> None:
    text = render_prompt("validator", **VARS["validator"])
    assert "## Challenge every choice" in text
    assert "why this instead of the" in text and "`(not stated)`" in text
    assert '"challenges": [' in text


SKILLS_SENTENCES = (
    "## Skills",
    "look for skills that fit this work: use the find-skills skill if you have it",
    "Use the skills that are already installed.",
    "Never install a new skill unless the maintainer has approved it.",
)


@pytest.mark.parametrize("role", [*ROLES, "chat"])
def test_every_builtin_prompt_has_the_skills_block(role: str) -> None:
    source = files("jev_agent.roles").joinpath(role, "prompt.md").read_text(encoding="utf-8")
    text = " ".join(source.split())
    assert all(part in text for part in SKILLS_SENTENCES)


@pytest.mark.parametrize("role", [r for r in ROLES if r != "coder"])
def test_skills_block_comes_before_the_output_format(role: str) -> None:
    text = render_prompt(role, **VARS[role])  # type: ignore[arg-type]
    assert text.count("## Skills") == 1
    assert text.index("## Skills") < text.index("## Output")

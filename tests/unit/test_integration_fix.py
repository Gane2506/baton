"""Fix rounds are counted by the task `kind`, not by the title prefix (#58)."""

from __future__ import annotations

from typing import Literal

import pytest

from jev_agent.core.commands import CommandResult
from jev_agent.core.integration import (
    FIX_TITLE,
    OUTPUT_TAIL_CHARS,
    FailedCommand,
    fix_rounds,
    fix_task_card,
    is_fix_task,
)
from jev_agent.core.models import TaskCard


def _card(id: str, title: str, kind: Literal["plan", "fix"] | None = None) -> TaskCard:
    return TaskCard(id=id, title=title, description="d", kind=kind)


def test_planner_task_with_fix_title_does_not_count() -> None:
    # The issue's scenario: a planned task whose title happens to start with the fix
    # prefix must not consume a fix round once kinds exist.
    card = _card("T1", "Fix integration failures in parser", kind="plan")
    assert not is_fix_task(card)
    assert fix_rounds([card]) == 0


def test_fix_tasks_count_by_kind_not_title() -> None:
    fix = _card("T2", f"{FIX_TITLE} (round 1)", kind="fix")
    assert is_fix_task(fix)
    assert fix_rounds([fix]) == 1
    # Renaming the title no longer resets the count for kind-marked cards.
    renamed = fix.model_copy(update={"title": "Make the suite green again"})
    assert fix_rounds([renamed]) == 1


def test_legacy_cards_without_kind_fall_back_to_title() -> None:
    # Runs written before kinds existed have kind=None; their fix tasks still count.
    legacy_fix = _card("T2", f"{FIX_TITLE} (round 1)")
    legacy_plan = _card("T1", "Build the parser")
    assert is_fix_task(legacy_fix)
    assert fix_rounds([legacy_plan, legacy_fix]) == 1


def test_fix_task_card_marks_kind() -> None:
    cards = [_card("T1", "Build the parser", kind="plan")]
    failure = FailedCommand("make test", CommandResult(2, "boom"))
    card = fix_task_card(cards, failure)
    assert card.kind == "fix"
    assert fix_rounds([*cards, card]) == 1
    # A second round counts on top of the first.
    card2 = fix_task_card([*cards, card], failure)
    assert fix_rounds([*cards, card, card2]) == 2


def test_fix_task_card_redacts_secret_straddling_the_tail_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#27 remainder: the tail was cut before redacting, so a secret split by the cut leaked."""
    secret = "sk-test-" + "s3cr3t" * 8
    monkeypatch.setenv("JEV_TEST_LEAK_SECRET", secret)
    # The tail cut lands inside the secret: cut-then-redact left the second half behind.
    output = "A" * 5000 + secret + "B" * (OUTPUT_TAIL_CHARS - 20)
    failure = FailedCommand("make test", CommandResult(2, output))
    card = fix_task_card([_card("T1", "Build the parser", kind="plan")], failure)
    assert secret not in card.description
    assert "[REDACTED]" in card.description

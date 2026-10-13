import pytest

from jev_agent.core.models import TaskCard
from jev_agent.core.taskgraph import CycleError, topological_order


def _card(tid: str, *deps: str) -> TaskCard:
    return TaskCard(id=tid, title=tid, description="d", depends_on=list(deps))


def _ids(cards: list[TaskCard]) -> list[str]:
    return [c.id for c in cards]


def test_in_order_plan_is_unchanged() -> None:
    cards = [_card("T1"), _card("T2", "T1"), _card("T3")]
    assert _ids(topological_order(cards)) == ["T1", "T2", "T3"]


def test_dependencies_come_first_and_ties_keep_plan_order() -> None:
    cards = [_card("T3", "T2"), _card("T1"), _card("T2", "T1"), _card("T4")]
    assert _ids(topological_order(cards)) == ["T1", "T2", "T3", "T4"]


def test_diamond() -> None:
    cards = [_card("T4", "T2", "T3"), _card("T2", "T1"), _card("T3", "T1"), _card("T1")]
    assert _ids(topological_order(cards)) == ["T1", "T2", "T3", "T4"]


def test_cycle_is_detected_and_named() -> None:
    cards = [_card("T1"), _card("T2", "T4"), _card("T3", "T2"), _card("T4", "T3")]
    with pytest.raises(CycleError) as info:
        topological_order(cards)
    assert set(info.value.cycle) == {"T2", "T3", "T4"}
    assert info.value.cycle[0] == info.value.cycle[-1]
    assert "dependency cycle" in str(info.value)

"""Unit tests for `jev_agent.core.skills`: registry sanity and the find-skills pass."""

import pytest

from jev_agent.core.skills import (
    SKILLS,
    describe_for_prompt,
    find_skills,
    skill_by_name,
)


def test_registry_has_stable_unique_names() -> None:
    names = [s.name for s in SKILLS]
    assert len(names) == len(set(names)) > 5
    for s in SKILLS:
        assert s.title and s.description and s.keywords and s.when_to_use


def test_find_design_skill() -> None:
    matches = find_skills("I want to create a design system for my dashboard")
    assert matches, "expected at least one skill"
    assert matches[0].skill.name == "design-new"


def test_find_reverse_skill() -> None:
    matches = find_skills("can you reverse the design from this url?")
    assert any(m.skill.name == "design-reverse" for m in matches)


def test_find_api_keys_skill() -> None:
    matches = find_skills("where do I add my OpenRouter API key?")
    assert any(m.skill.name == "api-keys" for m in matches)


def test_find_server_skill() -> None:
    matches = find_skills("add my contabo server so I can see cpu stats")
    assert any(m.skill.name == "manage-servers" for m in matches)


def test_find_provider_auth_skill() -> None:
    matches = find_skills("how do I sign in with Claude?")
    assert any(m.skill.name == "provider-auth" for m in matches)


def test_generic_chatter_matches_nothing() -> None:
    for msg in ("hello", "thanks!", "ok", "hmm interesting"):
        assert find_skills(msg) == [], f"unexpected match for {msg!r}"


def test_single_word_matches() -> None:
    # Issue #223: a single keyword used to score at most 0.6/sqrt(len(keywords))
    # — below the 0.25 bar — so single-keyword discovery could never match.
    # Each lone word below must now find its skill.
    cases = {
        "theme": "design-new",
        "design": "design-new",
        "typography": "design-new",
        "queue": "queue-idea",
        "backlog": "queue-idea",
        "brief": "write-brief",
        "server": "manage-servers",
        "contabo": "manage-servers",
        "openrouter": "api-keys",
        "codex": "provider-auth",
        "lessons": "learning",
        "help": "chat-help",
        "pipeline": "plan-run",
        "screenshot": "design-reverse",
    }
    for message, expected in cases.items():
        matches = find_skills(message)
        assert matches, f"no match for {message!r}"
        assert matches[0].skill.name == expected, message


def test_hyphenated_keyword_matches_phrase() -> None:
    matches = find_skills("give me a design-system for this app")
    assert matches and matches[0].skill.name == "design-new"


def test_phrase_outranks_single_word() -> None:
    matches = find_skills("design system server")
    assert [m.skill.name for m in matches[:2]] == ["design-new", "manage-servers"]
    assert matches[0].score > matches[1].score


def test_repeated_word_does_not_inflate_score() -> None:
    assert find_skills("design design design")[0].score == find_skills("design")[0].score


@pytest.mark.parametrize(
    "message",
    [
        "I don't want a design system",
        "no design system please",
        "do not queue this idea",
        "I never want server stats",
    ],
)
def test_negated_keyword_matches_nothing(message: str) -> None:
    assert find_skills(message) == [], f"unexpected match for {message!r}"


def test_why_not_and_not_only_are_not_negations() -> None:
    assert find_skills("why not design a new theme?")[0].skill.name == "design-new"
    assert find_skills("not only a design system, also a queue")[0].skill.name == "design-new"


def test_limit_and_ordering() -> None:
    matches = find_skills("design system api key server", limit=2)
    assert len(matches) <= 2
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_skill_by_name() -> None:
    assert skill_by_name("design-new") is not None
    assert skill_by_name("nope") is None


def test_describe_for_prompt_empty() -> None:
    assert describe_for_prompt([]) == ""


def test_describe_for_prompt_lists_skills() -> None:
    matches = find_skills("I want to create a design system")
    text = describe_for_prompt(matches)
    assert "Create a design system" in text
    assert "/design-studio" in text

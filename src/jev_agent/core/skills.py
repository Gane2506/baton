"""Baton's skill registry: what Baton can do, described for automatic discovery.

Each `Skill` names a capability, when to use it, and how to invoke it (dashboard
route and/or CLI command). `find_skills()` runs a lightweight keyword pass over a
user message and returns the best-matching skills — the chat pipeline calls it on
every message so Baton visibly "scours for skills" instead of being a black box.

Pure functions, stdlib-only: no model calls, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Skill:
    """One Baton capability the chat can discover and invoke."""

    name: str  # stable id, e.g. "design-new"
    title: str  # human label, e.g. "Create a design system"
    description: str  # one or two sentences: what it does
    keywords: tuple[str, ...]  # trigger words/phrases (lowercase, matched loosely)
    when_to_use: str  # guidance for the model: user intents this serves
    dashboard: str | None = None  # dashboard route, e.g. "/design-studio"
    cli: str | None = None  # CLI equivalent, e.g. "jev design new"
    action_hint: str = ""  # short user-facing "how to start", e.g. "Open Design Studio → New"


@dataclass
class SkillMatch:
    skill: Skill
    score: float  # 0..1, higher is a stronger match


SKILLS: tuple[Skill, ...] = (
    Skill(
        name="design-new",
        title="Create a design system",
        description=(
            "Build a new visual design system (colors, typography, spacing, components) "
            "via a guided wizard, with exportable reusable elements."
        ),
        keywords=(
            "design system",
            "design-system",
            "design",
            "theme",
            "branding",
            "color palette",
            "colours",
            "typography",
            "look and feel",
            "visual style",
            "style guide",
        ),
        when_to_use=(
            "The user wants a new design system, theme, brand look, color palette, "
            "or visual style — or asks how to make the dashboard/UI look different."
        ),
        dashboard="/design-studio",
        cli="jev design new",
        action_hint="Open Design Studio → New system, or run `jev design new`",
    ),
    Skill(
        name="design-reverse",
        title="Reverse a design system from a site",
        description=(
            "Extract a design system (colors, fonts, spacing) from an existing "
            "website URL or uploaded screenshots."
        ),
        keywords=(
            "reverse",
            "extract design",
            "from url",
            "url",
            "from screenshot",
            "screenshot",
            "from website",
            "website",
            "copy the style of",
            "like that site",
            "clone the look",
        ),
        when_to_use=(
            "The user points at an existing website or screenshots and wants its "
            "design tokens extracted as a reusable system."
        ),
        dashboard="/design-studio",
        cli="jev design reverse",
        action_hint="Open Design Studio → Reverse from URL/screenshots",
    ),
    Skill(
        name="queue-idea",
        title="Queue an idea for later",
        description=(
            "Add the chat's idea brief to the idea queue so Baton works on it later, "
            "with a priority."
        ),
        keywords=(
            "queue",
            "add to queue",
            "idea queue",
            "later",
            "backlog",
            "save this idea",
        ),
        when_to_use=(
            "The user wants to park an idea for later instead of running it now, "
            "or mentions the queue/backlog."
        ),
        dashboard="/chats",
        cli="jev queue add",
        action_hint="Use the 'Queue it' button in chat, or `/queue`",
    ),
    Skill(
        name="plan-run",
        title="Plan or run the idea",
        description=(
            "Turn the chat's idea brief into a planned pipeline run (agents, reviews, "
            "tests) — plan-only to preview, or full run to build."
        ),
        keywords=(
            "plan",
            "run it",
            "build it",
            "execute",
            "start building",
            "make it happen",
            "pipeline",
            "agents",
        ),
        when_to_use=(
            "The user is ready to turn the discussed idea into real work — they say "
            "plan, run, build, or execute."
        ),
        dashboard="/chats",
        cli="jev run",
        action_hint="Use the 'Plan it' or 'Run it' buttons in chat",
    ),
    Skill(
        name="write-brief",
        title="Write an idea brief",
        description=(
            "Ask the model to distill the conversation into a structured idea brief "
            "the planner can act on."
        ),
        keywords=(
            "brief",
            "write brief",
            "summarize the idea",
            "idea brief",
            "spec",
            "requirements",
        ),
        when_to_use=(
            "The conversation has converged on an idea and the user wants it written "
            "up as a brief — or asks for a summary/spec of what's discussed."
        ),
        dashboard="/chats",
        cli="jev chat brief",
        action_hint="Use the 'Write brief' button in chat, or `/brief`",
    ),
    Skill(
        name="manage-servers",
        title="Manage servers",
        description=(
            "Add SSH servers (for workers or stats), view CPU/RAM/disk health, and refresh stats."
        ),
        keywords=(
            "server",
            "servers",
            "ssh",
            "add server",
            "connect server",
            "contabo",
            "cpu",
            "ram",
            "disk",
            "health",
            "stats",
        ),
        when_to_use=(
            "The user wants to add a server, check server health/stats, or mentions "
            "SSH, hosting, or infrastructure."
        ),
        dashboard="/connections",
        cli="jev servers",
        action_hint="Open Connections → Servers → Add server",
    ),
    Skill(
        name="api-keys",
        title="Set up API keys",
        description=(
            "Add external provider API keys (OpenRouter, Anthropic, OpenAI, TypeSafe) "
            "so Baton's workers and Jev decisions can run."
        ),
        keywords=(
            "api key",
            "apikey",
            "api-key",
            "openrouter",
            "anthropic",
            "openai",
            "typesafe",
            "key",
            "token",
            "credentials",
            "set up",
        ),
        when_to_use=(
            "The user asks where to add API keys, mentions a provider (OpenRouter, "
            "Anthropic, OpenAI, TypeSafe), or hits 'no model'/'not configured' errors."
        ),
        dashboard="/connections",
        cli="jev config",
        action_hint="Open Connections → API keys → Add key for the provider",
    ),
    Skill(
        name="provider-auth",
        title="Sign in with Claude / ChatGPT",
        description=(
            "One-click OAuth-style sign-in for the Claude Code and Codex CLIs "
            "(subscription login), or paste a token manually."
        ),
        keywords=(
            "sign in",
            "signin",
            "sign-in",
            "oauth",
            "login",
            "log in",
            "claude code",
            "codex",
            "chatgpt",
            "subscription",
        ),
        when_to_use=(
            "The user wants to connect their Claude or ChatGPT subscription, signs "
            "in, or asks about OAuth/login for providers."
        ),
        dashboard="/connections",
        cli="claude setup-token",
        action_hint="Open Connections → Providers → Sign in with Claude / ChatGPT",
    ),
    Skill(
        name="learning",
        title="Learn from past runs",
        description=(
            "Review planner lessons learned from previous runs — what worked, "
            "what struggled — and tune future decisions."
        ),
        keywords=(
            "learning",
            "lessons",
            "what worked",
            "past runs",
            "history",
            "improve",
            "retrospective",
        ),
        when_to_use=(
            "The user asks what Baton learned, wants a retrospective, or wants to "
            "improve future runs based on history."
        ),
        dashboard="/learning",
        cli="jev learning",
        action_hint="Open the Learning page, or run `jev learning`",
    ),
    Skill(
        name="chat-help",
        title="Chat with Baton",
        description=(
            "General conversation with Baton — ask questions, brainstorm, get help "
            "using any feature."
        ),
        keywords=(
            "help",
            "how do i",
            "how to",
            "what can you do",
            "question",
            "explain",
        ),
        when_to_use=(
            "The user asks for help, asks what Baton can do, or has a general "
            "question not covered by a more specific skill."
        ),
        dashboard="/chats",
        cli="jev chat",
        action_hint="Just ask — describe what you want to do",
    ),
)

# Minimum score for a skill to be reported; tuned so generic chatter ("hello",
# "thanks") matches nothing while real intents match. One single-word hit scores
# 1/3 (see _score), so the bar sits below that; a lone common word is the weakest
# real signal this pass reports.
_MIN_SCORE = 0.25
# Cap on reported matches: the UI shows a short list, not a dump.
_MAX_MATCHES = 3
# Evidence weights: a multi-word phrase is a strong intent signal, a single word
# a weak one. Scores are absolute (evidence in the message), not divided by how
# many keywords a skill lists — the old sqrt(len(keywords)) divisor made a single
# word hit score at most 0.6/sqrt(6) ≈ 0.24, below the bar, so single-keyword
# discovery could never match at all (issue #223).
_PHRASE_WEIGHT = 2.0
_WORD_WEIGHT = 1.0
# Raw evidence that saturates the score at 1.0: one phrase + one word, or three
# single words, is a certain match.
_FULL_SCORE_RAW = 3.0

# Tokens that negate a keyword occurrence ("I don't want a design system" must
# not match design-new). Checked in the tokens just before the occurrence; the
# "why not" and "not only" idioms are suggestions, not negations.
_NEGATORS = frozenset(
    {
        "no",
        "not",
        "never",
        "without",
        "don't",
        "doesn't",
        "didn't",
        "can't",
        "won't",
        "isn't",
        "aren't",
        "dont",
        "doesnt",
        "didnt",
        "cant",
        "wont",
    }
)
# How far back from a keyword occurrence a negator may sit and still negate it:
# "don't want a design system" (3 back) yes, "not sure how to design" (4 back) no.
_NEGATION_WINDOW = 3


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens, in order. Apostrophes stay attached ("don't" is one
    token, so the negation pass sees it); hyphens split ("design-system" reads as
    the phrase "design system")."""
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def _keyword_tokens(keyword: str) -> tuple[str, ...]:
    """A keyword as a token tuple, so "design system" and "design-system" are the
    same trigger and count once."""
    return tuple(_tokens(keyword))


def _find_occurrences(tokens: list[str], phrase: tuple[str, ...]) -> list[int]:
    """Start indexes where `phrase` occurs as consecutive tokens."""
    width = len(phrase)
    return [
        i
        for i in range(len(tokens) - width + 1)
        if tuple(tokens[i : i + width]) == phrase
    ]


def _is_negated(tokens: list[str], start: int) -> bool:
    """Whether a negator sits in the window just before `start`.

    "why not" ("why not design a theme?") and "not only" are suggestions, not
    negations, so a negator preceded by "why" or followed by "only" does not count.
    """
    for j in range(max(0, start - _NEGATION_WINDOW), start):
        if tokens[j] not in _NEGATORS:
            continue
        if j > 0 and tokens[j - 1] == "why":
            continue
        if j + 1 < len(tokens) and tokens[j + 1] == "only":
            continue
        return True
    return False


def _score(skill: Skill, tokens: list[str]) -> float:
    """0..1 match strength for `skill` against the message tokens.

    Each distinct keyword trigger counts once (so "design system" + "design-system"
    do not double-count the same occurrence, and a repeated word does not inflate
    the score); phrases count double, single words single; negated occurrences do
    not count. A single word hit scores 1/3, a single phrase 2/3.
    """
    raw = 0.0
    seen: set[tuple[str, ...]] = set()
    for keyword in skill.keywords:
        phrase = _keyword_tokens(keyword)
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        weight = _PHRASE_WEIGHT if len(phrase) > 1 else _WORD_WEIGHT
        if any(
            not _is_negated(tokens, start)
            for start in _find_occurrences(tokens, phrase)
        ):
            raw += weight
    return min(1.0, raw / _FULL_SCORE_RAW)


def find_skills(message: str, *, limit: int = _MAX_MATCHES) -> list[SkillMatch]:
    """Best-matching skills for `message`, strongest first.

    Scores each skill by keyword evidence: multi-word phrases count more than
    single words, a lone keyword is enough to match, and a negated keyword
    ("don't want a design system") does not count. Returns at most `limit`
    matches above `_MIN_SCORE`; generic messages return [].
    """
    tokens = _tokens(message)
    scored = [
        SkillMatch(skill=skill, score=round(score, 2))
        for skill in SKILLS
        if (score := _score(skill, tokens)) >= _MIN_SCORE
    ]
    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[:limit]


def skill_by_name(name: str) -> Skill | None:
    """Look up a skill by its stable id."""
    return next((s for s in SKILLS if s.name == name), None)


def describe_for_prompt(matches: list[SkillMatch]) -> str:
    """Render matched skills as model context: what Baton can do right now."""
    if not matches:
        return ""
    lines = ["Relevant Baton capabilities for this message:"]
    for m in matches:
        s = m.skill
        where = s.dashboard or s.cli or ""
        lines.append(f"- {s.title}: {s.description} (use: {s.action_hint}; {where})")
    return "\n".join(lines)

"""Badge CSS coverage (micro-audit E1) plus badge pulse behavior (E3/E4).

Rule going forward: every string that can reach the ``badge()`` macro as the
CSS class (the explicit ``cls`` argument, or the text itself when ``cls`` is
omitted) must have a matching ``.s-<name>`` rule in ``static/style.css``.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from jev_agent.app.jobs import JobState
from jev_agent.cli.doctor_cmd import Status as DoctorStatus
from jev_agent.core.models import RunState, TaskState
from jev_agent.core.store import QueueStatus

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = ROOT / "src" / "jev_agent" / "dashboard" / "templates"
CSS = (ROOT / "src" / "jev_agent" / "dashboard" / "static" / "style.css").read_text(
    encoding="utf-8"
)

CSS_BADGE_CLASSES = set(re.findall(r"\.s-([A-Za-z0-9_-]+)", CSS))

# Dynamic badge texts (no literal first argument): expression -> every value it
# can take. Sources are the backend types so the test fails if a new status is
# added without a CSS mapping.
TEXT_DOMAINS: dict[str, tuple[str, ...]] = {
    "r.status": typing.get_args(QueueStatus),  # queue.html queue-item rows
    "r.state": tuple(s.value for s in RunState),  # spend.html run rows
    "job.state": typing.get_args(JobState),  # _run_live.html dashboard jobs
    "run.state": tuple(s.value for s in RunState),  # state_badge(run.state)
    "t.state": tuple(s.value for s in TaskState),  # state_badge(t.state)
    # jev_agent/app/settings.py: Entity.source is "repo" | "user".
    "en.source": ("repo", "user"),
    "c.status": typing.get_args(DoctorStatus),  # settings doctor checks
}

_LITERAL = re.compile(r"""^"[^"]*"$|^'[^']*'$""")
_COMPARE_LIT = re.compile(r"""(?:==|!=)\s*("[^"]*"|'[^']*')""")


def _balanced_calls(text: str, name: str) -> list[str]:
    """Return the raw argument strings of every ``name(...)`` call in text."""
    out: list[str] = []
    for m in re.finditer(r"(?<![\w.])" + re.escape(name) + r"\(", text):
        i, depth = m.end(), 1
        in_str: str | None = None
        while depth and i < len(text):
            ch = text[i]
            if in_str:
                if ch == in_str:
                    in_str = None
            elif ch in "\"'":
                in_str = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        out.append(text[m.end() : i - 1])
    return out


def _split_args(argstr: str) -> list[str]:
    args, cur, depth, in_str = [], [], 0, None
    for ch in argstr:
        if in_str:
            cur.append(ch)
            if ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str, cur = ch, [*cur, ch]
        elif ch == "(":
            depth, cur = depth + 1, [*cur, ch]
        elif ch == ")":
            depth, cur = depth - 1, [*cur, ch]
        elif ch == "," and depth == 0:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur))
    return [a.strip() for a in args]


def _literal(value: str) -> str | None:
    # A single quoted string only — a ternary like `"ok" if x else "warn"`
    # is an expression, not a literal.
    return value[1:-1] if _LITERAL.match(value) else None


def _expression_literals(expr: str) -> set[str]:
    """String literals a ``cls`` expression can produce.

    Comparison literals (``s.access == "basic"``) are conditions, not results,
    so they are stripped first.
    """
    stripped = _COMPARE_LIT.sub("", expr)
    return {m.group(0)[1:-1] for m in re.finditer(r""""[^"]*"|'[^']*'""", stripped)}


def _needed_classes() -> dict[str, list[str]]:
    """Map CSS class suffix -> list of 'template: call' needing it."""
    needed: dict[str, list[str]] = {}
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        # Skip the macro definitions themselves in macros.html — only calls.
        # (Both the {% macro %} tag and the body, e.g. {{ badge(text) }}.)
        text = re.sub(
            r"{%\s*macro\s+(?:badge|state_badge)\b.*?{%\s*endmacro\s*%}",
            "",
            text,
            flags=re.DOTALL,
        )
        for macro in ("badge", "state_badge"):
            for call in _balanced_calls(text, macro):
                args = _split_args(call)
                text_arg = args[0]
                cls_arg: str | None = None
                for extra in args[1:]:
                    if re.match(r"^[A-Za-z_]\w*\s*=", extra):
                        continue  # kwarg (e.g. live=true), not the class
                    cls_arg = extra
                    break
                text_lit = _literal(text_arg)
                where = f"{path.name}: {macro}({call})"

                if cls_arg is not None:
                    cls_lit = _literal(cls_arg)
                    if cls_lit is not None:
                        needed.setdefault(cls_lit, []).append(where)
                    else:
                        # Expression (e.g. a ternary): every literal it can
                        # produce is a possible class.
                        for lit in _expression_literals(cls_arg):
                            needed.setdefault(lit, []).append(where)
                elif text_lit is not None:
                    needed.setdefault(text_lit, []).append(where)
                else:
                    domain = TEXT_DOMAINS.get(text_arg)
                    assert domain is not None, (
                        f"{where}: dynamic badge text {text_arg!r} has no "
                        "registered value domain — add it to TEXT_DOMAINS"
                    )
                    for value in domain:
                        needed.setdefault(value, []).append(where)
    return needed


def test_every_badge_class_has_css_mapping() -> None:
    needed = _needed_classes()
    assert needed, "no badge() calls found — the extractor is broken"
    missing = {
        name: sorted(set(uses))
        for name, uses in sorted(needed.items())
        if name not in CSS_BADGE_CLASSES
    }
    assert not missing, "badge() texts with no .s-<name> rule in static/style.css:\n" + "\n".join(
        f"  .s-{name}: {', '.join(uses)}" for name, uses in missing.items()
    )


def _badge_html() -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATES))
    return env.from_string(
        '{% from "macros.html" import badge, state_badge %}'
        '{{ badge("running") }}|{{ badge("RUNNING") }}|'
        '{{ badge("enabled") }}|{{ badge("paused", "pending") }}|'
        '{{ badge("not running", "pending") }}|'
        '{{ state_badge("RUNNING") }}|{{ state_badge("done") }}'
    ).render()


def test_badge_pulse_is_decided_by_status_value() -> None:
    # E3: active states pulse whether or not the caller passes live=true;
    # mere states (enabled/paused) never do.
    parts = _badge_html().split("|")
    assert 'class="badge live s-running"' in parts[0]
    assert 'class="badge live s-RUNNING"' in parts[1]
    assert 'class="badge s-enabled"' in parts[2]
    assert 'class="badge s-pending">paused' in parts[3]
    assert 'class="badge s-pending">not running' in parts[4]


def test_state_badge_live_check_is_case_insensitive() -> None:
    # E4: lowercase states pulse just like their uppercase twins.
    parts = _badge_html().split("|")
    assert 'class="badge live s-RUNNING"' in parts[5]
    assert 'class="badge s-done"' in parts[6]

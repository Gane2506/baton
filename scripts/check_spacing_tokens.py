"""Fail on literal spacing values in the dashboard UI (issue #332).

Every ``gap`` / ``margin`` / ``padding`` value in the dashboard stylesheets
must resolve to the single spacing scale defined once in
``src/jev_agent/dashboard/static/style.css`` (``:root``)::

    --s0: 2px; --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 20px;
    --s6: 24px; --s7: 32px; --s8: 48px; --s9: 64px; --hairline: 1px;

New spacing snaps to the nearest step; if no step fits, extend the scale
there — never add a literal at the use site. The per-theme density
overrides (``--ui-gap`` / ``--ui-pad`` in ``ui-styles.css``) must likewise
resolve to the scale, not to literals.

What this checks (stdlib only, no dependency sync — it runs in the
Principles workflow):

- ``src/jev_agent/dashboard/static/*.css``: no literal length (``Npx`` /
  ``Nrem`` / ``Nem`` / ``N%``) inside a ``gap`` / ``row-gap`` /
  ``column-gap`` / ``margin*`` / ``padding*`` / ``scroll-margin*``
  declaration, including literals hiding inside ``var()`` fallbacks and
  ``calc()``. Bare ``0`` and ``auto`` are fine; ``var(--s*)``,
  ``var(--hairline)``, ``var(--ui-gap)`` and ``var(--ui-pad)`` are fine.
  CSS comments are ignored.
- Every ``--s*`` / ``--hairline`` token referenced by a spacing value is
  actually defined in the dashboard stylesheets (a typo must fail loudly,
  not silently drop the spacing).
- ``src/jev_agent/dashboard/templates/*.html``: no inline ``style=""``
  attribute carrying a literal spacing value (the CSP forbids inline
  styles anyway — this keeps it that way).
- ``src/jev_agent/dashboard/static/*.js``: no ``element.style.<gap|margin|
  padding>`` assignment with a literal value.

Deliberately out of scope (not spacing): ``border`` widths, ``border-radius``,
``font-size`` / ``line-height``, ``width`` / ``height`` / ``max-width``,
``grid-template-columns`` / ``flex-basis``, and positioning (``top`` /
``bottom`` / ``background-position``) — those have their own reasons to
carry lengths. If a future spacing value genuinely cannot use the scale,
add it to ALLOWLIST below with a one-line justification, in the same
commit as the code that needs it.

Usage: ``python3 scripts/check_spacing_tokens.py`` — exit 0 when clean.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
STATIC = REPO / "src/jev_agent/dashboard/static"
TEMPLATES = REPO / "src/jev_agent/dashboard/templates"
STYLE_CSS = STATIC / "style.css"

# file:line -> justification. Empty: nothing in the dashboard needs an
# exception today. Additions need a one-line justification here, in the
# same commit as the code that needs the exception.
ALLOWLIST: dict[str, str] = {}

COMMENT = re.compile(r"/\*.*?\*/", re.S)
# A spacing declaration: property, colon, value up to ; or }.
SPACING_PROP = re.compile(
    r"(gap|row-gap|column-gap|margin(?:-[a-z-]+)?|padding(?:-[a-z-]+)?"
    r"|scroll-margin(?:-[a-z-]+)?|--ui-gap|--ui-pad)(\s*:\s*)([^;}{]+)"
)
LITERAL = re.compile(r"-?\d+(?:\.\d+)?(?:px|rem|em|%)")
TOKEN_REF = re.compile(r"var\(\s*(--[a-z0-9-]+)")
TOKEN_DEF = re.compile(r"(--[a-z0-9-]+)\s*:")
INLINE_STYLE = re.compile(r"""style\s*=\s*"([^"]*)"|style\s*=\s*'([^']*)'""")
JS_STYLE_SPACING = re.compile(r"\.style\s*\.\s*(gap|margin\w*|padding\w*)\s*=")


def strip_comments(text: str) -> str:
    return COMMENT.sub(lambda m: "".join("\n" if c == "\n" else " " for c in m.group(0)), text)


def check_css(path: pathlib.Path, defined: set[str]) -> list[str]:
    failures: list[str] = []
    text = strip_comments(path.read_text(encoding="utf-8"))
    for match in SPACING_PROP.finditer(text):
        lineno = text.count("\n", 0, match.start()) + 1
        key = f"{path.name}:{lineno}"
        if key in ALLOWLIST:
            continue
        value = match.group(3)
        for lit in LITERAL.finditer(value):
            failures.append(
                f"{path}:{lineno}: literal spacing {lit.group()!r} in "
                f"`{match.group(1)}: {value.strip()}` — use the --s* scale "
                f"(or extend it in style.css :root)"
            )
        for ref in TOKEN_REF.finditer(value):
            token = ref.group(1)
            if token not in defined:
                failures.append(
                    f"{path}:{lineno}: spacing references undefined token "
                    f"{token!r} in `{match.group(1)}: {value.strip()}`"
                )
    return failures


def check_templates() -> list[str]:
    failures: list[str] = []
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in INLINE_STYLE.finditer(text):
            style = match.group(1) if match.group(1) is not None else match.group(2)
            lineno = text.count("\n", 0, match.start()) + 1
            for prop in SPACING_PROP.finditer(style):
                if LITERAL.search(prop.group(3)):
                    failures.append(
                        f"{path}:{lineno}: literal spacing in inline style "
                        f"`{prop.group(1)}: {prop.group(3).strip()}` — "
                        f"move it to a stylesheet token"
                    )
    return failures


def check_js() -> list[str]:
    failures: list[str] = []
    for path in sorted(STATIC.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for match in JS_STYLE_SPACING.finditer(text):
            line = text[max(0, text.rfind("\n", 0, match.start()) + 1) :]
            line = line.split("\n", 1)[0]
            lineno = text.count("\n", 0, match.start()) + 1
            if LITERAL.search(line.split("=", 1)[1] if "=" in line else line):
                failures.append(
                    f"{path}:{lineno}: JS sets spacing from a literal "
                    f"(`{line.strip()}`) — use a stylesheet token"
                )
    return failures


def main() -> int:
    # A spacing value may reference any custom property defined in the
    # dashboard stylesheets (the --s* scale lives in style.css :root;
    # layout tokens such as --submenu-w live there too). A reference to a
    # token defined nowhere is a typo that would silently drop the spacing.
    defined: set[str] = set()
    for css in STATIC.glob("*.css"):
        defined.update(TOKEN_DEF.findall(css.read_text(encoding="utf-8")))
    failures: list[str] = []
    for path in sorted(STATIC.glob("*.css")):
        failures.extend(check_css(path, defined))
    failures.extend(check_templates())
    failures.extend(check_js())
    if failures:
        print(f"spacing tokens: {len(failures)} violation(s)", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    scale = sorted(t for t in defined if re.fullmatch(r"--s\d+|--hairline", t))
    print(
        f"spacing tokens: ok — no literal gap/margin/padding; "
        f"scale tokens defined ({', '.join(scale)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

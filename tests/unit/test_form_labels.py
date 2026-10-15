"""Settings form labels (micro-audit B1/B2).

B1: boolean dropdowns render "On"/"Off" (tristate adds "Use default") while
    still submitting "true"/"false".
B2: the six historical "use the default" labels are unified to "Use default".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = ROOT / "src" / "jev_agent" / "dashboard" / "templates"

_ENV = Environment(loader=FileSystemLoader(TEMPLATES))
_FIELD_INPUT = _ENV.from_string(
    '{% from "settings.html" import field_input %}{{ field_input(f, "f1") }}'
)


def _render(kind: str, text: str | None, choices: tuple[str, ...] = ()) -> str:
    f = SimpleNamespace(path="x.y", kind=kind, text=text, choices=list(choices))
    return _FIELD_INPUT.render(f=f)


def test_bool_renders_on_off_but_submits_true_false() -> None:
    html = _render("bool", "true")
    assert ">On<" in html and ">Off<" in html
    assert ">true<" not in html and ">false<" not in html
    # The submitted values are unchanged.
    assert 'value="true"' in html and 'value="false"' in html


def test_tristate_renders_use_default_on_off() -> None:
    html = _render("tristate", None)
    assert ">Use default<" in html and ">On<" in html and ">Off<" in html
    assert ">true<" not in html and ">false<" not in html
    assert "default (unset)" not in html
    assert 'value="unset"' in html
    assert 'title="from jev.toml"' in html


RETIRED_DEFAULT_LABELS = (
    "repo setting",
    "config level",
    "config setting",
    "repo default",
    "default (unset)",
    "(default)",
)

UNIFIED_FILES = ("queue.html", "board.html", "chats.html", "settings.html")


def test_use_default_labels_are_unified() -> None:
    for name in UNIFIED_FILES:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        for label in RETIRED_DEFAULT_LABELS:
            assert label not in text, f"{name} still uses {label!r}"
        assert "Use default" in text, f"{name} lost its Use default option"


def test_use_default_options_carry_the_source_tooltip() -> None:
    # B2: where the default comes from the repo config, the tooltip says so.
    for name in ("queue.html", "board.html", "chats.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'title="from jev.toml">Use default<' in text, name

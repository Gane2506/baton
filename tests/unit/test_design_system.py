"""The design system store (`core/design_system.py`), the `jev design` CLI and the
designer role: round-trip, versioning, prompt rendering and corrupt-file tolerance."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app
from jev_agent.core.design_system import (
    STARTER_MARKDOWN,
    STARTER_TOKENS,
    DesignSystem,
    design_context_for_coder,
    get_design_system,
    list_versions,
    render_for_prompt,
    save_design_system,
)
from jev_agent.core.models import DesignDoc, Plan, RunState, ScreenSpec, TaskCard
from jev_agent.roles.render import render_prompt
from jev_agent.roles.schemas import ROLE_SCHEMAS

runner = CliRunner()

CONFIG = '[dispatcher]\nprimary = "rules"\n'


@pytest.fixture
def repo(fixture_repo: Path) -> Path:
    (fixture_repo / "jev.toml").write_text(CONFIG)
    return fixture_repo


def _jev(repo: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--repo", str(repo)])
    return result.exit_code, " ".join(re.sub(r"[─-╿]", " ", result.output).split())


# ---- store round-trip ----------------------------------------------------------------------


def test_save_and_get_round_trip(repo: Path) -> None:
    version = save_design_system(repo, "# System\n", {"colors": {"primary": "#000"}})
    assert version == 1
    system = get_design_system(repo)
    assert system is not None
    assert system.version == 1
    assert system.markdown == "# System\n"
    assert system.tokens == {"colors": {"primary": "#000"}}
    assert system.updated_at  # ISO timestamp written


def test_regenerate_versions_and_keeps_old(repo: Path) -> None:
    v1 = save_design_system(repo, "# v1\n", {"colors": {"a": "#111"}})
    v2 = save_design_system(repo, "# v2\n", {"colors": {"b": "#222"}})
    assert (v1, v2) == (1, 2)
    assert list_versions(repo) == [1, 2]
    d = repo / ".jev" / "design"
    assert (d / "design-system.v1.md").read_text() == "# v1\n"
    assert json.loads((d / "tokens.v1.json").read_text()) == {"colors": {"a": "#111"}}
    assert (d / "design-system.md").read_text() == "# v2\n"
    v3 = save_design_system(repo, "# v3\n", {"colors": {"c": "#333"}})
    assert v3 == 3
    assert list_versions(repo) == [1, 2, 3]
    assert (d / "design-system.v2.md").read_text() == "# v2\n"


def test_no_system_gives_none_and_empty_versions(repo: Path) -> None:
    assert get_design_system(repo) is None
    assert list_versions(repo) == []


# ---- corrupt files never raise --------------------------------------------------------------


def test_corrupt_files_return_none_not_raise(repo: Path) -> None:
    d = repo / ".jev" / "design"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{not json", encoding="utf-8")
    (d / "design-system.md").write_text("# ok\n", encoding="utf-8")
    (d / "tokens.json").write_text("also not json", encoding="utf-8")
    with pytest.warns(UserWarning):
        assert get_design_system(repo) is None
    assert list_versions(repo) == []  # no crash either


def test_missing_tokens_json_returns_none(repo: Path) -> None:
    d = repo / ".jev" / "design"
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"version": 1, "updated_at": "x"}', encoding="utf-8")
    (d / "design-system.md").write_text("# ok\n", encoding="utf-8")
    with pytest.warns(UserWarning):
        assert get_design_system(repo) is None


# ---- rendering -------------------------------------------------------------------------------


def test_render_for_prompt_compact_and_empty() -> None:
    assert render_for_prompt(None) == ""
    system = DesignSystem(
        version=2,
        updated_at="2026-10-15T00:00:00+00:00",
        markdown="# System\n\n" + ("x" * 5000),
        tokens={
            "colors": {"primary": "#2563EB"},
            "type_scale": {"base": 15},
            "spacing": {"md": 16},
            "radii": {"md": 10},
        },
    )
    text = render_for_prompt(system)
    assert "Design system v2" in text
    assert "primary #2563EB" in text
    assert "base 15" in text
    assert len(text) < 3000  # the markdown is capped


def test_design_context_for_coder() -> None:
    assert design_context_for_coder(None) == ""
    doc = DesignDoc(
        title="Chat UI",
        summary="Fullscreen chat with a rail.",
        screens=[ScreenSpec(name="Chat", description="The thread.", components=["Button"])],
        notes=["propose system: add Banner"],
    )
    text = design_context_for_coder(doc)
    assert "Chat UI" in text and "Chat" in text and "Button" in text and "Banner" in text


# ---- CLI --------------------------------------------------------------------------------------


def test_cli_show_without_system(repo: Path) -> None:
    code, out = _jev(repo, "design", "show")
    assert code == 0 and "no design system yet" in out.lower(), out


def test_cli_init_then_show(repo: Path) -> None:
    code, out = _jev(repo, "design", "init")
    assert code == 0 and "v1" in out, out
    system = get_design_system(repo)
    assert system is not None and system.version == 1
    colors = system.tokens["colors"]
    assert isinstance(colors, dict) and "primary" in colors
    assert "Screen specs (generated)" in system.markdown
    code, out = _jev(repo, "design", "show")
    assert code == 0 and "Design system v1" in out, out
    # init again refuses unless forced
    code, out = _jev(repo, "design", "init")
    assert code == 1 and "--force" in out, out
    code, out = _jev(repo, "design", "init", "--force")
    assert code == 0 and "v2" in out, out  # forced replace still versions the old one
    assert list_versions(repo) == [1, 2]


def test_starter_template_is_not_empty() -> None:
    assert len(STARTER_MARKDOWN.strip()) > 200
    assert set(STARTER_TOKENS) >= {"colors", "type_scale", "spacing", "radii"}


# ---- designer role -------------------------------------------------------------------------------


def _prompt_vars(design_system: str | None) -> dict[str, Any]:
    return {
        "idea": "a dashboard",
        "plan_json": None,
        "design_system": design_system,
        "brief": "a dashboard",
    }


def test_designer_prompt_renders_without_system() -> None:
    text = render_prompt("designer", **_prompt_vars(None))
    assert "No design system yet" in text
    assert '"screens"' in text and '"tokens_ref"' in text


def test_designer_prompt_renders_with_system() -> None:
    system = DesignSystem(
        version=3,
        updated_at="2026-10-15T00:00:00+00:00",
        markdown="# System\nUse the primary color.",
        tokens={"colors": {"primary": "#2563EB"}},
    )
    text = render_prompt("designer", **_prompt_vars(render_for_prompt(system)))
    assert "Current design system" in text
    assert "Design system v3" in text
    assert "primary #2563EB" in text


def test_role_schemas_parses_design_doc() -> None:
    schema = ROLE_SCHEMAS["designer"]
    doc = schema.model_validate(
        {
            "title": "Chat UI",
            "summary": "s",
            "screens": [{"name": "Chat", "components": ["Button"]}],
            "tokens_ref": "v3",
            "notes": ["n1"],
            "version": 2,
        }
    )
    assert isinstance(doc, DesignDoc)
    assert doc.screens[0].name == "Chat" and doc.tokens_ref == "v3"


# ---- models --------------------------------------------------------------------------------------


def test_plan_needs_design_defaults_false() -> None:
    plan = Plan(
        summary="s",
        tasks=[TaskCard(id="T1", title="t", description="d")],
    )
    assert plan.needs_design is False
    plan2 = plan.model_copy(update={"needs_design": True})
    assert Plan.model_validate(plan2.model_dump()).needs_design is True


def test_new_run_states_exist() -> None:
    assert RunState.DESIGNING.value == "DESIGNING"
    assert RunState.AWAITING_DESIGNING.value == "AWAITING_DESIGNING"

"""`jev design figma ...`: export, edit, import and sync from the CLI.

Export and edit generate `use_figma` Plugin API code — no network, no
token. Import and sync need a token and a Figma file, so they fail plainly
without one (the network path is covered by the dashboard tests' fake)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app
from jev_agent.core.design_library import save_library_system

runner = CliRunner()

TOKENS = {
    "colors": {"primary": "#111111"},
    "type_scale": {"base": 15},
    "spacing": {"md": 16},
    "radii": {"md": 8},
}


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at a temp dir so the library never touches the real ~/.jev."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _seed(name: str = "acme") -> None:
    save_library_system(name, "# Acme\n", dict(TOKENS), {"name": name, "source": "wizard"})


def test_figma_export_prints_the_script(home: Path) -> None:
    _seed()
    result = runner.invoke(app, ["design", "figma", "export", "acme"])
    assert result.exit_code == 0, result.output
    assert "createVariableCollection" in result.output
    assert "Baton / acme" in result.output
    assert "setReactionsAsync" in result.output
    assert "SMART_ANIMATE" in result.output


def test_figma_export_writes_to_a_file(home: Path, tmp_path: Path) -> None:
    _seed()
    out = tmp_path / "acme.figma.js"
    result = runner.invoke(app, ["design", "figma", "export", "acme", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "createVariableCollection" in out.read_text(encoding="utf-8")


def test_figma_export_missing_system_fails(home: Path) -> None:
    result = runner.invoke(app, ["design", "figma", "export", "nope"])
    assert result.exit_code == 1
    assert "no design system" in result.output


def test_figma_edit_prints_one_snippet(home: Path) -> None:
    result = runner.invoke(
        app,
        [
            "design",
            "figma",
            "edit",
            "variable",
            "--collection",
            "Baton / acme",
            "--name",
            "color/primary",
            "--mode",
            "Dark",
            "--value",
            "#1D4ED8",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "setValueForMode" in result.output
    assert "get_screenshot" in result.output


def test_figma_edit_rejects_a_bad_edit(home: Path) -> None:
    result = runner.invoke(
        app, ["design", "figma", "edit", "node-fill", "--node-id", "nope", "--value", "#ff0000"]
    )
    assert result.exit_code == 1
    assert "bad Figma node id" in result.output


def test_figma_import_without_token_fails(home: Path) -> None:
    result = runner.invoke(
        app, ["design", "figma", "import", "https://www.figma.com/design/abc123/X"]
    )
    assert result.exit_code == 1
    assert "no Figma token" in result.output


def test_figma_sync_unlinked_system_fails(home: Path) -> None:
    _seed()
    result = runner.invoke(app, ["design", "figma", "sync", "acme"])
    assert result.exit_code == 1
    assert "not linked to a Figma file" in result.output

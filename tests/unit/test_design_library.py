"""The user-level design system library (`core/design_library.py`) and the
`jev design list|use|clone|delete|export` commands: name validation, CRUD
round-trip, versioning, 0700 permissions, corrupt-file tolerance, the three
token export formats, and `use` recording its source."""

import json
import os
import re
import stat
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jev_agent.cli.app import app
from jev_agent.core.design_library import (
    delete_library_system,
    export_tokens_css,
    export_tokens_tailwind,
    export_tokens_w3c,
    library_dir,
    library_system_path,
    list_library,
    load_library_system,
    save_library_system,
    validate_library_name,
)
from jev_agent.core.design_system import get_design_system

runner = CliRunner()

CONFIG = '[dispatcher]\nprimary = "rules"\n'

TOKENS: dict[str, object] = {
    "colors": {"primary": "#2563EB", "background": "#FFFFFF"},
    "type_scale": {"xs": 12, "base": 15},
    "spacing": {"sm": 8, "md": 16},
    "radii": {"sm": 6, "full": 9999},
}

MARKDOWN = "# Library system\n\nA reusable design system.\n"


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at a temp dir so the library never touches the real ~/.jev."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def repo(fixture_repo: Path) -> Path:
    (fixture_repo / "jev.toml").write_text(CONFIG)
    return fixture_repo


def _meta(home: Path, name: str) -> dict[str, object]:
    raw = json.loads((home / ".jev" / "design-systems" / name / "meta.json").read_text())
    assert isinstance(raw, dict)
    return {str(key): value for key, value in raw.items()}


def _w3c_node(w3c: dict[str, object], group: str, key: str) -> dict[str, object]:
    section = w3c[group]
    assert isinstance(section, dict)
    node = section[key]
    assert isinstance(node, dict)
    return node


# ---- name validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["chat-ui", "A1_b-2", "x", "a" * 64, "DesignSystem9", "trailing-"],
)
def test_valid_names(home: Path, name: str) -> None:
    assert validate_library_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "has space", "../evil", "..", ".", "-leading", "a/b", "café", "a" * 65],
)
def test_invalid_names_raise(home: Path, name: str) -> None:
    with pytest.raises(ValueError):
        validate_library_name(name)


def test_validated_name_cannot_escape_library(home: Path) -> None:
    path = library_system_path("chat-ui")
    assert path.parent == library_dir()
    assert ".." not in str(path.relative_to(library_dir()))


# ---- CRUD round-trip -----------------------------------------------------------------------------


def test_save_and_load_round_trip(home: Path) -> None:
    version = save_library_system(
        "chat-ui", MARKDOWN, dict(TOKENS), {"name": "chat-ui", "app_type": "web", "stack": "react"}
    )
    assert version == 1
    system = load_library_system("chat-ui")
    assert system is not None
    assert system.version == 1
    assert system.markdown == MARKDOWN
    assert system.tokens == TOKENS
    assert system.updated_at
    meta = _meta(home, "chat-ui")
    assert meta["version"] == 1
    assert meta["name"] == "chat-ui"
    assert meta["app_type"] == "web"
    assert meta["stack"] == "react"
    assert meta["updated_at"]


def test_meta_extra_cannot_clobber_version(home: Path) -> None:
    save_library_system("x", MARKDOWN, {}, {"version": 99, "updated_at": "bogus"})
    assert _meta(home, "x")["version"] == 1
    assert _meta(home, "x")["updated_at"] != "bogus"


def test_load_missing_returns_none(home: Path) -> None:
    assert load_library_system("nope") is None


def test_list_library_sorted(home: Path) -> None:
    save_library_system("zeta", MARKDOWN, {})
    save_library_system("alpha", MARKDOWN, {})
    (library_dir() / "not-a-system").mkdir()  # a dir without meta.json is not listed
    assert list_library() == ["alpha", "zeta"]


def test_delete(home: Path) -> None:
    assert delete_library_system("missing") is False
    save_library_system("gone", MARKDOWN, {})
    assert delete_library_system("gone") is True
    assert delete_library_system("gone") is False
    assert list_library() == []


# ---- versioning ----------------------------------------------------------------------------------


def test_regenerate_versions_and_keeps_old(home: Path) -> None:
    assert save_library_system("chat-ui", "# v1\n", {"colors": {"a": "#111"}}) == 1
    assert save_library_system("chat-ui", "# v2\n", {"colors": {"b": "#222"}}) == 2
    d = library_system_path("chat-ui")
    assert (d / "design-system.v1.md").read_text() == "# v1\n"
    assert json.loads((d / "tokens.v1.json").read_text()) == {"colors": {"a": "#111"}}
    assert (d / "design-system.md").read_text() == "# v2\n"
    system = load_library_system("chat-ui")
    assert system is not None and system.version == 2
    assert _meta(home, "chat-ui")["version"] == 2


# ---- permissions ---------------------------------------------------------------------------------


def test_library_dirs_are_0700(home: Path) -> None:
    assert stat.S_IMODE(os.stat(library_dir()).st_mode) == 0o700
    save_library_system("chat-ui", MARKDOWN, {})
    assert stat.S_IMODE(os.stat(library_system_path("chat-ui")).st_mode) == 0o700


# ---- corrupt files never raise -------------------------------------------------------------------


def test_corrupt_library_files_return_none_not_raise(home: Path) -> None:
    d = library_system_path("broken")
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{not json", encoding="utf-8")
    (d / "design-system.md").write_text("# ok\n", encoding="utf-8")
    (d / "tokens.json").write_text("also not json", encoding="utf-8")
    with pytest.warns(UserWarning):
        assert load_library_system("broken") is None
    # save over the corruption still versions up cleanly from nothing
    with pytest.warns(UserWarning):
        assert save_library_system("broken", MARKDOWN, {}) == 1


def test_missing_tokens_json_returns_none(home: Path) -> None:
    d = library_system_path("partial")
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"version": 1, "updated_at": "x"}', encoding="utf-8")
    (d / "design-system.md").write_text("# ok\n", encoding="utf-8")
    with pytest.warns(UserWarning):
        assert load_library_system("partial") is None


# ---- exports -------------------------------------------------------------------------------------


def test_export_w3c_round_trips_values() -> None:
    w3c = export_tokens_w3c(TOKENS)
    assert _w3c_node(w3c, "color", "primary") == {"$value": "#2563EB", "$type": "color"}
    assert _w3c_node(w3c, "fontSize", "base") == {"$value": 15, "$type": "dimension"}
    assert _w3c_node(w3c, "spacing", "md") == {"$value": 16, "$type": "dimension"}
    assert _w3c_node(w3c, "borderRadius", "full") == {"$value": 9999, "$type": "borderRadius"}
    # $value keeps every token value verbatim: extracting them rebuilds tokens.json
    groups = {
        "color": "colors",
        "fontSize": "type_scale",
        "spacing": "spacing",
        "borderRadius": "radii",
    }
    rebuilt: dict[str, dict[str, object]] = {}
    for group, source in groups.items():
        section = w3c[group]
        assert isinstance(section, dict)
        rebuilt[source] = {
            str(key): node["$value"] for key, node in section.items() if isinstance(node, dict)
        }
    assert rebuilt == TOKENS
    json.dumps(w3c)  # serializes cleanly


def test_export_w3c_skips_non_dict_sections() -> None:
    assert export_tokens_w3c({"colors": "not-a-dict"}) == {
        "color": {},
        "fontSize": {},
        "spacing": {},
        "borderRadius": {},
    }


def test_export_tailwind() -> None:
    text = export_tokens_tailwind(TOKENS)
    assert "theme.extend" in text
    assert "module.exports" in text
    assert '"#2563EB"' in text  # colors verbatim
    assert '"15px"' in text and '"16px"' in text and '"9999px"' in text  # sizes with px units


def test_export_css() -> None:
    text = export_tokens_css(TOKENS)
    assert ":root" in text
    assert "--color-primary: #2563EB;" in text
    assert "--color-background: #FFFFFF;" in text
    assert "--font-size-xs: 12px;" in text
    assert "--font-size-base: 15px;" in text
    assert "--spacing-sm: 8px;" in text
    assert "--radius-sm: 6px;" in text
    assert "--radius-full: 9999px;" in text


def test_export_css_round_trips_through_px_convention() -> None:
    """Every token reappears as a custom property: the px suffix is the only change."""
    text = export_tokens_css(TOKENS)
    recovered: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("--"):
            key, _, value = line.partition(":")
            recovered[key] = value.strip().rstrip(";")
    expected = {
        "--color-primary": "#2563EB",
        "--color-background": "#FFFFFF",
        "--font-size-xs": "12px",
        "--font-size-base": "15px",
        "--spacing-sm": "8px",
        "--spacing-md": "16px",
        "--radius-sm": "6px",
        "--radius-full": "9999px",
    }
    assert recovered == expected


# ---- CLI -----------------------------------------------------------------------------------------


def _jev(home: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def test_cli_list(home: Path) -> None:
    save_library_system("chat-ui", MARKDOWN, {}, {"app_type": "web", "stack": "react"})
    code, out = _jev(home, "design", "list")
    assert code == 0, out
    assert "chat-ui" in out and "web" in out and "react" in out and "app_type" in out


def test_cli_list_empty(home: Path) -> None:
    code, out = _jev(home, "design", "list")
    assert code == 0, out
    assert "app_type" in out  # the table headers still render


def test_cli_use_installs_and_records_source(home: Path, repo: Path) -> None:
    save_library_system("chat-ui", MARKDOWN, dict(TOKENS), {"app_type": "web", "stack": "react"})
    code, out = _jev(home, "design", "use", "chat-ui", "--repo", str(repo))
    assert code == 0, out
    system = get_design_system(repo)
    assert system is not None
    assert system.version == 1
    assert system.markdown == MARKDOWN
    assert system.tokens == TOKENS
    meta = json.loads((repo / ".jev" / "design" / "meta.json").read_text())
    assert meta["source"] == "library:chat-ui"
    assert meta["app_type"] == "web"
    assert meta["stack"] == "react"


def test_cli_use_missing_refuses(home: Path, repo: Path) -> None:
    code, out = _jev(home, "design", "use", "nope", "--repo", str(repo))
    assert code == 1
    assert 'no design system named "nope"' in out
    assert get_design_system(repo) is None


def test_cli_clone(home: Path) -> None:
    save_library_system("chat-ui", MARKDOWN, dict(TOKENS), {"app_type": "web", "stack": "react"})
    code, out = _jev(home, "design", "clone", "chat-ui", "chat-ui-2")
    assert code == 0, out
    clone = load_library_system("chat-ui-2")
    assert clone is not None
    assert clone.version == 1  # fresh version history
    assert clone.markdown == MARKDOWN and clone.tokens == TOKENS
    meta = _meta(home, "chat-ui-2")
    assert meta["source"] == "clone:chat-ui"
    assert meta["app_type"] == "web" and meta["stack"] == "react"
    # cloning again bumps the clone's own history, not the original's
    code, _ = _jev(home, "design", "clone", "chat-ui", "chat-ui-2")
    assert code == 1  # the target already exists


def test_cli_clone_missing_refuses(home: Path) -> None:
    code, out = _jev(home, "design", "clone", "nope", "new-name")
    assert code == 1 and "nope" in out


def test_cli_delete(home: Path) -> None:
    save_library_system("gone", MARKDOWN, {})
    code, out = _jev(home, "design", "delete", "gone", "--yes")
    assert code == 0, out
    assert list_library() == []
    code, out = _jev(home, "design", "delete", "gone", "--yes")
    assert code == 1 and "gone" in out


def test_cli_export_defaults_and_out(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_library_system("chat-ui", MARKDOWN, dict(TOKENS), {})
    monkeypatch.chdir(tmp_path)
    cases = (
        ("w3c", "chat-ui.tokens.json"),
        ("tailwind", "chat-ui.tailwind.js"),
        ("css", "chat-ui.css"),
    )
    for fmt, filename in cases:
        code, out = _jev(home, "design", "export", "chat-ui", "--format", fmt)
        assert code == 0, out
        target = tmp_path / filename
        assert target.exists()
        text = target.read_text()
        if fmt == "w3c":
            parsed = json.loads(text)
            assert parsed["color"]["primary"]["$value"] == "#2563EB"
        elif fmt == "tailwind":
            assert "theme.extend" in text
        else:
            assert ":root" in text
    # explicit --out
    code, out = _jev(home, "design", "export", "chat-ui", "--format", "css", "--out", "custom.css")
    assert code == 0, out
    assert (tmp_path / "custom.css").read_text().startswith(":root")
    # unknown format refuses
    code, out = _jev(home, "design", "export", "chat-ui", "--format", "png")
    assert code == 1 and "unknown format" in out


def test_cli_export_missing_refuses(home: Path) -> None:
    code, out = _jev(home, "design", "export", "nope", "--format", "css")
    assert code == 1 and "nope" in out


# ---- atomicity and write-failure hygiene --------------------------------------------------


def test_save_rename_failure_leaves_old_version_intact_and_no_temps(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the rename-into-place fails, the staged temps are removed and the old
    version's files are untouched (never a torn mix)."""
    save_library_system("chat-ui", "# v1\n", {"colors": {"a": "#111"}})

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save_library_system("chat-ui", "# v2\n", {"colors": {"b": "#222"}})

    d = library_system_path("chat-ui")
    assert not list(d.glob("*.tmp"))  # no staged temps left behind
    assert json.loads((d / "meta.json").read_text())["version"] == 1
    assert (d / "design-system.md").read_text() == "# v1\n"
    assert json.loads((d / "tokens.json").read_text()) == {"colors": {"a": "#111"}}


def test_save_uses_temp_files_next_to_targets(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The temp file lives in the same directory as the target (so `os.replace`
    cannot cross filesystems) and is removed after a successful save."""
    seen: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
        text: bool = False,
    ) -> tuple[int, str]:
        fd, name = real_mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)
        seen.append(Path(str(name)))
        return fd, name

    monkeypatch.setattr("tempfile.mkstemp", spy_mkstemp)
    save_library_system("chat-ui", MARKDOWN, dict(TOKENS))
    d = library_system_path("chat-ui")
    assert seen, "expected temp files to be staged"
    assert all(tmp.parent == d for tmp in seen)
    assert not list(d.glob("*.tmp"))


def test_non_utf8_library_files_return_none_not_raise(home: Path) -> None:
    d = library_system_path("binary")
    d.mkdir(parents=True)
    (d / "meta.json").write_bytes(b"\xff\xfe not utf8")
    (d / "design-system.md").write_bytes(b"# ok\xff\n")
    with pytest.warns(UserWarning):
        assert load_library_system("binary") is None


# ---- export output validity ---------------------------------------------------------------


def test_export_w3c_shape_is_valid_w3c_tokens_json() -> None:
    """Every node is `{"$value", "$type"}` and the whole document serializes as JSON."""
    w3c = export_tokens_w3c(TOKENS)
    text = json.dumps(w3c, indent=2)
    parsed = json.loads(text)  # must actually parse
    for group in ("color", "fontSize", "spacing", "borderRadius"):
        section = parsed[group]
        assert isinstance(section, dict)
        for key, node in section.items():
            assert set(node) == {"$value", "$type"}, key
            assert isinstance(node["$type"], str)
    # an empty token set still yields the four groups, valid and empty
    empty = json.loads(json.dumps(export_tokens_w3c({})))
    assert all(empty[group] == {} for group in ("color", "fontSize", "spacing", "borderRadius"))


def test_export_tailwind_is_a_valid_js_object() -> None:
    """The snippet parses as a JS object literal with the tokens under theme.extend."""
    text = export_tokens_tailwind(TOKENS)
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))
    body = body.replace("module.exports = ", "", 1).rstrip()
    assert body.endswith(";")
    # quote the unquoted JS keys so the object parses as JSON
    as_json = re.sub(r'([{,]\s*)(")?([A-Za-z_][A-Za-z0-9_]*)(")?:', r'\1"\3":', body[:-1])
    parsed = json.loads(as_json)
    extend = parsed["theme"]["extend"]
    assert extend["colors"]["primary"] == "#2563EB"
    assert extend["fontSize"]["base"] == "15px"
    assert extend["spacing"]["md"] == "16px"
    assert extend["borderRadius"]["full"] == "9999px"


def test_export_css_parses_as_custom_properties() -> None:
    text = export_tokens_css(TOKENS)
    lines = [line.strip() for line in text.splitlines()]
    assert lines[0] == ":root {" and lines[-1] == "}"
    declarations = lines[1:-1]
    assert declarations, "expected at least one custom property"
    for declaration in declarations:
        assert declaration.startswith("--") and declaration.endswith(";")
        prop, _, value = declaration[:-1].partition(":")
        assert prop.strip() and value.strip(), declaration
    props = [declaration[:-1].partition(":")[0].strip() for declaration in declarations]
    assert len(props) == len(set(props)), "duplicate custom properties"


def test_cli_export_write_failure_reports_cleanly(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_library_system("chat-ui", MARKDOWN, dict(TOKENS))

    def boom(target: object, text: object) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr("jev_agent.cli.design_cmds._write_atomic", boom)
    code, out = _jev(home, "design", "export", "chat-ui", "--format", "css")
    assert code == 1
    assert "could not write" in out


# ---- delete semantics ---------------------------------------------------------------------


def test_cli_delete_without_yes_on_non_tty_deletes(home: Path) -> None:
    """The CliRunner stdin is not a TTY, so the confirm is skipped (TTY asks)."""
    save_library_system("gone", MARKDOWN, {})
    code, out = _jev(home, "design", "delete", "gone")
    assert code == 0, out
    assert list_library() == []


def test_cli_delete_never_breaks_repo_copies(home: Path, repo: Path) -> None:
    """`use` installs a full independent copy: deleting the library original
    afterwards leaves the repo's installed system untouched."""
    save_library_system("chat-ui", MARKDOWN, dict(TOKENS))
    assert _jev(home, "design", "use", "chat-ui", "--repo", str(repo))[0] == 0
    code, out = _jev(home, "design", "delete", "chat-ui", "--yes")
    assert code == 0, out
    system = get_design_system(repo)
    assert system is not None
    assert system.markdown == MARKDOWN
    assert system.tokens == TOKENS

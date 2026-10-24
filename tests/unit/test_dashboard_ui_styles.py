"""Dashboard UI styles (workstream UI-02, issue #116): registry, CSS, persistence, route.

The style registry is the single source of truth: the sidebar picker, the
`data-ui-style` attribute and `static/ui-styles.css` all key off its slugs.
`XDG_CONFIG_HOME` is isolated per test by the conftest `_isolated_env` fixture.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.config.loader import default_user_config_path
from jev_agent.dashboard import ui_styles
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.security import DashboardSettings


def _user_config() -> Path:
    return default_user_config_path()


# ---- registry integrity --------------------------------------------------------


def test_sixteen_styles_bento_default() -> None:
    assert len(ui_styles.UI_STYLES) == 16
    assert ui_styles.DEFAULT_UI_STYLE == "bento"


def test_slugs_unique_css_safe() -> None:
    slugs = [style.slug for style in ui_styles.UI_STYLES]
    assert len(set(slugs)) == 16
    assert all(re.fullmatch(r"[a-z0-9-]+", slug) for slug in slugs)
    assert ui_styles.DEFAULT_UI_STYLE in slugs


def test_labels_and_blurbs() -> None:
    labels = [style.label for style in ui_styles.UI_STYLES]
    assert all(label.strip() for label in labels)
    assert len(set(labels)) == 16
    assert all(style.blurb.strip() for style in ui_styles.UI_STYLES)
    assert ui_styles.label_for("bento") == "Bento Grid"


def test_normalize() -> None:
    for style in ui_styles.UI_STYLES:
        assert ui_styles.normalize_ui_style(style.slug) == style.slug
    for bad in (None, "", "nope", "BENTO", 123):
        assert ui_styles.normalize_ui_style(bad) == "bento"


def test_css_covers_every_slug() -> None:
    css = (Path(ui_styles.__file__).parent / "static" / "ui-styles.css").read_text(encoding="utf-8")
    for style in ui_styles.UI_STYLES:
        assert f'[data-ui-style="{style.slug}"]' in css, style.slug


# ---- persistence ---------------------------------------------------------------


def test_round_trip_preserves_other_config() -> None:
    assert ui_styles.read_ui_style() == "bento"  # nothing stored yet
    assert ui_styles.write_ui_style("cyberpunk") == "cyberpunk"
    assert ui_styles.read_ui_style() == "cyberpunk"
    text = _user_config().read_text(encoding="utf-8")
    assert "[dashboard]" in text and 'ui_style = "cyberpunk"' in text
    assert "[dispatcher]" in text  # the pre-existing config survived


def test_write_unknown_slug_rejected() -> None:
    with pytest.raises(ValueError, match="unknown UI style"):
        ui_styles.write_ui_style("nope")
    assert ui_styles.read_ui_style() == "bento"


def test_read_missing_file_is_default(tmp_path: Path) -> None:
    assert ui_styles.read_ui_style(tmp_path / "nope.toml") == "bento"


def test_read_corrupt_file_is_default(tmp_path: Path) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("[dashboard\nui_style = ", encoding="utf-8")
    assert ui_styles.read_ui_style(bad) == "bento"


def test_read_unknown_stored_value_is_default(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[dashboard]\nui_style = "nope"\n', encoding="utf-8")
    assert ui_styles.read_ui_style(config) == "bento"


# ---- config schema ---------------------------------------------------------------


def test_dashboard_section_is_a_first_class_config_key(tmp_path: Path) -> None:
    """`[dashboard] ui_style` validates; the dashboard reads the user file only."""
    from jev_agent.config import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jev.toml").write_text("", encoding="utf-8")
    user = tmp_path / "user.toml"
    user.write_text('[dashboard]\nui_style = "cyberpunk"\n', encoding="utf-8")
    assert load_config(repo, user).dashboard.ui_style == "cyberpunk"
    assert load_config(repo, tmp_path / "missing.toml").dashboard.ui_style == "bento"


# ---- dashboard route -----------------------------------------------------------


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})))
    with TestClient(app) as client:
        yield client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").text)
    assert match
    return match.group(1)


def _post(client: TestClient, data: dict[str, str]) -> httpx.Response:
    response = client.post(
        "/ui-style",
        data={**data, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert isinstance(response, httpx.Response)
    return response


def test_picker_rendered_with_sixteen_options(client: TestClient) -> None:
    html = client.get("/").text
    assert 'data-ui-style="bento"' in html
    assert 'id="ui-style"' in html
    options = re.findall(
        r'<option value="([a-z0-9-]+)"', html.split('id="ui-style"')[1].split("</select>")[0]
    )
    assert sorted(options) == sorted(style.slug for style in ui_styles.UI_STYLES)
    assert re.search(r'<option value="bento" selected[\s>]', html)


def test_set_style_persists_and_redirects_back(client: TestClient) -> None:
    response = _post(client, {"style": "brutalism"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert ui_styles.read_ui_style() == "brutalism"
    html = client.get("/queue").text
    assert 'data-ui-style="brutalism"' in html
    assert re.search(r'<option value="brutalism" selected[\s>]', html)


def test_set_style_redirects_to_referer(client: TestClient) -> None:
    response = client.post(
        "/ui-style",
        data={"style": "y2k", "csrf_token": _csrf(client)},
        headers={"referer": "http://testserver/spend"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/spend"
    assert ui_styles.read_ui_style() == "y2k"


def test_set_unknown_style_flashes_error_and_keeps_old(client: TestClient) -> None:
    response = _post(client, {"style": "nope"})
    assert response.status_code == 303
    assert ui_styles.read_ui_style() == "bento"
    page = client.get(response.headers["location"]).text
    assert "unknown UI style" in page


def test_set_style_needs_csrf(client: TestClient) -> None:
    response = client.post("/ui-style", data={"style": "bento"}, follow_redirects=False)
    assert response.status_code == 403


def test_ui_style_route_rejects_get(client: TestClient) -> None:
    assert client.get("/ui-style").status_code == 405

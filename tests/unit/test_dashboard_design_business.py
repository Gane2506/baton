"""The dashboard half of issue #283: business-context intake in the Design Studio.

Covers the wizard's Business step, the system detail edit form, the code
page's business panel + add/remove actions, and the download zip.
"""

from __future__ import annotations

import html
import io
import json
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.core.design_business import BusinessContext, propose_elements
from jev_agent.core.design_library import save_library_system
from jev_agent.dashboard.app import create_app
from jev_agent.dashboard.design_studio import (
    STUDIO_STEPS,
    WizardError,
    first_incomplete_step,
    validate_step,
)
from jev_agent.dashboard.security import DashboardSettings

TOKENS = {
    "colors": {"primary": "#e8590c", "ink": "#1a1b1e"},
    "type_scale": {"base": 16},
    "spacing": {"md": 16},
    "radii": {"md": 8},
}
MARKDOWN = (
    "# Acme Brand\n\nA warm, confident marketing system.\n\n## Components\n\nButtons and cards.\n"
)
RESTAURANT = BusinessContext(industry="Restaurant", offering="Online ordering")


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at a temp dir so the library never touches the real ~/.jev."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def client(fixture_repo: Path) -> Iterator[TestClient]:
    app = create_app(
        DashboardSettings(root=fixture_repo, allowed_hosts=frozenset({"testserver"})),
    )
    with TestClient(app) as client:
        yield client


def _csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").text)
    assert match
    return match.group(1)


def _post(client: TestClient, url: str, data: dict[str, str] | None = None) -> httpx.Response:
    """POST with a valid CSRF token, following redirects so flashes are visible."""
    return cast(
        httpx.Response,
        client.post(
            url,
            data={"csrf_token": _csrf(client), **(data or {})},
            follow_redirects=True,
        ),
    )


def _visible(response: httpx.Response) -> str:
    """The page text as the user sees it (flashes render quotes as `&#34;`)."""
    return html.unescape(response.text)


def _seed(name: str = "acme-brand", **meta: object) -> int:
    extras: dict[str, object] = {
        "name": name,
        "source": "wizard",
        "app_type": "marketing",
        "stack": "Next.js",
    }
    extras.update(meta)
    return save_library_system(name, MARKDOWN, dict(TOKENS), extras)


def _library_meta(home: Path, name: str = "acme-brand") -> dict[str, object]:
    raw = json.loads(
        (home / ".jev" / "design-systems" / name / "meta.json").read_text(encoding="utf-8")
    )
    assert isinstance(raw, dict)
    return {str(key): value for key, value in raw.items()}


def _wizard_to_generate(client: TestClient, **overrides: str) -> None:
    """Drive the full wizard: Purpose -> Business -> App type -> Stack -> Brand -> Direction."""
    defaults = {
        "product": "Acme Analytics",
        "industry": "Restaurant",
        "offering": "Online ordering",
        "app_type": "dashboard",
        "stack": "Web + Tailwind",
        "name": "acme-brand",
        "vibe": "warm, confident",
        "theme": "both",
    }
    defaults.update(overrides)
    _post(client, "/design-studio/new")
    response = _post(client, "/design-studio/new/purpose", {"product": defaults["product"]})
    assert response.request.url.path == "/design-studio/new/business"
    response = _post(
        client,
        "/design-studio/new/business",
        {"industry": defaults["industry"], "offering": defaults["offering"]},
    )
    assert response.request.url.path == "/design-studio/new/app_type"
    response = _post(client, "/design-studio/new/app_type", {"app_type": defaults["app_type"]})
    assert response.request.url.path == "/design-studio/new/stack"
    response = _post(client, "/design-studio/new/stack", {"stack": defaults["stack"]})
    assert response.request.url.path == "/design-studio/new/brand"
    response = _post(
        client,
        "/design-studio/new/brand",
        {
            "name": defaults["name"],
            "vibe": defaults["vibe"],
            "theme": defaults["theme"],
        },
    )
    assert response.request.url.path == "/design-studio/new/direction"
    response = _post(client, "/design-studio/new/direction", {})
    assert response.request.url.path == "/design-studio/new/generate"


# ---- wizard business step ------------------------------------------------------------


def test_step_order_is_purpose_business_app_type_stack_brand_direction_generate() -> None:
    assert STUDIO_STEPS == (
        "purpose",
        "business",
        "app_type",
        "stack",
        "brand",
        "direction",
        "generate",
    )
    assert first_incomplete_step({}) == "purpose"
    assert first_incomplete_step({"purpose": {"product": "Acme"}}) == "business"


def test_business_step_validates_empty_ok_and_keeps_all_five_keys() -> None:
    state = {"purpose": {"product": "Acme"}}
    answers = validate_step("business", {}, state)
    assert answers == {
        "industry": "",
        "audience": "",
        "tone": "",
        "offering": "",
        "description": "",
    }
    # All five keys present even when empty, so first_incomplete_step treats
    # the step as done instead of looping the wizard forever.
    assert first_incomplete_step({**state, "business": answers}) == "app_type"


def test_business_step_keeps_submitted_values() -> None:
    answers = validate_step(
        "business",
        {"industry": "Restaurant", "audience": "Families", "description": "Bistro"},
        {"purpose": {"product": "Acme"}},
    )
    assert answers["industry"] == "Restaurant"
    assert answers["audience"] == "Families"
    assert answers["tone"] == ""
    assert answers["description"] == "Bistro"


def test_business_step_too_long_raises_wizard_error() -> None:
    with pytest.raises(WizardError, match="industry"):
        validate_step(
            "business",
            {"industry": "x" * 501},
            {"purpose": {"product": "Acme"}},
        )


def test_business_step_requires_purpose_first() -> None:
    with pytest.raises(WizardError, match="Purpose"):
        validate_step("business", {}, {})


def test_wizard_business_page_renders_and_empty_post_advances(
    client: TestClient, home: Path
) -> None:
    _post(client, "/design-studio/new")
    _post(client, "/design-studio/new/purpose", {"product": "Acme"})
    page = client.get("/design-studio/new/business")
    assert page.status_code == 200
    assert "seven guided questions" in page.text
    assert "Optional — skip what doesn't apply" in page.text
    for field in ("industry", "audience", "tone", "offering", "description"):
        assert f'name="{field}"' in page.text
    response = _post(client, "/design-studio/new/business", {})
    assert response.request.url.path == "/design-studio/new/app_type"


def test_wizard_generate_summary_shows_business(client: TestClient, home: Path) -> None:
    _post(client, "/design-studio/new")
    _post(client, "/design-studio/new/purpose", {"product": "Acme"})
    _post(
        client,
        "/design-studio/new/business",
        {"industry": "Restaurant", "offering": "Online ordering"},
    )
    _post(client, "/design-studio/new/app_type", {"app_type": "dashboard"})
    _post(client, "/design-studio/new/stack", {"stack": "Web + Tailwind"})
    _post(
        client,
        "/design-studio/new/brand",
        {"name": "acme-brand", "vibe": "warm", "theme": "both"},
    )
    _post(client, "/design-studio/new/direction", {})
    page = client.get("/design-studio/new/generate")
    assert page.status_code == 200
    assert "Restaurant" in page.text
    assert "Online ordering" in page.text


def test_generate_stores_business_context_and_elements(client: TestClient, home: Path) -> None:
    _wizard_to_generate(client)
    response = _post(client, "/design-studio/new/generate", {})
    assert response.request.url.path == "/design-systems/acme-brand"
    assert "created in the design library" in _visible(response)
    meta = _library_meta(home)
    assert meta["business_context"] == {
        "industry": "Restaurant",
        "offering": "Online ordering",
    }
    assert meta["business_elements"] == [element.key for element in propose_elements(RESTAURANT)]
    assert meta["business_elements"], "a restaurant context should propose elements"


def test_generate_with_empty_business_stores_nothing(client: TestClient, home: Path) -> None:
    _wizard_to_generate(client, industry="", offering="")
    response = _post(client, "/design-studio/new/generate", {})
    assert response.request.url.path == "/design-systems/acme-brand"
    meta = _library_meta(home)
    assert "business_context" not in meta
    assert "business_elements" not in meta


# ---- system detail page ---------------------------------------------------------------


def test_detail_shows_business_panel_and_edit_form(client: TestClient, home: Path) -> None:
    _seed(business_context=RESTAURANT.to_meta())
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    assert "Business context" in page.text
    assert "Restaurant" in page.text
    assert "Online ordering" in page.text
    assert 'action="/design-systems/business"' in page.text
    assert 'name="industry"' in page.text


def test_detail_without_context_invites_adding_one(client: TestClient, home: Path) -> None:
    _seed()
    page = client.get("/design-systems/acme-brand")
    assert page.status_code == 200
    assert "No business context yet" in page.text
    assert 'action="/design-systems/business"' in page.text


def test_detail_edit_updates_context_without_bumping_version(
    client: TestClient, home: Path
) -> None:
    _seed(business_context=RESTAURANT.to_meta())
    assert _library_meta(home)["version"] == 1
    response = _post(
        client,
        "/design-systems/business",
        {"name": "acme-brand", "industry": "SaaS", "offering": "Analytics"},
    )
    assert response.request.url.path == "/design-systems/acme-brand"
    assert 'business context updated for "acme-brand"' in _visible(response)
    meta = _library_meta(home)
    assert meta["version"] == 1, "a meta-only edit must not bump the version"
    assert meta["business_context"] == {"industry": "SaaS", "offering": "Analytics"}
    # No stored element list existed, so the edit resets it to the newly
    # proposed keys for the SaaS profile.
    assert meta["business_elements"] == [
        element.key
        for element in propose_elements(BusinessContext(industry="SaaS", offering="Analytics"))
    ]
    page = client.get("/design-systems/acme-brand")
    assert "SaaS" in page.text


def test_detail_edit_keeps_customized_element_list(client: TestClient, home: Path) -> None:
    _seed(business_context=RESTAURANT.to_meta(), business_elements=["menu-card"])
    response = _post(
        client,
        "/design-systems/business",
        {"name": "acme-brand", "industry": "SaaS", "offering": "Analytics"},
    )
    assert response.request.url.path == "/design-systems/acme-brand"
    meta = _library_meta(home)
    assert meta["business_elements"] == ["menu-card"]


def test_detail_edit_to_empty_clears_both_keys(client: TestClient, home: Path) -> None:
    _seed(
        business_context=RESTAURANT.to_meta(),
        business_elements=["menu-card", "reservation-form"],
    )
    _post(client, "/design-systems/business", {"name": "acme-brand"})
    meta = _library_meta(home)
    assert "business_context" not in meta
    assert "business_elements" not in meta
    assert meta["version"] == 1


def test_detail_edit_unknown_system_flashes_error(client: TestClient, home: Path) -> None:
    response = _post(client, "/design-systems/business", {"name": "missing", "industry": "SaaS"})
    assert response.request.url.path == "/design-systems/missing"
    assert "no design system" in _visible(response)


# ---- code page -------------------------------------------------------------------------


def test_code_page_shows_business_panel_and_file_entries(client: TestClient, home: Path) -> None:
    _seed(business_context=RESTAURANT.to_meta())
    page = cast(
        httpx.Response,
        client.get("/design-systems/acme-brand/code?target=html-css"),
    )
    assert page.status_code == 200
    assert "Suggested for your business" in page.text
    visible = _visible(page)
    for element in propose_elements(RESTAURANT):
        assert element.title in visible
        assert element.why in visible
    assert "business-elements.html" in page.text
    assert "business-elements.css" in page.text
    assert "More elements" in page.text
    assert 'action="/design-systems/business-elements"' in page.text


def test_code_page_without_context_shows_no_business_panel(client: TestClient, home: Path) -> None:
    _seed()
    page = client.get("/design-systems/acme-brand/code?target=html-css")
    assert page.status_code == 200
    assert "Suggested for your business" not in page.text
    assert "business-elements.html" not in page.text


def test_business_elements_add_and_remove_toggle_stored_list(
    client: TestClient, home: Path
) -> None:
    _seed(business_context=RESTAURANT.to_meta(), business_elements=[])
    response = _post(
        client,
        "/design-systems/business-elements",
        {"name": "acme-brand", "element": "menu-card", "action": "add"},
    )
    assert response.request.url.path == "/design-systems/acme-brand/code"
    assert 'added business element "menu-card"' in _visible(response)
    assert _library_meta(home)["business_elements"] == ["menu-card"]
    # Adding again keeps the order and does not duplicate the key.
    _post(
        client,
        "/design-systems/business-elements",
        {"name": "acme-brand", "element": "menu-card", "action": "add"},
    )
    assert _library_meta(home)["business_elements"] == ["menu-card"]
    response = _post(
        client,
        "/design-systems/business-elements",
        {"name": "acme-brand", "element": "menu-card", "action": "remove"},
    )
    assert 'removed business element "menu-card"' in _visible(response)
    assert _library_meta(home)["business_elements"] == []


def test_business_elements_add_preserves_target(client: TestClient, home: Path) -> None:
    _seed(business_context=RESTAURANT.to_meta(), business_elements=[])
    response = _post(
        client,
        "/design-systems/business-elements",
        {
            "name": "acme-brand",
            "element": "menu-card",
            "action": "add",
            "target": "react-tailwind",
        },
    )
    assert response.request.url.path == "/design-systems/acme-brand/code"
    assert "target=react-tailwind" in str(response.request.url)


def test_business_elements_rejects_unknown_key(client: TestClient, home: Path) -> None:
    _seed(business_context=RESTAURANT.to_meta(), business_elements=["menu-card"])
    response = _post(
        client,
        "/design-systems/business-elements",
        {"name": "acme-brand", "element": "not-a-thing", "action": "add"},
    )
    assert response.request.url.path == "/design-systems/acme-brand/code"
    assert "unknown business element" in _visible(response)
    assert _library_meta(home)["business_elements"] == ["menu-card"]


def test_business_elements_rejects_bad_action(client: TestClient, home: Path) -> None:
    _seed(business_context=RESTAURANT.to_meta(), business_elements=["menu-card"])
    response = _post(
        client,
        "/design-systems/business-elements",
        {"name": "acme-brand", "element": "menu-card", "action": "explode"},
    )
    assert "unknown business element action" in _visible(response)
    assert _library_meta(home)["business_elements"] == ["menu-card"]


# ---- download + showcase ---------------------------------------------------------------


def test_download_zip_includes_business_files_when_enabled(client: TestClient, home: Path) -> None:
    _seed(business_context=RESTAURANT.to_meta(), business_elements=["menu-card"])
    response = client.get("/design-systems/acme-brand/code/download?target=html-css")
    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert any(name.endswith("/business-elements.html") for name in names)
    assert any(name.endswith("/business-elements.css") for name in names)
    assert any(name.endswith("/tokens.css") for name in names)


def test_download_zip_without_enabled_elements_has_no_business_files(
    client: TestClient, home: Path
) -> None:
    _seed(business_context=RESTAURANT.to_meta(), business_elements=[])
    response = client.get("/design-systems/acme-brand/code/download?target=html-css")
    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert not any("business-elements" in name for name in names)


def test_showcase_on_demand_carries_business_context(client: TestClient, home: Path) -> None:
    # Seeded systems have no saved showcase.html, so the page renders on demand.
    _seed(business_context=RESTAURANT.to_meta())
    page = client.get("/design-systems/acme-brand/showcase")
    assert page.status_code == 200
    assert "Built for Restaurant" in page.text

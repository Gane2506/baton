"""The Design Studio: the landing, the new-system wizard and its creation pipeline
(issue #124, DS-01)."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest

pytest.importorskip("starlette", reason="dashboard extra not installed")

from starlette.testclient import TestClient

from jev_agent.core.design_kit import APP_TYPES, STACKS
from jev_agent.core.design_library import list_library, save_library_system
from jev_agent.dashboard import studio_decisions
from jev_agent.dashboard.app import NAV, create_app
from jev_agent.dashboard.design_studio import (
    STUDIO_STEPS,
    StudioWizard,
    WizardError,
    first_incomplete_step,
    generate_from_state,
    stack_options,
    validate_step,
    wizard_context,
)
from jev_agent.dashboard.security import DashboardSettings
from jev_agent.dashboard.studio_decisions import StackRecommendation
from jev_agent.dispatch.base import Dispatcher


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
    """POST with a valid CSRF token, following redirects."""
    # The TestClient returns httpx2.Response when httpx2 is installed (it
    # is, via the code-orchestrator extra -> huggingface_hub) and
    # httpx.Response otherwise; cast to the declared type (both share the
    # same response API).
    return cast(
        httpx.Response,
        client.post(
            url,
            data={"csrf_token": _csrf(client), **(data or {})},
            follow_redirects=True,
        ),
    )


def _wizard(client: TestClient, **overrides: str) -> None:
    """Drive the wizard through Purpose -> Business -> App type -> Stack -> Brand -> Direction."""
    defaults = {
        "product": "Acme Analytics",
        "industry": "SaaS",
        "offering": "Analytics",
        "app_type": "dashboard",
        "stack": "Web + Tailwind",
        "name": "acme-brand",
        "vibe": "warm, confident",
        "theme": "both",
    }
    defaults.update(overrides)
    _post(client, "/design-studio/new")  # starts the wizard at Purpose (POST: it clears state)
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
    # The Direction step is all-optional: submitting it untouched keeps the
    # compiled brief exactly as previewed.
    response = _post(client, "/design-studio/new/direction", {})
    assert response.request.url.path == "/design-studio/new/generate"


# ---- the recommendation seam -------------------------------------------------------------


def test_stack_options_recommended_first() -> None:
    for app_type in APP_TYPES:
        options, recommended = stack_options(app_type)
        assert options, f"no stacks for {app_type}"
        assert recommended == options[0], "the recommendation must be listed first"


def test_stack_options_unknown_app_type() -> None:
    with pytest.raises(KeyError):
        stack_options("spaceship")


# ---- wizard state ------------------------------------------------------------------------


def test_first_incomplete_step() -> None:
    assert first_incomplete_step({}) == "purpose"
    state = {"purpose": {"product": "x"}, "app_type": {"app_type": "mobile"}}
    assert first_incomplete_step(state) == "business"
    full = {
        "purpose": {"product": "x"},
        "business": {"industry": "", "audience": "", "tone": "", "offering": "", "description": ""},
        "app_type": {"app_type": "mobile"},
        "stack": {"stack": "Flutter"},
        "brand": {"name": "acme", "vibe": "", "theme": "both"},
        "direction": {"complete": "1"},
    }
    assert first_incomplete_step(full) is None
    assert first_incomplete_step({**full, "direction": {}}) == "direction"


def test_wizard_store_prunes_and_bounds() -> None:
    wizard = StudioWizard()
    wizard.put("s1", "purpose", {"product": "x"})
    assert wizard.get("s1") == {"purpose": {"product": "x"}}
    wizard.clear("s1")
    assert wizard.get("s1") == {}
    assert StudioWizard._MAX_SESSIONS > 0
    assert StudioWizard._TTL_S > 0


def test_validate_step_unknown() -> None:
    with pytest.raises(WizardError):
        validate_step("generate", {}, {})


# ---- nav ---------------------------------------------------------------------------------


def test_nav_lists_design_studio() -> None:
    assert any(item.href == "/design-studio" and item.label == "Design Studio" for item in NAV)
    hrefs = [item.href for item in NAV]
    assert len(hrefs) == len(set(hrefs)), "duplicate href in dashboard NAV"


def test_nav_link_renders_and_is_active(client: TestClient, home: Path) -> None:
    board = client.get("/")
    assert board.status_code == 200
    assert 'href="/design-studio"' in board.text
    assert 'href="#i-sparkles"' in board.text
    page = client.get("/design-studio")
    assert page.status_code == 200
    assert re.search(
        r'<a href="/design-studio"[^>]*class="active"[^>]*aria-current="page"',
        page.text,
    )


# ---- landing -----------------------------------------------------------------------------


def test_studio_landing_has_two_entry_cards(client: TestClient, home: Path) -> None:
    page = client.get("/design-studio")
    assert page.status_code == 200
    assert "New design system" in page.text
    assert "/design-studio/new" in page.text
    assert "Reverse from URL or screenshots" in page.text
    assert "/design-studio/reverse" in page.text
    # Audit 5: the wizard card is a <button> (POST form, issue #200) and Chrome
    # does not lay a button's children out as a flex container, so the column
    # lives on an inner span; the button itself is a plain block.
    assert "studio-card-inner" in page.text
    css = client.get("/static/style.css").text
    assert ".studio-card-inner" in css
    assert "button.studio-card" in css
    # Chrome's button UA style is white-space: nowrap, inherited by the card
    # copy: the description must wrap like the link card's.
    assert "white-space: normal" in css


# ---- the landing library previews (issue #281) -------------------------------------------


def _seed_preview_system(home: Path) -> None:
    """One library system with known tokens, for the landing previews."""
    save_library_system(
        "acme",
        "# Acme\n",
        {
            "colors": {
                "primary": "#2563eb",
                "primary_hover": "#1d4ed8",
                "background": "#ffffff",
                "text": "#101418",
                "success": "#16a34a",
            },
            "type_scale": {"base": 16, "xl": 24},
        },
        meta_extra={
            "app_type": "dashboard",
            "stack": "Web + Tailwind",
            "vibe_profile": "minimal",
        },
    )


def test_studio_landing_shows_library_previews(client: TestClient, home: Path) -> None:
    _seed_preview_system(home)
    page = client.get("/design-studio")
    assert page.status_code == 200
    assert "From your library" in page.text
    assert "studio-preview" in page.text
    assert "acme" in page.text
    # The preview wears the system's own tokens — carried in data attributes,
    # never inline style: the dashboard CSP drops inline styles silently
    # (issue #200), so data-attrs.js applies them instead.
    assert 'data-bg-color="#2563EB"' in page.text
    assert 'data-fg-color="#2563EB"' in page.text
    assert 'data-bg-color="#FFFFFF"' in page.text
    assert 'data-swatch-color="#16A34A"' in page.text
    assert 'style="background:' not in page.text
    assert 'style="color:' not in page.text
    # The minimal vibe's tagline, from the showcase's own profiles.
    assert "Less, but better." in page.text
    assert "/design-systems/acme/showcase" in page.text
    assert "/design-systems/acme/code" in page.text


def test_studio_landing_empty_library(client: TestClient, home: Path) -> None:
    page = client.get("/design-studio")
    assert page.status_code == 200
    assert "No systems in the library yet." in page.text
    assert 'studio-preview"' not in page.text


def test_studio_landing_skips_invalid_colors(client: TestClient, home: Path) -> None:
    # A garbage color value (or a nested dict) must never reach an inline style:
    # the preview falls back to the default palette instead (the #279 lesson).
    save_library_system(
        "broken",
        "# Broken\n",
        {
            "colors": {
                "primary": "not-a-color",
                "background": {"light": "#FFFFFF"},
            },
        },
        meta_extra={"app_type": "dashboard", "stack": "Web + Tailwind"},
    )
    page = client.get("/design-studio")
    assert page.status_code == 200
    assert "not-a-color" not in page.text
    assert "{'light'" not in page.text
    assert "#2563EB" in page.text  # the fallback primary


# ---- the wizard steps --------------------------------------------------------------------


def test_start_redirects_to_purpose(client: TestClient, home: Path) -> None:
    # Issue #200: only the POST clears wizard state (CSRF-checked); a GET is
    # a pure redirect to Purpose, so it can never wipe an in-progress wizard.
    # The TestClient returns httpx2.Response when httpx2 is installed; cast
    # so this variable keeps one type alongside `_post`'s httpx.Response.
    response = cast(httpx.Response, client.get("/design-studio/new", follow_redirects=False))
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/design-studio/new/purpose"
    response = _post(client, "/design-studio/new", {})
    assert response.request.url.path == "/design-studio/new/purpose"
    response = cast(
        httpx.Response,
        client.post("/design-studio/new", data={}, follow_redirects=False),  # no CSRF token
    )
    assert response.status_code == 403


def test_get_new_never_clears_wizard_state(client: TestClient, home: Path) -> None:
    # The 303 on GET /design-studio/new is the by-design redirect to Purpose
    # (issue #281): a stray prefetch must never wipe an in-progress wizard.
    _post(client, "/design-studio/new")
    _post(client, "/design-studio/new/purpose", {"product": "Acme Analytics"})
    response = cast(httpx.Response, client.get("/design-studio/new", follow_redirects=False))
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/design-studio/new/purpose"
    # The visitor is still on Business (first incomplete step after Purpose,
    # issue #283): the Purpose answer survived the GET.
    page = client.get("/design-studio/new/business")
    assert page.status_code == 200
    assert "What business is this for?" in page.text


def test_unknown_step_404s(client: TestClient, home: Path) -> None:
    assert client.get("/design-studio/new/bogus").status_code == 404
    response = _post(client, "/design-studio/new/bogus", {})
    assert response.status_code == 404


def test_stepper_labels_in_order(client: TestClient, home: Path) -> None:
    page = client.get("/design-studio/new/purpose")
    assert page.status_code == 200
    labels = re.findall(r'<span class="step-label">([^<]+)</span>', page.text)
    assert labels == [
        label
        for _, label in [
            ("purpose", "Purpose"),
            ("business", "Business"),
            ("app_type", "App type"),
            ("stack", "Stack"),
            ("brand", "Brand"),
            ("direction", "Direction"),
            ("generate", "Generate"),
        ]
    ]
    assert STUDIO_STEPS == (
        "purpose",
        "business",
        "app_type",
        "stack",
        "brand",
        "direction",
        "generate",
    )


def test_cannot_skip_ahead(client: TestClient, home: Path) -> None:
    response = client.get("/design-studio/new/stack", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/design-studio/new/purpose"


def test_purpose_requires_a_name(client: TestClient, home: Path) -> None:
    response = _post(client, "/design-studio/new/purpose", {"product": "  "})
    assert response.status_code == 200
    assert "Give the product a name" in response.text
    # The visitor stays on the same step; nothing was recorded.
    assert (
        client.get("/design-studio/new/app_type", follow_redirects=False).headers["location"]
        == "/design-studio/new/purpose"
    )


def test_app_type_rejects_unknown(client: TestClient, home: Path) -> None:
    _post(client, "/design-studio/new/purpose", {"product": "Acme"})
    response = _post(client, "/design-studio/new/app_type", {"app_type": "spaceship"})
    assert response.status_code == 200
    assert "Pick one of the four app types" in response.text


def test_stack_shows_recommendation_first(client: TestClient, home: Path) -> None:
    _post(client, "/design-studio/new/purpose", {"product": "Acme"})
    _post(client, "/design-studio/new/business", {})
    _post(client, "/design-studio/new/app_type", {"app_type": "dashboard"})
    page = client.get("/design-studio/new/stack")
    assert page.status_code == 200
    options, recommended = stack_options("dashboard")
    positions = [page.text.index(f'value="{option}"') for option in options]
    assert positions == sorted(positions), "stacks must render in recommendation order"
    assert recommended in page.text
    assert "Recommended" in page.text


def test_stack_rejects_unknown(client: TestClient, home: Path) -> None:
    _post(client, "/design-studio/new/purpose", {"product": "Acme"})
    _post(client, "/design-studio/new/app_type", {"app_type": "dashboard"})
    response = _post(client, "/design-studio/new/stack", {"stack": "COBOL"})
    assert response.status_code == 200
    assert "Pick one of the listed stacks" in response.text


def test_brand_validates_name_and_keeps_input(client: TestClient, home: Path) -> None:
    _wizard(client, name="unused")
    # Walk back to Brand and submit a bad name: the error shows, input is kept.
    response = _post(
        client,
        "/design-studio/new/brand",
        {"name": "not a name!", "vibe": "warm", "theme": "dark"},
    )
    assert response.status_code == 200
    assert "invalid library design system name" in html.unescape(response.text)
    assert 'value="warm"' in response.text  # the vibe input survived


def test_brand_rejects_taken_name(client: TestClient, home: Path) -> None:
    save_library_system("taken", "# Taken", {"colors": {}}, {"name": "taken"})
    _post(client, "/design-studio/new/purpose", {"product": "Acme"})
    _post(client, "/design-studio/new/app_type", {"app_type": "dashboard"})
    _post(client, "/design-studio/new/stack", {"stack": "Web + Tailwind"})
    response = _post(
        client,
        "/design-studio/new/brand",
        {"name": "taken", "vibe": "", "theme": "both"},
    )
    assert response.status_code == 200
    assert "already exists in the library" in html.unescape(response.text)


def test_generate_requires_every_step(client: TestClient, home: Path) -> None:
    response = _post(client, "/design-studio/new/generate", {})
    assert response.request.url.path == "/design-studio/new/purpose"
    assert "Finish every wizard step" in html.unescape(response.text)


# ---- generation --------------------------------------------------------------------------


def test_full_wizard_creates_library_system(client: TestClient, home: Path) -> None:
    _wizard(client)
    page = client.get("/design-studio/new/generate")
    assert page.status_code == 200
    assert "Acme Analytics" in page.text
    assert "acme-brand" in page.text

    response = _post(client, "/design-studio/new/generate", {})
    assert response.request.url.path == "/design-systems/acme-brand"
    assert "created in the design library" in html.unescape(response.text)

    assert "acme-brand" in list_library()
    system_dir = home / ".jev" / "design-systems" / "acme-brand"
    for filename in (
        "design-system.md",
        "tokens.json",
        "meta.json",
        "showcase.html",
        "motion.js",
        "scene.js",
        "logo.svg",
        "favicon.svg",
        "favicon-16.png",
        "favicon-32.png",
        "favicon-180.png",
        "favicon-192.png",
        "favicon-512.png",
    ):
        assert (system_dir / filename).is_file(), f"missing {filename}"
    assert "prefers-reduced-motion" in (system_dir / "motion.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in (system_dir / "scene.js").read_text(encoding="utf-8")

    # Issue #301: meta.json records the brief, the fingerprint, the gates
    # result, and the scene type — and the generated system honours the
    # brief's decided choices.
    meta = json.loads((system_dir / "meta.json").read_text(encoding="utf-8"))
    brief = meta["design_brief"]
    assert brief["harmony"] and brief["type_pairing"] and brief["layout"]
    assert brief["scene"] in (
        "warm-ambient",
        "data-geometry",
        "product-orbit",
        "storefront",
        "calm-growth",
        "campus",
        "journey",
    )
    assert meta["scene_type"] == brief["scene"]
    assert meta["design_fingerprint"]
    assert len(meta["design_fingerprint"]) == 64
    assert len(brief["palette_ramp"]) == 11
    assert set(brief["axes"]) == {
        "warmth",
        "contrast",
        "density",
        "geometry",
        "formality",
        "motion",
        "type_personality",
    }
    gates = meta["quality_gates"]
    assert gates["passed"] is True
    assert gates["attempts"] >= 1
    assert gates["violations"] == []
    assert "fallback" not in gates
    choices = meta["design_choices"]
    assert choices["color_strategy"] == brief["harmony"]
    assert choices["type_pairing"] == brief["type_pairing"]
    assert choices["layout"] == brief["layout"]
    assert choices["motion"] == brief["motion"]
    assert choices["components"] == brief["layout_pattern"]


def test_generate_clears_wizard_state(client: TestClient, home: Path) -> None:
    _wizard(client, name="fresh-system")
    _post(client, "/design-studio/new/generate", {})
    # A new visit restarts at Purpose instead of resuming the finished wizard.
    response = client.get("/design-studio/new/stack", follow_redirects=False)
    assert response.headers["location"] == "/design-studio/new/purpose"


# ---- DS-03: the Jev recommendation --------------------------------------------------


def test_stack_step_shows_default_recommendation_card(client: TestClient, home: Path) -> None:
    # No API keys in the suite (the conftest strips them), so the wizard falls back
    # to the static default — and says so on the card.
    _post(client, "/design-studio/new/purpose", {"product": "Acme"})
    _post(client, "/design-studio/new/business", {})
    _post(client, "/design-studio/new/app_type", {"app_type": "dashboard"})
    page = client.get("/design-studio/new/stack")
    assert page.status_code == 200
    assert 'class="studio-rec"' in page.text
    assert "Default recommendation" in page.text
    assert "Jev recommended" not in page.text
    assert "Jev was unreachable" in html.unescape(page.text)


def _fake_jev_flutter(
    purpose: str, app_type: str, *, vibe: str = "", dispatcher: Dispatcher | None = None
) -> StackRecommendation:
    return StackRecommendation(
        stack="Flutter",
        source="jev",
        rationale="Jev picked Flutter (90% confidence).",
    )


def test_jev_recommendation_reorders_options_and_badges(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(
        purpose: str,
        app_type: str,
        *,
        vibe: str = "",
        dispatcher: Dispatcher | None = None,
    ) -> StackRecommendation:
        return _fake_jev_flutter(purpose, app_type, vibe=vibe, dispatcher=dispatcher)

    monkeypatch.setattr(studio_decisions, "recommend_stack", fake)
    _post(client, "/design-studio/new/purpose", {"product": "Acme"})
    _post(client, "/design-studio/new/business", {})
    _post(client, "/design-studio/new/app_type", {"app_type": "mobile"})
    page = client.get("/design-studio/new/stack")
    assert page.status_code == 200
    assert 'class="studio-rec"' in page.text
    assert "Jev recommended" in page.text
    assert "Jev picked Flutter" in html.unescape(page.text)
    assert "Default recommendation" not in page.text
    expected = ["Flutter", "React Native", "SwiftUI", "Kotlin/Jetpack Compose"]
    positions = [page.text.index(f'name="stack" value="{option}"') for option in expected]
    assert positions == sorted(positions), "the Jev pick renders first"


def test_stack_decision_made_once_per_session(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    async def counting(
        purpose: str,
        app_type: str,
        *,
        vibe: str = "",
        dispatcher: Dispatcher | None = None,
    ) -> StackRecommendation:
        calls.append((purpose, app_type))
        return StackRecommendation(
            stack=STACKS[app_type][0],
            source="default",
            rationale="the static default",
        )

    monkeypatch.setattr(studio_decisions, "recommend_stack", counting)
    _post(client, "/design-studio/new/purpose", {"product": "Acme"})
    _post(client, "/design-studio/new/app_type", {"app_type": "dashboard"})
    assert calls == [("Acme", "dashboard")]
    client.get("/design-studio/new/stack")
    client.get("/design-studio/new/stack")
    assert len(calls) == 1, "re-rendering the Stack step must not re-ask Jev"
    _post(client, "/design-studio/new/app_type", {"app_type": "mobile"})
    assert len(calls) == 2, "changing the app type re-asks exactly once"


# ---- the Design direction step (issue #301) -------------------------------------------------


def _direction_state() -> dict[str, dict[str, str]]:
    """A complete wizard state up to Brand, for a fintech dashboard."""
    return {
        "purpose": {"product": "Acme Analytics"},
        "business": {
            "industry": "fintech",
            "audience": "investors",
            "tone": "professional",
            "offering": "portfolio analytics",
            "description": "",
        },
        "app_type": {"app_type": "dashboard"},
        "stack": {"stack": "Web + Tailwind"},
        "brand": {"name": "acme-brand", "vibe": "precise, trustworthy", "theme": "both"},
    }


def _compiled_form(state: dict[str, dict[str, str]]) -> dict[str, str]:
    """The Direction form as the template renders it: sliders 0..100, the
    compiled choice keys, the focal point, and the avoid-list lines."""
    preview = wizard_context(state, "direction")["direction_brief"]
    form = {f"axis_{axis['key']}": str(axis["value"]) for axis in preview["axes"]}
    form.update(
        {
            "harmony": preview["harmony"]["key"],
            "type_pairing": preview["type_pairing"]["key"],
            "layout": preview["layout"]["key"],
            "motion_profile": preview["motion"]["key"],
            "scene": preview["scene"]["key"],
            "layout_pattern": preview["layout_pattern"]["key"],
            "focal_point": preview["focal_point"],
            "avoid": "\n".join(preview["avoid"]),
        }
    )
    return form


def test_direction_context_shows_compiled_brief() -> None:
    context = wizard_context(_direction_state(), "direction")
    preview = context["direction_brief"]
    assert [axis["key"] for axis in preview["axes"]] == [
        "warmth",
        "contrast",
        "density",
        "geometry",
        "formality",
        "motion",
        "type_personality",
    ]
    assert all(0 <= axis["value"] <= 100 for axis in preview["axes"])
    assert len(preview["palette_ramp"]) == 11
    assert all(re.match(r"^#[0-9A-F]{6}$", hex_value) for hex_value in preview["palette_ramp"])
    assert preview["primary_hex"] in preview["palette_ramp"]
    assert preview["harmony"]["name"]
    assert preview["type_pairing"]["name"]
    assert preview["layout"]["name"]
    assert preview["motion"]["name"]
    assert preview["scene"]["label"]
    assert preview["layout_pattern"]["name"]
    assert preview["avoid"], "the avoid-list is part of the preview"
    assert "no purple gradients" in preview["avoid"]
    assert preview["current"]["harmony"] == preview["harmony"]["key"]
    # A fintech brief decides data-geometry, and the preview says so.
    assert preview["scene"]["key"] == "data-geometry"


def test_direction_requires_earlier_steps() -> None:
    with pytest.raises(WizardError):
        validate_step("direction", {}, {})


def test_direction_untouched_stores_only_complete() -> None:
    state = _direction_state()
    assert validate_step("direction", _compiled_form(state), state) == {"complete": "1"}


def test_direction_tweaks_are_stored() -> None:
    state = _direction_state()
    preview = wizard_context(state, "direction")["direction_brief"]
    other_harmony = next(
        opt["key"] for opt in preview["harmonies"] if opt["key"] != preview["harmony"]["key"]
    )
    form = _compiled_form(state)
    form["harmony"] = other_harmony
    form["axis_warmth"] = "95" if preview["axes"][0]["value"] != 95 else "5"
    stored = validate_step("direction", form, state)
    assert stored["complete"] == "1"
    assert stored["harmony"] == other_harmony
    assert stored["axis_warmth"] in ("0.95", "0.05")
    # Unchanged fields are not stored: the step stays a sparse diff.
    assert "type_pairing" not in stored
    assert "axis_contrast" not in stored


def test_direction_rejects_bad_values() -> None:
    state = _direction_state()
    with pytest.raises(WizardError):
        validate_step("direction", {"axis_warmth": "warm"}, state)
    with pytest.raises(WizardError):
        validate_step("direction", {"harmony": "not-a-harmony"}, state)
    with pytest.raises(WizardError):
        validate_step("direction", {"scene": "not-a-scene"}, state)


def test_direction_page_renders_and_advances(client: TestClient, home: Path) -> None:
    _post(client, "/design-studio/new/purpose", {"product": "Acme Analytics"})
    _post(
        client,
        "/design-studio/new/business",
        {"industry": "fintech", "offering": "portfolio analytics"},
    )
    _post(client, "/design-studio/new/app_type", {"app_type": "dashboard"})
    _post(client, "/design-studio/new/stack", {"stack": "Web + Tailwind"})
    _post(
        client,
        "/design-studio/new/brand",
        {"name": "acme-brand", "vibe": "precise, trustworthy", "theme": "both"},
    )
    page = client.get("/design-studio/new/direction")
    assert page.status_code == 200
    assert "Which direction should it take?" in page.text
    assert 'name="axis_warmth"' in page.text
    assert 'name="harmony"' in page.text
    assert 'name="avoid"' in page.text
    assert "data-geometry" in page.text
    assert ' style="' not in page.text, "no inline styles on the direction step"
    assert "<script>" not in page.text, "no inline script blocks on the direction step"
    assert "onclick=" not in page.text

    # Tweak the warmth axis and the harmony, then generate: the brief and
    # the generated system must honour both.
    response = _post(
        client,
        "/design-studio/new/direction",
        {"axis_warmth": "95", "harmony": "monochromatic"},
    )
    assert response.request.url.path == "/design-studio/new/generate"
    page = client.get("/design-studio/new/generate")
    assert "Monochromatic" in page.text
    response = _post(client, "/design-studio/new/generate", {})
    assert response.request.url.path == "/design-systems/acme-brand"
    meta = json.loads(
        (home / ".jev" / "design-systems" / "acme-brand" / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["design_brief"]["axes"]["warmth"] == 0.95
    assert meta["design_brief"]["harmony"] == "monochromatic"
    assert meta["design_choices"]["color_strategy"] == "monochromatic"


def test_generate_from_state_without_direction_tweaks(
    home: Path,
) -> None:
    state = _direction_state()
    state["direction"] = {"complete": "1"}
    assert generate_from_state(state) == "acme-brand"
    meta = json.loads(
        (home / ".jev" / "design-systems" / "acme-brand" / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["quality_gates"]["passed"] is True
    assert meta["scene_type"] == "data-geometry"
    assert (home / ".jev" / "design-systems" / "acme-brand" / "motion.js").is_file()
    assert (home / ".jev" / "design-systems" / "acme-brand" / "scene.js").is_file()


def test_generate_falls_back_to_legacy_palette_when_seeded_path_fails(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jev_agent.dashboard.design_studio as studio

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("seeded generation unavailable")

    monkeypatch.setattr(studio, "generate_briefed_system", _boom)
    state = _direction_state()
    state["direction"] = {"complete": "1"}
    assert generate_from_state(state) == "acme-brand"
    meta = json.loads(
        (home / ".jev" / "design-systems" / "acme-brand" / "meta.json").read_text(encoding="utf-8")
    )
    gates = meta["quality_gates"]
    assert gates["fallback"] == "legacy-palette"
    assert "seeded generation unavailable" in str(gates["fallback_reason"])
    assert isinstance(gates["violations"], list)
    assert meta["design_brief"]["harmony"], "the brief is still recorded"
    assert (home / ".jev" / "design-systems" / "acme-brand" / "motion.js").is_file()
    assert (home / ".jev" / "design-systems" / "acme-brand" / "scene.js").is_file()

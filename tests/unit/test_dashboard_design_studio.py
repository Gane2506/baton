"""The Design Studio: the landing, the new-system wizard and its creation pipeline
(issue #124, DS-01)."""

from __future__ import annotations

import html
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
    stack_options,
    validate_step,
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
    """Drive the wizard through Purpose -> App type -> Stack -> Brand."""
    defaults = {
        "product": "Acme Analytics",
        "app_type": "dashboard",
        "stack": "Web + Tailwind",
        "name": "acme-brand",
        "vibe": "warm, confident",
        "theme": "both",
    }
    defaults.update(overrides)
    _post(client, "/design-studio/new")  # starts the wizard at Purpose (POST: it clears state)
    response = _post(client, "/design-studio/new/purpose", {"product": defaults["product"]})
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
    assert first_incomplete_step(state) == "stack"
    full = {
        "purpose": {"product": "x"},
        "app_type": {"app_type": "mobile"},
        "stack": {"stack": "Flutter"},
        "brand": {"name": "acme", "vibe": "", "theme": "both"},
    }
    assert first_incomplete_step(full) is None


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
            ("app_type", "App type"),
            ("stack", "Stack"),
            ("brand", "Brand"),
            ("generate", "Generate"),
        ]
    ]
    assert STUDIO_STEPS == ("purpose", "app_type", "stack", "brand", "generate")


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
        "logo.svg",
        "favicon.svg",
        "favicon-16.png",
        "favicon-32.png",
        "favicon-180.png",
        "favicon-192.png",
        "favicon-512.png",
    ):
        assert (system_dir / filename).is_file(), f"missing {filename}"


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

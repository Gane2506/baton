"""Business context for the Design Studio (issue #283, core half).

Pins the API contract the dashboard worker codes against: `BusinessContext`
validation and meta round-trip, profile keyword matching, element proposal
and selection, business file generation for both codegen targets, the
business catalog, `enabled_element_keys`, and `update_library_meta`.
"""

import html
import io
import json
import zipfile
from pathlib import Path

import pytest

from jev_agent.core.design_business import (
    ELEMENT_CATALOG,
    MAX_CONTEXT_FIELD_LEN,
    MAX_DESCRIPTION_LEN,
    BusinessContext,
    elements_for_keys,
    enabled_element_keys,
    match_profile,
    propose_elements,
)
from jev_agent.core.design_codegen import (
    bundle_zip,
    business_catalog,
    business_files,
)
from jev_agent.core.design_kit import design_system_markdown, palette_for_vibe
from jev_agent.core.design_library import (
    load_library_system,
    save_library_system,
    update_library_meta,
)
from jev_agent.core.design_showcase import vibe_for_keywords
from jev_agent.core.design_showcase_html import render_showcase

TOKENS: dict[str, object] = {
    "colors": {"primary": "#2563EB", "background": "#FFFFFF"},
    "type_scale": {"xs": 12, "base": 15},
    "spacing": {"sm": 8, "md": 16},
    "radii": {"sm": 6, "full": 9999},
}

MARKDOWN = "# Library system\n\nA reusable design system.\n"

# profile key -> (matching keyword, exact proposed element keys)
PROFILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "fintech": (
        "fintech startup",
        ("pricing-card", "stat-card", "transaction-table", "signup-form"),
    ),
    "restaurant": (
        "neighborhood restaurant",
        ("menu-card", "reservation-form", "gallery-card", "hours-card"),
    ),
    "saas": (
        "b2b saas platform",
        ("feature-grid", "testimonial-card", "pricing-table", "onboarding-checklist"),
    ),
    "ecommerce": (
        "online retail store",
        ("product-card", "cart-summary", "checkout-form", "review-card"),
    ),
    "health": (
        "fitness and wellness clinic",
        ("appointment-form", "plan-card", "progress-tracker", "trainer-card"),
    ),
    "education": (
        "online course school",
        ("course-card", "lesson-list", "enrollment-form", "instructor-card"),
    ),
    "travel": (
        "travel booking agency",
        ("destination-card", "booking-form", "itinerary-card", "review-card"),
    ),
}


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at a temp dir so the library never touches the real ~/.jev."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _meta(home: Path, name: str) -> dict[str, object]:
    raw = json.loads((home / ".jev" / "design-systems" / name / "meta.json").read_text())
    assert isinstance(raw, dict)
    return {str(key): value for key, value in raw.items()}


# ---- BusinessContext ---------------------------------------------------------------


def test_context_defaults_are_empty() -> None:
    context = BusinessContext()
    assert context.is_empty()
    assert context.to_meta() == {}


def test_context_to_meta_keeps_only_non_empty() -> None:
    context = BusinessContext(industry="  fintech ", audience="", tone="friendly")
    assert context.to_meta() == {"industry": "fintech", "tone": "friendly"}


def test_context_from_meta_round_trip() -> None:
    context = BusinessContext(
        industry="fintech",
        audience="investors",
        tone="confident",
        offering="trading app",
        description="Trade stocks with zero commission.",
    )
    restored = BusinessContext.from_meta({"business_context": context.to_meta()})
    assert restored == context


@pytest.mark.parametrize(
    "meta",
    [None, {}, {"business_context": {}}, {"business_context": {"industry": "  "}}],
)
def test_from_meta_without_usable_context_is_none(meta: object) -> None:
    assert BusinessContext.from_meta(meta) is None  # type: ignore[arg-type]


def test_from_meta_ignores_unknown_keys() -> None:
    context = BusinessContext.from_meta(
        {"business_context": {"industry": "travel", "surprise": "ignored"}}
    )
    assert context is not None
    assert context.industry == "travel"


def test_from_form_strips_fields() -> None:
    context = BusinessContext.from_form(
        {
            "industry": "  restaurant ",
            "audience": "foodies",
            "tone": "",
            "offering": "",
            "description": "",
        }
    )
    assert context.industry == "restaurant"
    assert context.audience == "foodies"
    assert context.tone == ""


@pytest.mark.parametrize(
    "field,value,limit",
    [
        ("industry", "x" * (MAX_CONTEXT_FIELD_LEN + 1), MAX_CONTEXT_FIELD_LEN),
        ("audience", "x" * (MAX_CONTEXT_FIELD_LEN + 1), MAX_CONTEXT_FIELD_LEN),
        ("tone", "x" * (MAX_CONTEXT_FIELD_LEN + 1), MAX_CONTEXT_FIELD_LEN),
        ("offering", "x" * (MAX_CONTEXT_FIELD_LEN + 1), MAX_CONTEXT_FIELD_LEN),
        ("description", "x" * (MAX_DESCRIPTION_LEN + 1), MAX_DESCRIPTION_LEN),
    ],
)
def test_from_form_too_long_field_raises_naming_field_and_limit(
    field: str, value: str, limit: int
) -> None:
    with pytest.raises(ValueError, match=field):
        BusinessContext.from_form({field: value})
    with pytest.raises(ValueError, match=str(limit)):
        BusinessContext.from_form({field: value})


def test_from_form_at_the_limit_is_accepted() -> None:
    context = BusinessContext.from_form(
        {"industry": "x" * MAX_CONTEXT_FIELD_LEN, "description": "y" * MAX_DESCRIPTION_LEN}
    )
    assert len(context.industry) == MAX_CONTEXT_FIELD_LEN
    assert len(context.description) == MAX_DESCRIPTION_LEN


# ---- profiles ----------------------------------------------------------------------


@pytest.mark.parametrize("profile_key", list(PROFILES))
def test_match_profile_by_keyword(profile_key: str) -> None:
    keyword, _keys = PROFILES[profile_key]
    assert match_profile(BusinessContext(industry=keyword)) == profile_key


@pytest.mark.parametrize(
    "field",
    ["industry", "offering", "description"],
)
def test_match_profile_uses_industry_offering_description(field: str) -> None:
    assert match_profile(BusinessContext(**{field: "a crypto exchange"})) == "fintech"


def test_match_profile_unknown_and_empty() -> None:
    assert match_profile(BusinessContext(industry="underwater basket weaving")) is None
    assert match_profile(BusinessContext()) is None
    # Audience/tone alone do not match: only industry+offering+description count.
    assert match_profile(BusinessContext(audience="crypto traders")) is None


@pytest.mark.parametrize("profile_key", list(PROFILES))
def test_propose_elements_exact_keys(profile_key: str) -> None:
    keyword, keys = PROFILES[profile_key]
    proposed = propose_elements(BusinessContext(industry=keyword))
    assert [element.key for element in proposed] == list(keys)


def test_propose_elements_empty_and_unknown() -> None:
    assert propose_elements(BusinessContext()) == []
    assert propose_elements(BusinessContext(industry="underwater basket weaving")) == []


def test_elements_for_keys_skips_unknown_and_preserves_order() -> None:
    elements = elements_for_keys(["review-card", "nope", "pricing-card", "also-nope"])
    assert [element.key for element in elements] == ["review-card", "pricing-card"]


def test_catalog_covers_every_profile_element_and_layouts_are_valid() -> None:
    layouts = {"card", "form", "table", "stat", "grid", "quote", "checklist"}
    for _profile_key, (_keyword, keys) in PROFILES.items():
        for key in keys:
            element = ELEMENT_CATALOG[key]
            assert element.layout in layouts
            assert element.title and element.description and element.why
            assert element.data, f"{key} has no data"
    # The shared review card is one catalog entry used by two profiles.
    assert ELEMENT_CATALOG["review-card"].layout == "quote"


def test_catalog_data_shapes_match_layouts() -> None:
    for element in ELEMENT_CATALOG.values():
        data = element.data
        if element.layout == "card":
            assert isinstance(data["price"], str) and isinstance(data["price_label"], str)
            assert isinstance(data["cta"], str) and isinstance(data["points"], list)
        elif element.layout == "form":
            assert isinstance(data["fields"], list) and isinstance(data["submit"], str)
            for field in data["fields"]:
                assert {"name", "label", "type", "placeholder"} <= set(field)
        elif element.layout == "table":
            assert isinstance(data["columns"], list) and isinstance(data["rows"], list)
        elif element.layout == "stat":
            assert isinstance(data["value"], str) and isinstance(data["label"], str)
            assert isinstance(data["change"], str) and isinstance(data["progress"], int)
        elif element.layout == "grid":
            assert isinstance(data["items"], list)
            for item in data["items"]:
                assert {"title", "text"} <= set(item)
        elif element.layout == "quote":
            assert {"text", "author", "role"} <= set(data)
        elif element.layout == "checklist":
            assert isinstance(data["items"], list)


# ---- business_files / business_catalog ---------------------------------------------


@pytest.mark.parametrize("target", ["html-css", "react-tailwind"])
def test_business_files_empty_elements_is_empty(target: str) -> None:
    assert business_files(target, "Acme", TOKENS, []) == {}
    assert business_catalog(target, []) == []


def test_business_files_html_css() -> None:
    elements = propose_elements(BusinessContext(industry="fintech startup"))
    files = business_files("html-css", "Acme", TOKENS, elements)
    assert set(files) == {"business-elements.html", "business-elements.css"}
    html_page = files["business-elements.html"]
    for element in elements:
        assert element.title in html_page
    assert ".price-value" in files["business-elements.css"]
    # No Python dict reprs leaked into the output.
    assert "'price':" not in html_page
    assert "{'price'" not in html_page


def test_business_files_react_tailwind() -> None:
    elements = propose_elements(BusinessContext(industry="saas platform"))
    files = business_files("react-tailwind", "Acme", TOKENS, elements)
    assert set(files) == {"business-components.jsx"}
    jsx = files["business-components.jsx"]
    for element in elements:
        assert element.title in jsx
        component = "".join(part.capitalize() for part in element.key.split("-"))
        assert f"export function {component}" in jsx
    assert "from './components'" in jsx
    assert "{'columns'" not in jsx
    assert "{'col" not in jsx


def test_business_files_escapes_hostile_system_name() -> None:
    elements = propose_elements(BusinessContext(industry="travel booking"))
    evil = 'Evil */ <script>alert("x")</script> & "quoted"'
    html_files = business_files("html-css", evil, TOKENS, elements)
    # The demo page escapes the name as HTML text: no live script, no raw name.
    assert "<script>alert" not in html_files["business-elements.html"]
    assert evil not in html_files["business-elements.html"]
    # Comments carry the comment-safe name: the `*/` terminator is neutralized
    # and the comment delimiters stay balanced (a `<script>` inside a CSS/JS
    # comment is inert, exactly as the base codegen treats names).
    css = html_files["business-elements.css"]
    assert "* /" in css
    assert css.count("/*") == css.count("*/")
    jsx_files = business_files("react-tailwind", evil, TOKENS, elements)
    jsx = jsx_files["business-components.jsx"]
    assert "* /" in jsx
    assert jsx.count("/*") == jsx.count("*/")


def test_business_files_all_profiles_generate_valid_output() -> None:
    """Every profile's elements render for both targets without dict reprs."""
    for keyword, _keys in PROFILES.values():
        elements = propose_elements(BusinessContext(industry=keyword))
        assert elements
        for target in ("html-css", "react-tailwind"):
            files = business_files(target, "Acme", TOKENS, elements)
            assert files
            for element in elements:
                # Titles are escaped in the output ("&" -> "&amp;"; quotes/apostrophes stay raw).
                assert any(
                    html.escape(element.title, quote=False) in content for content in files.values()
                )
            for content in files.values():
                assert "'price':" not in content
                assert "{'columns'" not in content


@pytest.mark.parametrize("target", ["html-css", "react-tailwind"])
def test_business_catalog_matches_business_files(target: str) -> None:
    elements = propose_elements(BusinessContext(industry="restaurant"))
    files = business_files(target, "Acme", TOKENS, elements)
    catalog = business_catalog(target, elements)
    assert [entry["filename"] for entry in catalog] == list(files)
    for entry in catalog:
        assert entry["title"] and entry["description"]


def test_bundle_zip_includes_business_files() -> None:
    elements = propose_elements(BusinessContext(industry="fintech startup"))
    data = bundle_zip("html-css", "Acme", TOKENS, business_elements=elements)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert any(name.endswith("business-elements.html") for name in names)
    assert any(name.endswith("business-elements.css") for name in names)
    assert any(name.endswith("index.html") for name in names)
    # Without elements the zip has no business files.
    plain = bundle_zip("html-css", "Acme", TOKENS)
    plain_names = zipfile.ZipFile(io.BytesIO(plain)).namelist()
    assert not any("business-elements" in name for name in plain_names)


# ---- enabled_element_keys ----------------------------------------------------------


def test_enabled_element_keys_stored_list_wins() -> None:
    context = BusinessContext(industry="fintech startup")
    meta = {"business_elements": ["review-card", "pricing-card"]}
    assert enabled_element_keys(meta, context) == ["review-card", "pricing-card"]


def test_enabled_element_keys_stored_list_filters_unknown() -> None:
    context = BusinessContext(industry="fintech startup")
    meta = {"business_elements": ["pricing-card", "nope", 42]}
    assert enabled_element_keys(meta, context) == ["pricing-card"]


def test_enabled_element_keys_absent_means_proposed() -> None:
    context = BusinessContext(industry="health clinic")
    assert enabled_element_keys({}, context) == [
        "appointment-form",
        "plan-card",
        "progress-tracker",
        "trainer-card",
    ]
    assert enabled_element_keys(None, context) == [
        "appointment-form",
        "plan-card",
        "progress-tracker",
        "trainer-card",
    ]


def test_enabled_element_keys_no_context_is_empty() -> None:
    assert enabled_element_keys({}, None) == []
    assert enabled_element_keys(None, None) == []
    assert enabled_element_keys({}, BusinessContext()) == []


def test_enabled_element_keys_empty_stored_list_means_none() -> None:
    context = BusinessContext(industry="fintech startup")
    assert enabled_element_keys({"business_elements": []}, context) == []


# ---- update_library_meta -----------------------------------------------------------


def test_update_library_meta_patches_and_deletes(home: Path) -> None:
    save_library_system("acme", MARKDOWN, TOKENS, {"app_type": "website"})
    before = _meta(home, "acme")
    version = before["version"]
    updated_at = before["updated_at"]
    result = update_library_meta(
        "acme",
        {"business_context": {"industry": "fintech"}, "app_type": None},
    )
    assert result is not None
    assert result["business_context"] == {"industry": "fintech"}
    assert "app_type" not in result
    # No version bump, no updated_at change.
    assert result["version"] == version
    assert result["updated_at"] == updated_at
    assert _meta(home, "acme") == result
    # The system itself is untouched.
    assert load_library_system("acme") is not None


def test_update_library_meta_missing_system_is_none(home: Path) -> None:
    assert update_library_meta("ghost", {"business_context": {"industry": "x"}}) is None


def test_update_library_meta_invalid_name_raises(home: Path) -> None:
    with pytest.raises(ValueError, match="invalid library design system name"):
        update_library_meta("../escape", {"x": 1})
    with pytest.raises(ValueError, match="invalid library design system name"):
        update_library_meta("", {"x": 1})


def test_update_library_meta_round_trip_with_context(home: Path) -> None:
    save_library_system("acme", MARKDOWN, TOKENS)
    context = BusinessContext(industry="travel", audience="backpackers")
    update_library_meta("acme", {"business_context": context.to_meta()})
    restored = BusinessContext.from_meta(_meta(home, "acme"))
    assert restored == context


# ---- markdown + showcase wiring ----------------------------------------------------


def test_design_system_markdown_with_business() -> None:
    palette = palette_for_vibe("fintech")
    plain = design_system_markdown("Acme", "website", "Web + Tailwind", palette, "light")
    assert "Business context" not in plain
    context = BusinessContext(industry="fintech", audience="investors", offering="trading app")
    marked = design_system_markdown(
        "Acme", "website", "Web + Tailwind", palette, "light", business=context
    )
    assert "## Business context" in marked
    assert "fintech" in marked
    assert "Pricing plans" in marked
    # An empty context adds no section.
    empty = design_system_markdown(
        "Acme", "website", "Web + Tailwind", palette, "light", business=BusinessContext()
    )
    assert "Business context" not in empty


def test_render_showcase_business_line() -> None:
    vibe = vibe_for_keywords("fintech")
    tokens: dict[str, object] = {"colors": {"primary": "#2563EB", "background": "#FFFFFF"}}
    plain = render_showcase("Acme", vibe, tokens)
    assert "Built for" not in plain
    html_page = render_showcase("Acme", vibe, tokens, business=BusinessContext(industry="fintech"))
    assert "Built for fintech" in html_page
    evil = render_showcase(
        "Acme", vibe, tokens, business=BusinessContext(industry="<script>alert(1)</script>")
    )
    assert "<script>alert(1)</script>" not in evil
    assert "Built for &lt;script&gt;" in evil

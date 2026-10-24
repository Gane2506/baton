"""The Design Studio's server-side wizard state and creation pipeline (issue #124).

The studio is a standalone creation interface at `/design-studio`, independent
from the main chat: a guided stepper (Purpose -> App type -> Stack -> Brand ->
Generate) that mirrors the `jev design new` CLI wizard's six questions. The
dashboard keeps each visitor's answers in a session-keyed store (the same shape
as the flash store in `dashboard/app.py`), and the final step runs the
`core/design_kit.py` pipeline and saves with `save_library_system()`.

Nothing here touches credentials or the network; the state never leaves the
dashboard process.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

from jev_agent.core.design_kit import (
    APP_TYPES,
    STACKS,
    THEMES,
    design_system_markdown,
    palette_for_vibe,
    tokens_for_app_type,
)
from jev_agent.core.design_library import (
    library_system_path,
    list_library,
    save_library_system,
    validate_library_name,
)
from jev_agent.core.design_showcase import VIBE_PROFILES, rich_tokens, vibe_for_keywords
from jev_agent.core.design_showcase_html import render_showcase
from jev_agent.core.logo import generate_mark, mark_template, mark_to_png, mark_to_svg
from jev_agent.dashboard.studio_decisions import (
    STACK_REC_KEY,
    StackRecommendation,
    default_recommendation,
)

STUDIO_STEPS: tuple[str, ...] = ("purpose", "app_type", "stack", "brand", "generate")
"""The wizard's steps in order (mirrors the stepper labels on the page)."""

STEP_LABELS: dict[str, str] = {
    "purpose": "Purpose",
    "app_type": "App type",
    "stack": "Stack",
    "brand": "Brand",
    "generate": "Generate",
}

_MAX_PRODUCT_LEN = 120
_MAX_VIBE_LEN = 200


class WizardError(ValueError):
    """A wizard answer failed validation; the message is safe to show the user."""


def _str(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    return value.strip() if isinstance(value, str) else ""


def stack_options(app_type: str, recommendation: str | None = None) -> tuple[list[str], str]:
    """The stack choices for `app_type`: the ordered list and the recommended one.

    The recommendation comes first. DS-03: the wizard passes the cached Jev
    pick as `recommendation`; without one (or when it is not a listed stack) the
    recommendation is the design kit's static default — the first entry of
    `STACKS[app_type]`, like the CLI wizard's.
    """
    stacks = list(STACKS[app_type])
    first = stacks[0]
    if recommendation is not None and recommendation in stacks:
        first = recommendation
    return [first, *(s for s in stacks if s != first)], first


class StudioWizard:
    """Server-side wizard state, keyed by dashboard session.

    Mirrors the flash store's shape (bounded, TTL-pruned): the session cookie
    is attacker-influenceable, so a session that starts the wizard but never
    finishes must not grow the store without bound.
    """

    _TTL_S = 3600
    _MAX_SESSIONS = 200

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, dict[str, dict[str, str]]] = {}
        self._seen: dict[str, float] = {}

    def get(self, session: str) -> dict[str, dict[str, str]]:
        """This session's answers so far (`{step: {field: value}}`), possibly {}."""
        with self._lock:
            self._prune()
            return {step: dict(data) for step, data in self._states.get(session, {}).items()}

    def put(self, session: str, step: str, data: dict[str, str]) -> None:
        """Record a validated step's answers for this session."""
        with self._lock:
            self._prune()
            state = self._states.setdefault(session, {})
            state[step] = dict(data)
            self._seen[session] = time.monotonic()
            # Bound the store: drop the oldest sessions first.
            while len(self._states) > self._MAX_SESSIONS:
                oldest = min(self._seen, key=lambda key: self._seen[key])
                del self._states[oldest]
                del self._seen[oldest]

    def clear(self, session: str) -> None:
        """Forget a session's answers (start over, or a finished generation)."""
        with self._lock:
            self._states.pop(session, None)
            self._seen.pop(session, None)

    def _prune(self) -> None:
        """Drop sessions idle past the TTL (called with the lock held)."""
        cutoff = time.monotonic() - self._TTL_S
        for session in [key for key, seen in self._seen.items() if seen < cutoff]:
            del self._seen[session]
            self._states.pop(session, None)


def first_incomplete_step(state: Mapping[str, Mapping[str, str]]) -> str | None:
    """The earliest step with no answers yet (None when the wizard is complete)."""
    for step in STUDIO_STEPS:
        if step != "generate" and not state.get(step):
            return step
    return None


def validate_step(
    step: str, form: Mapping[str, object], state: Mapping[str, Mapping[str, str]]
) -> dict[str, str]:
    """Validate one wizard step's POSTed fields against the answers so far.

    Returns the step's answers to store; raises WizardError with a user-facing
    message when the step cannot be advanced. Earlier steps must already be
    complete, so a forged POST cannot skip ahead.
    """
    if step == "purpose":
        return _validate_purpose(form)
    if step == "app_type":
        return _validate_app_type(form)
    if step == "stack":
        return _validate_stack(form, state)
    if step == "brand":
        return _validate_brand(form)
    raise WizardError(f"unknown wizard step {step!r}")


def _require_earlier(state: Mapping[str, Mapping[str, str]], *steps: str) -> None:
    for earlier in steps:
        if not state.get(earlier):
            raise WizardError(f"answer {STEP_LABELS[earlier]!r} first, then come back to this step")


def _validate_purpose(form: Mapping[str, object]) -> dict[str, str]:
    """Step 1 (easy first): the product name — free text, one question."""
    product = _str(form, "product")
    if not product:
        raise WizardError("Give the product a name so the studio knows what it is designing for.")
    if len(product) > _MAX_PRODUCT_LEN:
        raise WizardError(f"Keep the product name under {_MAX_PRODUCT_LEN} characters.")
    return {"product": product}


def _validate_app_type(form: Mapping[str, object]) -> dict[str, str]:
    """Step 2: the app type — one choice from the design kit's four."""
    app_type = _str(form, "app_type")
    if app_type not in APP_TYPES:
        raise WizardError("Pick one of the four app types to continue.")
    return {"app_type": app_type}


def _validate_stack(
    form: Mapping[str, object], state: Mapping[str, Mapping[str, str]]
) -> dict[str, str]:
    """Step 3: the stack — the recommended option is listed first."""
    _require_earlier(state, "app_type")
    app_type = state["app_type"]["app_type"]
    stack = _str(form, "stack")
    options, _recommended = stack_options(app_type)
    if stack not in options:
        raise WizardError("Pick one of the listed stacks to continue.")
    return {"stack": stack}


def _validate_brand(form: Mapping[str, object]) -> dict[str, str]:
    """Step 4: the brand — system name (validated), vibe profile + keywords, theme."""
    name = _str(form, "name")
    try:
        validate_library_name(name)
    except ValueError as exc:
        raise WizardError(str(exc)) from exc
    if name in list_library():
        raise WizardError(
            f'a design system named "{name}" already exists in the library — '
            "pick a different name (the studio only creates new systems)"
        )
    vibe_profile = _str(form, "vibe_profile")
    if vibe_profile and vibe_profile not in VIBE_PROFILES:
        raise WizardError("Pick one of the listed vibes.")
    vibe = _str(form, "vibe")
    if len(vibe) > _MAX_VIBE_LEN:
        raise WizardError(f"Keep the vibe keywords under {_MAX_VIBE_LEN} characters.")
    theme = _str(form, "theme") or "both"
    if theme not in THEMES:
        raise WizardError("Pick a theme: light, dark, or both.")
    return {"name": name, "vibe": vibe, "vibe_profile": vibe_profile, "theme": theme}


def generate_from_state(state: Mapping[str, Mapping[str, str]]) -> str:
    """Run the design-kit pipeline and save the system into the user library.

    Mirrors `jev design new`'s sequence exactly (tokens -> markdown -> save ->
    logo/favicons). Returns the system name; raises WizardError when the name
    is taken (a race between the Brand step and generation) or the answers are
    incomplete.
    """
    if first_incomplete_step(state) is not None:
        raise WizardError("Finish every wizard step before generating.")
    purpose = state["purpose"]
    app_type = state["app_type"]["app_type"]
    stack = state["stack"]["stack"]
    brand = state["brand"]
    name = brand["name"]
    product = purpose["product"]
    vibe = brand["vibe"]
    theme = brand["theme"]

    try:
        validate_library_name(name)
    except ValueError as exc:
        raise WizardError(str(exc)) from exc
    if name in list_library():
        raise WizardError(
            f'a design system named "{name}" already exists in the library — '
            "pick a different name (the studio only creates new systems)"
        )

    palette = palette_for_vibe(vibe)
    tokens = tokens_for_app_type(app_type, palette, theme)
    # Rich tokens (issue #155): the full design-token set — ramps, dark pairs,
    # typography, shadows, motion — merged under "design_tokens" so legacy
    # consumers (exports, prompts) keep reading the flat keys untouched.
    vibe_profile = VIBE_PROFILES.get(brand.get("vibe_profile", "")) or vibe_for_keywords(vibe)
    tokens["design_tokens"] = rich_tokens(vibe_profile, theme)
    markdown = design_system_markdown(name, app_type, stack, palette, theme)
    seed = f"{name}:{product}"
    template = mark_template(seed)
    meta_extra: dict[str, object] = {
        "name": name,
        "app_type": app_type,
        "stack": stack,
        "source": "studio",
        "product_name": product,
        "theme": theme,
        "vibe": vibe,
        "vibe_profile": vibe_profile.key,
        "logo_template": template,
    }
    save_library_system(name, markdown, tokens, meta_extra=meta_extra)

    # Showcase gallery (issue #155): a stunning standalone preview saved
    # alongside the system.
    showcase_html = render_showcase(name, vibe_profile, tokens["design_tokens"], theme)  # type: ignore[arg-type]
    system_path = library_system_path(name)
    (system_path / "showcase.html").write_bytes(showcase_html.encode("utf-8"))

    mark = generate_mark(seed, palette)
    assets: dict[str, bytes] = {
        "logo.svg": mark_to_svg(mark, 512).encode("utf-8"),
        "favicon.svg": mark_to_svg(mark, 64).encode("utf-8"),
        "favicon-16.png": mark_to_png(mark, 16),
        "favicon-32.png": mark_to_png(mark, 32),
        "favicon-180.png": mark_to_png(mark, 180),
        "favicon-192.png": mark_to_png(mark, 192),
        "favicon-512.png": mark_to_png(mark, 512),
    }
    for filename, data in assets.items():
        (system_path / filename).write_bytes(data)
    return name


def wizard_context(
    session_state: Mapping[str, Mapping[str, str]],
    step: str,
    entered: Mapping[str, object] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The shared template context for every wizard step page.

    `entered` holds the raw POSTed values when a step re-renders after a
    validation error, so the user's input is not lost.
    """
    done = {name for name in STUDIO_STEPS if name != "generate" and session_state.get(name)}
    current = STUDIO_STEPS.index(step)
    steps = [
        {
            "key": name,
            "label": STEP_LABELS[name],
            "done": name in done,
            "current": name == step,
            "index": index + 1,
        }
        for index, name in enumerate(STUDIO_STEPS)
    ]
    context: dict[str, Any] = {
        "wizard_steps": steps,
        "wizard_step": step,
        "wizard_step_index": current,
        "wizard_last": step == STUDIO_STEPS[-1],
        "answers": {
            name: {
                **dict(session_state.get(name, {})),
                **(
                    {key: str(value) for key, value in entered.items()}
                    if entered is not None and name == step
                    else {}
                ),
            }
            for name in STUDIO_STEPS
        },
        "app_types": APP_TYPES,
        "themes": THEMES,
        "vibe_profiles": [
            {"key": p.key, "name": p.name, "tagline": p.tagline, "description": p.description}
            for p in VIBE_PROFILES.values()
        ],
        **extra,
    }
    if step == "stack":
        # Issue #200: a POST to /stack with no prior app_type fails validation
        # (WizardError) and then re-renders this context — the old direct index
        # raised KeyError here, turning the friendly error into a bare 500.
        # With no app type yet there is nothing to list: render the step with
        # empty options and let the error message (and the step page's
        # incomplete-step redirect) send the visitor back to App type.
        app_type = (session_state.get("app_type") or {}).get("app_type")
        if app_type is None:
            context["stack_options"] = []
            context["stack_recommended"] = ""
            context["stack_app_type"] = ""
            context["stack_app_type_label"] = ""
            context["stack_rec"] = None
            return context
        # The Jev pick, decided once when the App type step was submitted and
        # cached in the wizard state; without a cache the static default applies.
        cached = StackRecommendation.from_state(session_state.get(STACK_REC_KEY))
        rec = cached or default_recommendation(app_type, "not_asked")
        options, recommended = stack_options(app_type, cached.stack if cached else None)
        context["stack_options"] = options
        context["stack_recommended"] = recommended
        context["stack_app_type"] = app_type
        context["stack_app_type_label"] = APP_TYPES[app_type]
        context["stack_rec"] = {
            "source": rec.source,
            "rationale": rec.rationale,
        }
    return context

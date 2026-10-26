"""Business context for the Design Studio (issue #283).

The studio used to generate the same fixed set of reusable components for
every system. Business context (industry, audience, tone, offering,
description) lets it propose and generate the elements a business actually
needs: a restaurant gets a menu card and a reservation form, a fintech gets
a pricing card and a transaction table.

This module owns the model (`BusinessContext`), the industry profiles
(keyword matching in the style of `design_kit._VIBE_FAMILIES`), the element
catalog, and the proposal/selection helpers. Code generation for the
elements lives in `design_codegen` (HTML/CSS) and `design_codegen_react`
(React/Tailwind); the dashboard wires the two together.

Business context text itself is never rendered into generated code — only
the catalog's fixed element content is. The context only selects *which*
elements are proposed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import BaseModel

MAX_CONTEXT_FIELD_LEN = 500
MAX_DESCRIPTION_LEN = 2000

_CONTEXT_FIELDS: tuple[str, ...] = ("industry", "audience", "tone", "offering", "description")
_FIELD_LIMITS: dict[str, int] = {field: MAX_CONTEXT_FIELD_LEN for field in _CONTEXT_FIELDS}
_FIELD_LIMITS["description"] = MAX_DESCRIPTION_LEN


class BusinessContext(BaseModel):
    """What the business does, who it serves, and how it talks about itself.

    All fields are plain strings defaulting to blank; `is_empty()` is True
    when every field is blank.
    """

    industry: str = ""
    audience: str = ""
    tone: str = ""
    offering: str = ""
    description: str = ""

    def is_empty(self) -> bool:
        """True when every field is blank (whitespace counts as blank)."""
        return not any(getattr(self, field).strip() for field in _CONTEXT_FIELDS)

    def to_meta(self) -> dict[str, str]:
        """Only the non-empty fields, stripped — the shape stored in meta.json."""
        return {
            field: value.strip()
            for field in _CONTEXT_FIELDS
            if (value := getattr(self, field).strip())
        }

    @classmethod
    def from_meta(cls, meta: Mapping[str, object] | None) -> BusinessContext | None:
        """Rebuild from a meta dict, or None when there is no usable context.

        None when `meta` is None, when it carries no `business_context` dict,
        when that dict is empty, or when every value in it is blank.
        Non-string values are stringified; unknown keys are ignored.
        """
        if not isinstance(meta, Mapping):
            return None
        raw = meta.get("business_context")
        if not isinstance(raw, Mapping) or not raw:
            return None
        values = {
            field: str(raw[field]).strip()
            for field in _CONTEXT_FIELDS
            if field in raw and raw[field] is not None
        }
        if not any(values.values()):
            return None
        return cls(**values)

    @classmethod
    def from_form(cls, form: Mapping[str, object]) -> BusinessContext:
        """Build from wizard form data: the five fields as stripped strings.

        Raises ValueError naming the field and its limit when any field
        exceeds its length cap (500 chars, 2000 for `description`).
        """
        values: dict[str, str] = {}
        for field in _CONTEXT_FIELDS:
            raw = form.get(field, "")
            value = "" if raw is None else str(raw).strip()
            limit = _FIELD_LIMITS[field]
            if len(value) > limit:
                raise ValueError(
                    f"business context field {field!r} is {len(value)} characters, "
                    f"over the {limit}-character limit"
                )
            values[field] = value
        return cls(**values)


class BusinessElement(BaseModel):
    """One business-specific reusable element.

    `layout` picks the rendering shape; `data` carries the fixed content in
    the exact shape for that layout (see the module docstring of the
    profiles below). The content here is fixed catalog copy, never
    user-supplied text.
    """

    key: str
    title: str
    description: str
    why: str
    layout: str
    data: dict[str, object] = {}


def _element(
    key: str,
    title: str,
    description: str,
    why: str,
    layout: str,
    data: dict[str, object],
) -> BusinessElement:
    return BusinessElement(
        key=key, title=title, description=description, why=why, layout=layout, data=data
    )


ELEMENT_CATALOG: dict[str, BusinessElement] = {
    # -- fintech ------------------------------------------------------------------
    "pricing-card": _element(
        "pricing-card",
        "Pricing plans",
        "Tiered plans with a clear monthly price and a single call to action.",
        "Investors compare plans before opening an account — clear pricing builds trust.",
        "card",
        {
            "price": "$29",
            "price_label": "per month",
            "cta": "Open account",
            "points": [
                "No hidden fees",
                "Real-time market data",
                "Bank-level security",
            ],
        },
    ),
    "stat-card": _element(
        "stat-card",
        "Portfolio at a glance",
        "The headline number, its change, and progress toward the monthly goal.",
        "Customers check one number first — the portfolio total — so it leads the page.",
        "stat",
        {
            "value": "$48,290",
            "label": "Total portfolio value",
            "change": "+12.4% this month",
            "progress": 72,
        },
    ),
    "transaction-table": _element(
        "transaction-table",
        "Recent transactions",
        "A dated list of money in and out, with amounts aligned for scanning.",
        "Trust in a finance product is an auditable history — every movement visible.",
        "table",
        {
            "columns": ["Date", "Description", "Amount"],
            "rows": [
                ["Oct 24, 2026", "Direct deposit — Payroll", "+$2,450.00"],
                ["Oct 23, 2026", "Transfer to Savings", "-$500.00"],
                ["Oct 22, 2026", "Card purchase — Grocery", "-$86.40"],
            ],
        },
    ),
    "signup-form": _element(
        "signup-form",
        "Create an account",
        "The shortest path from visitor to funded account.",
        "Every extra field drops signups — this asks only what onboarding needs.",
        "form",
        {
            "fields": [
                {
                    "name": "full_name",
                    "label": "Full name",
                    "type": "text",
                    "placeholder": "Jane Cooper",
                },
                {
                    "name": "email",
                    "label": "Email address",
                    "type": "email",
                    "placeholder": "jane@example.com",
                },
                {
                    "name": "password",
                    "label": "Password",
                    "type": "password",
                    "placeholder": "Create a password",
                },
            ],
            "submit": "Create account",
        },
    ),
    # -- restaurant ---------------------------------------------------------------
    "menu-card": _element(
        "menu-card",
        "Signature dish",
        "One dish, priced and described, with the reasons to order it.",
        "Diners decide on one dish first — the signature plate sells the menu.",
        "card",
        {
            "price": "$24",
            "price_label": "signature dish",
            "cta": "View full menu",
            "points": [
                "Wood-fired seasonal dishes",
                "Vegetarian and vegan options",
                "Locally sourced ingredients",
            ],
        },
    ),
    "reservation-form": _element(
        "reservation-form",
        "Book a table",
        "Date, time, party size, and a name — nothing more between guest and seat.",
        "A reservation is the restaurant's conversion — it must take under a minute.",
        "form",
        {
            "fields": [
                {"name": "date", "label": "Date", "type": "date", "placeholder": ""},
                {"name": "time", "label": "Time", "type": "time", "placeholder": ""},
                {
                    "name": "guests",
                    "label": "Guests",
                    "type": "number",
                    "placeholder": "2",
                },
                {
                    "name": "name",
                    "label": "Name",
                    "type": "text",
                    "placeholder": "Jane Cooper",
                },
            ],
            "submit": "Book a table",
        },
    ),
    "gallery-card": _element(
        "gallery-card",
        "From the kitchen",
        "A grid of dishes and moments that show what the room feels like.",
        "People eat with their eyes first — photos do the persuading words cannot.",
        "grid",
        {
            "items": [
                {"title": "Wood-fired oven", "text": "Every flatbread finished over open flame."},
                {
                    "title": "Seasonal specials",
                    "text": "The menu follows the market, week by week.",
                },
                {"title": "The dining room", "text": "Warm light, long tables, room to linger."},
            ],
        },
    ),
    "hours-card": _element(
        "hours-card",
        "Hours & location",
        "When the kitchen is open and how to find the door.",
        "The second question every guest asks is when you are open — answer it upfront.",
        "card",
        {
            "price": "11:00 AM - 10:00 PM",
            "price_label": "kitchen hours",
            "cta": "Get directions",
            "points": [
                "Mon-Thu 11 AM - 10 PM",
                "Fri-Sat 11 AM - 11 PM",
                "Sun 10 AM - 9 PM",
            ],
        },
    ),
    # -- saas ---------------------------------------------------------------------
    "feature-grid": _element(
        "feature-grid",
        "What you get",
        "The core capabilities, each in one line, so a buyer can scan the value.",
        "B2B buyers scan features before pricing — the grid is the pitch.",
        "grid",
        {
            "items": [
                {"title": "Automations", "text": "Recurring work runs itself once you set it up."},
                {"title": "Team workspaces", "text": "Shared projects with roles and permissions."},
                {"title": "Integrations", "text": "Connects with the tools you already use."},
                {"title": "Reports", "text": "Usage and outcomes, exportable in one click."},
            ],
        },
    ),
    "testimonial-card": _element(
        "testimonial-card",
        "What customers say",
        "A named quote from a customer in the buyer's own role.",
        "A peer's words carry more weight than any feature list for a B2B buyer.",
        "quote",
        {
            "text": "We rolled it out to the whole team in a week — and cancelled two other tools.",
            "author": "Maya Chen",
            "role": "Operations Lead, Northwind",
        },
    ),
    "pricing-table": _element(
        "pricing-table",
        "Plans compared",
        "Every plan side by side, so the right tier picks itself.",
        "Subscription buyers compare tiers — the table makes the upgrade path obvious.",
        "table",
        {
            "columns": ["Plan", "Price", "Users", "Support"],
            "rows": [
                ["Starter", "$0", "Up to 3", "Community"],
                ["Team", "$29 / mo", "Up to 25", "Email"],
                ["Business", "$99 / mo", "Unlimited", "Priority"],
            ],
        },
    ),
    "onboarding-checklist": _element(
        "onboarding-checklist",
        "Get set up",
        "The first-run steps, in order, so a new workspace is live in minutes.",
        "Trial users churn when setup stalls — a checklist turns it into progress.",
        "checklist",
        {
            "items": [
                "Create your workspace",
                "Invite your team",
                "Connect your first integration",
                "Launch your first project",
            ],
        },
    ),
    # -- ecommerce ----------------------------------------------------------------
    "product-card": _element(
        "product-card",
        "Featured product",
        "One product with price, shipping note, and the reasons to buy it.",
        "Shoppers decide on one product at a time — the card is the storefront.",
        "card",
        {
            "price": "$49.99",
            "price_label": "free shipping over $75",
            "cta": "Add to cart",
            "points": [
                "Free returns within 30 days",
                "Ships in 24 hours",
                "2-year warranty included",
            ],
        },
    ),
    "cart-summary": _element(
        "cart-summary",
        "Your cart",
        "Line items with quantities and prices, so the total is never a surprise.",
        "Cart abandonment starts with surprise totals — the summary keeps it honest.",
        "table",
        {
            "columns": ["Item", "Qty", "Price"],
            "rows": [
                ["Canvas Tote", "1", "$29.99"],
                ["Ceramic Mug", "2", "$40.00"],
                ["Shipping", "—", "Free"],
            ],
        },
    ),
    "checkout-form": _element(
        "checkout-form",
        "Checkout",
        "Contact, shipping, and payment in one short form.",
        "Every extra checkout step costs orders — this keeps it to one page.",
        "form",
        {
            "fields": [
                {
                    "name": "full_name",
                    "label": "Full name",
                    "type": "text",
                    "placeholder": "Jane Cooper",
                },
                {
                    "name": "address",
                    "label": "Shipping address",
                    "type": "text",
                    "placeholder": "123 Main Street",
                },
                {
                    "name": "card_number",
                    "label": "Card number",
                    "type": "text",
                    "placeholder": "4242 4242 4242 4242",
                },
            ],
            "submit": "Place order",
        },
    ),
    "review-card": _element(
        "review-card",
        "Customer review",
        "A verified customer's words about the experience.",
        "Real customer voices build the trust that turns browsers into buyers.",
        "quote",
        {
            "text": (
                "Arrived in two days, beautifully packed — "
                "and the quality is better than the photos."
            ),
            "author": "Alex Rivera",
            "role": "Verified buyer",
        },
    ),
    # -- health -------------------------------------------------------------------
    "appointment-form": _element(
        "appointment-form",
        "Book an appointment",
        "Name, service, and a date — the front desk, online.",
        "Patients book when the moment strikes — the form must be there at midnight too.",
        "form",
        {
            "fields": [
                {
                    "name": "full_name",
                    "label": "Full name",
                    "type": "text",
                    "placeholder": "Jane Cooper",
                },
                {
                    "name": "service",
                    "label": "Service",
                    "type": "text",
                    "placeholder": "e.g. Initial consultation",
                },
                {"name": "date", "label": "Preferred date", "type": "date", "placeholder": ""},
            ],
            "submit": "Book appointment",
        },
    ),
    "plan-card": _element(
        "plan-card",
        "Membership plan",
        "One plan, priced monthly, with what it includes.",
        "Wellness sells as a plan, not a visit — the card makes joining the default.",
        "card",
        {
            "price": "$59",
            "price_label": "per month",
            "cta": "Start your plan",
            "points": [
                "Unlimited classes",
                "Monthly health check-in",
                "Cancel anytime",
            ],
        },
    ),
    "progress-tracker": _element(
        "progress-tracker",
        "Your progress",
        "This week's goal as a number, a change, and a bar that fills.",
        "Seeing progress is what keeps members coming back — the bar makes it visible.",
        "stat",
        {
            "value": "78%",
            "label": "Weekly goal progress",
            "change": "+6% vs last week",
            "progress": 78,
        },
    ),
    "trainer-card": _element(
        "trainer-card",
        "Meet your coach",
        "The person behind the plan, with credentials that build confidence.",
        "People commit to a coach, not a program — put a face on the plan.",
        "card",
        {
            "price": "Coach Maya",
            "price_label": "Strength & conditioning",
            "cta": "Meet your coach",
            "points": [
                "10+ years coaching",
                "Certified nutrition coach",
                "1:1 and small-group sessions",
            ],
        },
    ),
    # -- education ----------------------------------------------------------------
    "course-card": _element(
        "course-card",
        "Featured course",
        "One course with its price and what the student walks away with.",
        "Students enrol in one course at a time — the card is the catalogue's front door.",
        "card",
        {
            "price": "$199",
            "price_label": "per course",
            "cta": "Enroll now",
            "points": [
                "12 weeks, self-paced",
                "Certificate on completion",
                "Lifetime access to materials",
            ],
        },
    ),
    "lesson-list": _element(
        "lesson-list",
        "What you'll learn",
        "The lessons in order, so a student can see the whole journey upfront.",
        "A visible syllabus is what turns browsing into enrolling.",
        "checklist",
        {
            "items": [
                "Foundations: the core concepts",
                "Guided practice with feedback",
                "A real project, start to finish",
                "Review and certification",
            ],
        },
    ),
    "enrollment-form": _element(
        "enrollment-form",
        "Enroll",
        "Name, email, and course choice — enrolment in one short form.",
        "Enrolment should take a minute, not a phone call — the form removes the friction.",
        "form",
        {
            "fields": [
                {
                    "name": "full_name",
                    "label": "Full name",
                    "type": "text",
                    "placeholder": "Jane Cooper",
                },
                {
                    "name": "email",
                    "label": "Email address",
                    "type": "email",
                    "placeholder": "jane@example.com",
                },
                {
                    "name": "course",
                    "label": "Course",
                    "type": "text",
                    "placeholder": "e.g. Foundations",
                },
            ],
            "submit": "Enroll",
        },
    ),
    "instructor-card": _element(
        "instructor-card",
        "Your instructor",
        "The teacher's own words about how the course is taught.",
        "Students enrol with a teacher they trust — the instructor's voice sells the course.",
        "quote",
        {
            "text": (
                "I teach the way I wish I'd been taught: small steps, real examples, "
                "and feedback on every assignment."
            ),
            "author": "Dr. Priya Nair",
            "role": "Lead Instructor",
        },
    ),
    # -- travel -------------------------------------------------------------------
    "destination-card": _element(
        "destination-card",
        "Featured destination",
        "One trip with its starting price and what makes it worth the journey.",
        "Travellers fall for one place first — the destination card is the brochure.",
        "card",
        {
            "price": "From $899",
            "price_label": "per person",
            "cta": "Explore trip",
            "points": [
                "Flights and stays included",
                "Small groups, max 12 travellers",
                "Free cancellation up to 30 days",
            ],
        },
    ),
    "booking-form": _element(
        "booking-form",
        "Plan your trip",
        "Where, when, and how many — the start of every booking.",
        "A booking starts with three answers — ask them before anything else.",
        "form",
        {
            "fields": [
                {
                    "name": "destination",
                    "label": "Destination",
                    "type": "text",
                    "placeholder": "Where to?",
                },
                {"name": "check_in", "label": "Check-in", "type": "date", "placeholder": ""},
                {"name": "check_out", "label": "Check-out", "type": "date", "placeholder": ""},
                {
                    "name": "guests",
                    "label": "Guests",
                    "type": "number",
                    "placeholder": "2",
                },
            ],
            "submit": "Search stays",
        },
    ),
    "itinerary-card": _element(
        "itinerary-card",
        "Your itinerary",
        "The trip day by day, so travellers know exactly what to expect.",
        "A clear day-by-day plan is what turns interest into a booking.",
        "checklist",
        {
            "items": [
                "Day 1 — Arrive and settle in",
                "Day 2 — Guided city tour",
                "Day 3 — Free day to explore",
                "Day 4 — Departure transfers",
            ],
        },
    ),
}

# (profile key, matching keywords, element keys in proposal order)
_PROFILES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "fintech",
        ("finance", "bank", "payment", "crypto", "trading", "invest", "fintech"),
        ("pricing-card", "stat-card", "transaction-table", "signup-form"),
    ),
    (
        "restaurant",
        ("restaurant", "food", "cafe", "coffee", "bakery", "dining", "menu"),
        ("menu-card", "reservation-form", "gallery-card", "hours-card"),
    ),
    (
        "saas",
        ("saas", "software", "b2b", "platform", "subscription"),
        ("feature-grid", "testimonial-card", "pricing-table", "onboarding-checklist"),
    ),
    (
        "ecommerce",
        ("shop", "store", "ecommerce", "retail", "marketplace", "product"),
        ("product-card", "cart-summary", "checkout-form", "review-card"),
    ),
    (
        "health",
        ("health", "fitness", "wellness", "medical", "clinic", "gym"),
        ("appointment-form", "plan-card", "progress-tracker", "trainer-card"),
    ),
    (
        "education",
        ("education", "school", "course", "learn", "student", "teach"),
        ("course-card", "lesson-list", "enrollment-form", "instructor-card"),
    ),
    (
        "travel",
        ("travel", "hotel", "flight", "trip", "tour", "booking"),
        ("destination-card", "booking-form", "itinerary-card", "review-card"),
    ),
)

_PROFILE_ELEMENTS: dict[str, tuple[str, ...]] = {
    key: elements for key, _keywords, elements in _PROFILES
}


def match_profile(context: BusinessContext) -> str | None:
    """The industry profile key matching the context, or None.

    Matches on industry + offering + description, lowercased, against each
    profile's keywords (substring match, same style as
    `design_kit.palette_for_vibe`). The first profile in declaration order
    wins. An empty context never matches.
    """
    if context.is_empty():
        return None
    haystack = " ".join((context.industry, context.offering, context.description)).lower()
    if not haystack.strip():
        return None
    for key, keywords, _elements in _PROFILES:
        if any(word in haystack for word in keywords):
            return key
    return None


def propose_elements(context: BusinessContext) -> list[BusinessElement]:
    """The profile's elements for this context, or [] when nothing matches."""
    profile = match_profile(context)
    if profile is None:
        return []
    return elements_for_keys(_PROFILE_ELEMENTS[profile])


def elements_for_keys(keys: Iterable[str]) -> list[BusinessElement]:
    """Catalog elements for `keys`, in order; unknown keys are skipped."""
    return [ELEMENT_CATALOG[key] for key in keys if key in ELEMENT_CATALOG]


def enabled_element_keys(
    meta: Mapping[str, object] | None, context: BusinessContext | None
) -> list[str]:
    """The element keys enabled for a system.

    The stored `meta["business_elements"]` list wins when it is a list
    (unknown keys filtered out — an explicit empty list means none enabled).
    Otherwise the proposed keys for `context`, or [] when there is no
    context / no matching profile.
    """
    if isinstance(meta, Mapping):
        stored = meta.get("business_elements")
        if isinstance(stored, list):
            return [key for key in stored if isinstance(key, str) and key in ELEMENT_CATALOG]
    if context is None:
        return []
    return [element.key for element in propose_elements(context)]


def _coerce_str(value: object, default: str = "") -> str:
    return default if value is None else str(value)


def _coerce_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _data_strs(data: dict[str, object], key: str) -> list[str]:
    raw = data.get(key)
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _data_dicts(data: dict[str, object], key: str) -> list[dict[str, object]]:
    raw = data.get(key)
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]

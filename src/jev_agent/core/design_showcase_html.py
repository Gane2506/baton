"""Showcase HTML renderer for design systems (issue #155, part 2).

`render_showcase()` produces a self-contained, no-dependency HTML gallery for
one generated design system: hero, palette visualization, typography
specimens, component showcase with tokens applied, and a dark/light toggle.
Every section embodies the vibe profile — the page itself is the proof that
the system works.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from jev_agent.core.design_motion3d import (
    build_motion_spec,
    build_scene_spec,
    motion_tokens_from_tokens,
    reduced_motion_css,
    scene_type_for_context,
    showcase_motion3d_script,
)
from jev_agent.core.design_showcase import VibeProfile, _relative_luminance

if TYPE_CHECKING:
    from jev_agent.core.design_business import BusinessContext


def _css_value(value: object) -> str:
    """A token value safe inside a CSS declaration: control characters and
    `;`/`{`/`}` terminators stripped so a value cannot inject extra rules."""
    return re.sub(r"[\x00-\x1f\x7f;{}]", "", str(value)).strip()


def _px_value(value: object) -> str | None:
    """A token length as `Npx`, or None when the value is not a number
    (bools excluded) — non-numeric lengths are skipped, never interpolated
    raw into a `width:{v}px` style."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return f"{value}px"
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return None
        return f"{value}px"
    return None


def _dict_section(t: dict[str, object], key: str) -> dict[str, str]:
    """A token sub-dict with stringified values, or empty when absent/mistyped."""
    section = t.get(key)
    if not isinstance(section, dict):
        return {}
    return {str(k): str(v) for k, v in section.items()}


def _str_value(section: dict[str, str], key: str) -> str:
    return section.get(key, "")


def _type_vars(t: dict[str, object]) -> list[str]:
    """CSS custom properties for the type scale, from the token set.

    Each typography style (`display_xl` … `overline`) is a `{"size", "weight",
    "tracking", "leading"}` dict (`design_showcase._typography`); the
    `.type-*` rules in `_STYLES` read these variables, so a system whose type
    scale differs from the defaults actually renders at its own sizes. A
    missing or non-numeric field emits nothing — the rule's `var()` fallback
    (the historical hard-coded size) applies instead, so flat/legacy token
    sets render exactly as before.
    """
    typo = t.get("typography")
    if not isinstance(typo, dict):
        return []
    lines: list[str] = []
    for key, _label in _TYPE_STYLES:
        spec = typo.get(key)
        if not isinstance(spec, dict):
            continue
        stem = f"--tw-{key.replace('_', '-')}"
        if (px := _px_value(spec.get("size"))) is not None:
            lines.append(f"  {stem}-size: {px};")
        weight = spec.get("weight")
        if isinstance(weight, (int, float)) and not isinstance(weight, bool):
            lines.append(f"  {stem}-weight: {_css_value(weight)};")
        tracking = spec.get("tracking")
        if isinstance(tracking, (int, float)) and not isinstance(tracking, bool):
            lines.append(f"  {stem}-tracking: {_css_value(tracking)}em;")
        leading = spec.get("leading")
        if isinstance(leading, (int, float)) and not isinstance(leading, bool):
            lines.append(f"  {stem}-leading: {_css_value(leading)};")
    return lines


_TYPE_STYLES: tuple[tuple[str, str], ...] = (
    ("display_xl", "Display XL"),
    ("display_lg", "Display LG"),
    ("display_md", "Display MD"),
    ("display_sm", "Display SM"),
    ("headline_lg", "Headline LG"),
    ("headline_md", "Headline MD"),
    ("headline_sm", "Headline SM"),
    ("body_lg", "Body LG"),
    ("body_md", "Body MD"),
    ("body_sm", "Body SM"),
    ("caption", "Caption"),
    ("overline", "Overline"),
)


def _css_vars(colors: dict[str, str], t: dict[str, object], vibe: VibeProfile) -> str:
    """CSS custom properties from the token set (light theme defaults)."""
    lines = [":root {"]
    for key, value in colors.items():
        lines.append(f"  --c-{key.replace('_', '-')}: {_css_value(value)};")
    typo = _dict_section(t, "typography")
    lines.append(f"  --font-display: {_css_value(_str_value(typo, 'font_display'))};")
    lines.append(f"  --font-body: {_css_value(_str_value(typo, 'font_body'))};")
    lines.append(f"  --font-mono: {_css_value(_str_value(typo, 'font_mono'))};")
    lines.extend(_type_vars(t))
    spacing = _dict_section(t, "spacing")
    for key, value in spacing.items():
        px = _px_value(value)
        if px is not None:
            lines.append(f"  --sp-{key}: {px};")
    radii = _dict_section(t, "radii")
    for key, value in radii.items():
        px = "9999px" if value == "9999" else _px_value(value)
        if px is not None:
            lines.append(f"  --r-{key}: {px};")
    shadows = _dict_section(t, "shadows")
    for key, value in shadows.items():
        lines.append(f"  --sh-{key}: {_css_value(value)};")
    motion = _dict_section(t, "motion")
    for key, value in motion.items():
        lines.append(f"  --mo-{key.replace('_', '-')}: {_css_value(value)};")
    lines.append("}")
    return "\n".join(lines)


def _dark_overrides(dark: dict[str, str]) -> str:
    lines = ['[data-theme="dark"] {']
    for key, value in dark.items():
        lines.append(f"  --c-{key.replace('_', '-')}: {_css_value(value)};")
    # Dark shadow tuning: shadows need more presence on dark surfaces.
    lines.append("  --sh-md: 0 4px 16px rgba(0,0,0,0.35);")
    lines.append("  --sh-lg: 0 12px 32px rgba(0,0,0,0.40);")
    lines.append("}")
    return "\n".join(lines)


def _hero(
    vibe: VibeProfile,
    name: str,
    colors: dict[str, str],
    business: BusinessContext | None = None,
) -> str:
    """The hero section, treated per the vibe's hero_style.

    `name` and vibe copy are HTML-escaped here (the page is served as
    text/html). The split hero never indexes colors directly: flat/legacy
    token sets lack the accent/neutral ramps, so each swatch falls back
    through related roles to a constant, instead of raising KeyError.
    When `business` carries an industry, the hero gains a short
    "Built for {industry}" line (issue #283).
    """
    style = vibe.hero_style
    display_name = html.escape(name)
    business_line = ""
    industry = (getattr(business, "industry", "") or "").strip() if business is not None else ""
    if industry:
        business_line = (
            f'\n      <p class="hero-business" data-motion-hero>'
            f"Built for {html.escape(industry)}</p>"
        )
    inner = f"""
      <p class="hero-overline" data-motion-hero>Design system</p>
      <h1 class="hero-title" data-motion-hero>{display_name}</h1>
      <p class="hero-tagline" data-motion-hero>{html.escape(vibe.tagline)}</p>{business_line}
      <p class="hero-desc" data-motion-hero>{html.escape(vibe.description)}</p>
      <div class="hero-actions" data-motion-hero>
        <a class="btn btn-primary" href="#components">Explore components</a>
        <a class="btn btn-secondary" href="#palette">View palette</a>
      </div>"""
    if style == "gradient":
        return f"""<header class="hero hero-gradient">{inner}
      <div class="hero-glow" aria-hidden="true"></div>
    </header>"""
    if style == "pattern":
        dots = "".join('<span class="dot"></span>' for _ in range(24))
        return f"""<header class="hero hero-pattern">{inner}
      <div class="hero-dots" aria-hidden="true">{dots}</div>
    </header>"""
    if style == "split":
        primary = (
            colors.get("primary")
            or colors.get("accent_500")
            or colors.get("accent_300")
            or "#2563EB"
        )
        accent = colors.get("accent_300") or colors.get("accent_500") or primary
        neutral = (
            colors.get("neutral_200")
            or colors.get("neutral_100")
            or colors.get("neutral_300")
            or colors.get("surface")
            or colors.get("background")
            or "#E2E8F0"
        )
        return f"""<header class="hero hero-split">
      <div class="hero-split-text">{inner}</div>
      <div class="hero-split-card" aria-hidden="true">
        <div class="split-swatch" style="background:{_css_value(primary)}"></div>
        <div class="split-swatch" style="background:{_css_value(accent)}"></div>
        <div class="split-swatch" style="background:{_css_value(neutral)}"></div>
      </div>
    </header>"""
    if style == "dark":
        return f"""<header class="hero hero-dark">{inner}</header>"""
    # "type" — typographic hero, the type does the talking
    return f"""<header class="hero hero-type">
      <p class="hero-giant" aria-hidden="true">Aa</p>
      {inner}
    </header>"""


def _palette_section(colors: dict[str, str]) -> str:
    """Swatch grids: neutrals ramp, accent ramp, semantic roles."""

    def swatch(name: str, value: str) -> str:
        try:
            text = "#FFFFFF" if _is_dark(value) else "#101418"
        except (ValueError, TypeError):
            text = "#101418"
        safe_value = _css_value(value)
        hex_attr = html.escape(safe_value, quote=True)
        return (
            f'<button class="swatch" data-hex="{hex_attr}" title="Click to copy">'
            f'<span class="swatch-color" style="background:{safe_value};color:{text}">'
            f"{html.escape(safe_value)}</span>"
            f'<span class="swatch-name">{html.escape(name)}</span></button>'
        )

    neutrals = "".join(
        swatch(f"neutral_{n}", colors[f"neutral_{n}"])
        for n in ["50", "100", "200", "300", "400", "500", "600", "700", "800", "900", "950"]
        if f"neutral_{n}" in colors
    )
    accents = "".join(
        swatch(f"accent_{n}", colors[f"accent_{n}"])
        for n in ["100", "300", "500", "700", "900"]
        if f"accent_{n}" in colors
    )
    semantic = "".join(
        swatch(name, colors[name])
        for name in [
            "primary",
            "primary_hover",
            "success",
            "warning",
            "danger",
            "info",
        ]
        if name in colors
    )
    return f"""
    <section id="palette" class="section" data-reveal>
      <p class="overline">01 — Color</p>
      <h2 class="section-title">Palette</h2>
      <p class="section-desc">Click any swatch to copy its hex value.</p>
      <h3 class="group-title">Neutrals</h3>
      <div class="swatch-grid">{neutrals}</div>
      <h3 class="group-title">Accent ramp</h3>
      <div class="swatch-grid">{accents}</div>
      <h3 class="group-title">Semantic</h3>
      <div class="swatch-grid">{semantic}</div>
    </section>"""


def _is_dark(hex_color: str) -> bool:
    return _relative_luminance(hex_color) < 0.35


def _type_section(typo: dict[str, object]) -> str:
    """Every type style rendered at scale with its spec."""
    order = _TYPE_STYLES
    samples = {
        "display_xl": "Design is intelligence made visible",
        "display_lg": "Design is intelligence made visible",
        "display_md": "Design is intelligence made visible",
        "display_sm": "Design is intelligence made visible",
        "headline_lg": "The quick brown fox jumps over the lazy dog",
        "headline_md": "The quick brown fox jumps over the lazy dog",
        "headline_sm": "The quick brown fox jumps over the lazy dog",
        "body_lg": (
            "Good design is as little design as possible. Less, but better — "
            "because it concentrates on the essential aspects."
        ),
        "body_md": (
            "Good design is as little design as possible. Less, but better — "
            "because it concentrates on the essential aspects."
        ),
        "body_sm": ("Good design is as little design as possible. Less, but better."),
        "caption": "Caption — supporting text, timestamps, metadata",
        "overline": "OVERLINE — SECTION LABELS AND EYEBROWS",
    }
    rows = []
    for key, label in order:
        spec = typo.get(key)
        spec_dict: dict[str, object] = spec if isinstance(spec, dict) else {}
        sample = samples[key]

        def _spec(field: str, _d: dict[str, object] = spec_dict) -> str:
            return html.escape(str(_d.get(field, "")))

        rows.append(f"""
      <div class="type-row">
        <div class="type-meta">
          <span class="type-label">{label}</span>
          <span class="type-spec">{_spec("size")}px · {_spec("weight")} ·
            {_spec("tracking")}em · {_spec("leading")}</span>
        </div>
        <p class="type-sample type-{key.replace("_", "-")}">{sample}</p>
      </div>""")
    return f"""
    <section id="typography" class="section" data-reveal>
      <p class="overline">02 — Typography</p>
      <h2 class="section-title">Type scale</h2>
      <p class="section-desc">A real hierarchy: display for emotion, headlines for
      structure, body for reading, captions for metadata.</p>
      {"".join(rows)}
    </section>"""


def _components_section() -> str:
    return """
    <section id="components" class="section" data-reveal>
      <p class="overline">03 — Components</p>
      <h2 class="section-title">In action</h2>
      <p class="section-desc">Tokens applied — this is what the system feels like
      to build with.</p>

      <h3 class="group-title">Buttons</h3>
      <div class="demo-row">
        <button class="btn btn-primary">Primary</button>
        <button class="btn btn-secondary">Secondary</button>
        <button class="btn btn-ghost">Ghost</button>
        <button class="btn btn-danger">Danger</button>
        <button class="btn btn-primary" disabled>Disabled</button>
      </div>

      <h3 class="group-title">Inputs</h3>
      <div class="demo-grid">
        <label class="field">Label
          <input type="text" placeholder="Placeholder text" value="Typed value">
        </label>
        <label class="field">Label
          <input type="text" placeholder="Placeholder text" class="field-error" value="Wrong value">
          <span class="field-hint error">This field needs attention.</span>
        </label>
        <label class="field">Label
          <select><option>Option one</option><option>Option two</option></select>
        </label>
        <label class="field">Label
          <textarea rows="2" placeholder="Write something…"></textarea>
        </label>
      </div>

      <h3 class="group-title">Badges & alerts</h3>
      <div class="demo-row">
        <span class="badge badge-success">Success</span>
        <span class="badge badge-warning">Warning</span>
        <span class="badge badge-danger">Danger</span>
        <span class="badge badge-info">Info</span>
        <span class="badge badge-muted">Muted</span>
      </div>
      <div class="demo-stack">
        <div class="alert alert-success"><strong>Success.</strong> Your changes were saved.</div>
        <div class="alert alert-warning"><strong>Heads up.</strong> This action needs review.</div>
        <div class="alert alert-danger"><strong>Error.</strong> Something went wrong — try
          again.</div>
        <div class="alert alert-info"><strong>Note.</strong> New features are available.</div>
      </div>

      <h3 class="group-title">Card</h3>
      <div class="demo-grid">
        <article class="card">
          <div class="card-media" aria-hidden="true"></div>
          <div class="card-body">
            <p class="overline">Featured</p>
            <h4>Card headline that draws the eye</h4>
            <p class="card-text">A short supporting paragraph that explains what
            this card is about in one or two lines.</p>
            <div class="card-actions">
              <button class="btn btn-primary btn-sm">Primary</button>
              <button class="btn btn-ghost btn-sm">Ghost</button>
            </div>
          </div>
        </article>
        <article class="card">
          <div class="card-body">
            <div class="stat"><span class="stat-value">$48.2k</span><span
              class="stat-label">Revenue</span></div>
            <div class="stat"><span class="stat-value">+12.4%</span><span
              class="stat-label">Growth</span></div>
            <div class="stat"><span class="stat-value">8,102</span><span
              class="stat-label">Users</span></div>
          </div>
        </article>
      </div>

      <h3 class="group-title">Navigation</h3>
      <nav class="demo-nav" aria-label="Demo">
        <span class="demo-logo">Mark</span>
        <a href="#" class="demo-nav-link active">Overview</a>
        <a href="#" class="demo-nav-link">Projects</a>
        <a href="#" class="demo-nav-link">Team</a>
        <a href="#" class="demo-nav-link">Settings</a>
        <span class="demo-nav-right"><span class="avatar">AK</span></span>
      </nav>

      <h3 class="group-title">Data table</h3>
      <div class="table-wrap"><table class="demo-table">
        <thead><tr><th>Project</th><th>Status</th><th class="num">Budget</th><th
          class="num">Progress</th></tr></thead>
        <tbody>
          <tr><td><strong>Atlas</strong><span class="muted"> · Web app</span></td><td><span
            class="badge badge-success">Live</span></td><td class="num">$48,200</td><td
            class="num"><span class="progress"><span style="width:82%"></span></span> 82%</td></tr>
          <tr><td><strong>Beacon</strong><span class="muted"> · Mobile</span></td><td><span
            class="badge badge-warning">Review</span></td><td class="num">$12,750</td><td
            class="num"><span class="progress"><span style="width:45%"></span></span> 45%</td></tr>
          <tr><td><strong>Cipher</strong><span class="muted"> · API</span></td><td><span
            class="badge badge-info">Building</span></td><td class="num">$8,400</td><td
            class="num"><span class="progress"><span style="width:63%"></span></span> 63%</td></tr>
          <tr><td><strong>Dune</strong><span class="muted"> · Site</span></td><td><span
            class="badge badge-muted">Paused</span></td><td class="num">$3,100</td><td
            class="num"><span class="progress"><span style="width:12%"></span></span> 12%</td></tr>
        </tbody>
      </table></div>
    </section>"""


def _foundations_section(t: dict[str, object]) -> str:
    spacing = _dict_section(t, "spacing")
    radii = _dict_section(t, "radii")
    shadows = _dict_section(t, "shadows")
    sp = "".join(
        f'<div class="space-demo"><span class="space-box" style="width:{px};height:{px}"></span>'
        f'<span class="space-name">{html.escape(k)} · {px}</span></div>'
        for k, v in spacing.items()
        if (px := _px_value(v)) is not None
    )
    ra_items = []
    for k, v in radii.items():
        radius_css = "9999px" if v == "9999" else _px_value(v)
        if radius_css is None:
            continue
        ra_items.append(
            f'<div class="radius-demo"><span class="radius-box"'
            f' style="border-radius:{radius_css}"></span>'
            f'<span class="space-name">{html.escape(k)}</span></div>'
        )
    ra = "".join(ra_items)
    sh = "".join(
        f'<div class="shadow-demo"><span class="shadow-box"'
        f' style="box-shadow:{_css_value(v)}"></span>'
        f'<span class="space-name">{html.escape(k)}</span></div>'
        for k, v in shadows.items()
    )
    return f"""
    <section id="foundations" class="section" data-reveal>
      <p class="overline">04 — Foundations</p>
      <h2 class="section-title">Space, shape & depth</h2>
      <h3 class="group-title">Spacing</h3>
      <div class="foundation-grid">{sp}</div>
      <h3 class="group-title">Radii</h3>
      <div class="foundation-grid">{ra}</div>
      <h3 class="group-title">Shadows</h3>
      <div class="foundation-grid">{sh}</div>
    </section>"""


_STYLES = """
/* ============ showcase base ============ */
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  font-family:var(--font-body);background:var(--c-background);color:var(--c-text);
  line-height:1.6;-webkit-font-smoothing:antialiased;transition:background .3s,color .3s;
}
.wrap{max-width:1120px;margin:0 auto;padding:0 32px}
/* top bar */
.topbar{position:sticky;top:0;z-index:50;backdrop-filter:blur(12px);
  background:color-mix(in srgb,var(--c-background) 82%,transparent);
  border-bottom:1px solid var(--c-border)}
.topbar-inner {
  max-width:1120px;
  margin:0 auto;
  padding:14px 32px;
  display:flex;
  align-items:center;
  gap:28px;
}
.topbar-brand{font-family:var(--font-display);font-weight:700;letter-spacing:-0.01em}
.topbar nav{display:flex;gap:4px;margin-left:auto}
.topbar nav a{padding:8px 14px;border-radius:var(--r-md);color:var(--c-text-muted);
  text-decoration:none;font-size:14px;font-weight:500;transition:all var(--mo-duration-fast)
    var(--mo-ease-standard)}
.topbar nav a:hover{background:var(--c-surface-raised);color:var(--c-text)}
.theme-toggle{border:1px solid var(--c-border);background:var(--c-surface);color:var(--c-text);
  border-radius:var(--r-full);padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;
  transition:all var(--mo-duration-fast) var(--mo-ease-standard)}
.theme-toggle:hover{border-color:var(--c-border-strong)}
/* hero */
.hero{position:relative;overflow:hidden;padding:110px 32px 90px;text-align:center}
.hero-overline {
  font-family:var(--font-mono);
  font-size:12px;
  letter-spacing:.18em;
  text-transform:uppercase;
  color:var(--c-text-muted);margin-bottom:20px}
.hero-title{font-family:var(--font-display);font-weight:var(--hero-weight,700);
  letter-spacing:var(--hero-tracking,-0.025em);line-height:1.04;
  font-size:clamp(48px,7vw,88px);margin-bottom:18px}
.hero-tagline{font-family:var(--font-display);font-size:22px;font-weight:500;
  color:var(--c-primary);margin-bottom:14px}
.hero-business{font-size:15px;font-weight:600;color:var(--c-text-muted);margin-bottom:14px}
.hero-desc{max-width:620px;margin:0 auto 34px;color:var(--c-text-muted);font-size:17px}
.hero-actions{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.hero-gradient{background:
  radial-gradient(900px 480px at 20% 10%,color-mix(in srgb,var(--c-primary)
    22%,transparent),transparent 60%),
  radial-gradient(800px 420px at 85% 90%,color-mix(in srgb,var(--c-accent-300)
    20%,transparent),transparent 60%),
  var(--c-background)}
.hero-glow{position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(600px 300px at 50% 0%,color-mix(in srgb,var(--c-primary)
    14%,transparent),transparent 70%)}
.hero-pattern .hero-dots{position:absolute;inset:0;display:grid;grid-template-columns:repeat(8,1fr);
  gap:26px;padding:60px;opacity:.5;pointer-events:none}
.hero-pattern .dot{width:10px;height:10px;border-radius:50%;background:var(--c-primary);opacity:.28}
.hero-pattern .dot:nth-child(3n){background:var(--c-accent-300);opacity:.4}
.hero-pattern .dot:nth-child(5n){transform:scale(1.6)}
.hero-split {
  display:grid;
  grid-template-columns:1.1fr .9fr;
  gap:40px;
  text-align:left;
  align-items:center;
  max-width:1120px;margin:0 auto}
.hero-split .hero-actions{justify-content:flex-start}
.hero-split-card{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.split-swatch{aspect-ratio:3/4;border-radius:var(--r-lg);box-shadow:var(--sh-md)}
.hero-dark{background:#0B0B0D;color:#F5F5F4}
.hero-dark .hero-overline,.hero-dark .hero-desc{color:#A8A29E}
.hero-dark .hero-tagline{color:var(--c-primary)}
.hero-type .hero-giant{font-family:var(--font-display);font-weight:800;
  font-size:clamp(120px,22vw,260px);line-height:.9;letter-spacing:-0.04em;
  color:transparent;-webkit-text-stroke:1.5px
    var(--c-border-strong);margin-bottom:-30px;user-select:none}
/* sections */
.section{padding:72px 0;border-top:1px solid var(--c-border)}
.overline{font-family:var(--font-mono);font-size:12px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--c-primary);margin-bottom:12px}
.section-title{font-family:var(--font-display);font-weight:700;letter-spacing:-0.02em;
  font-size:clamp(30px,4vw,44px);line-height:1.1;margin-bottom:10px}
.section-desc{color:var(--c-text-muted);font-size:16px;max-width:640px;margin-bottom:36px}
.group-title{font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:var(--c-text-muted);margin:34px 0 16px}
/* palette */
.swatch-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:12px}
.swatch{border:1px solid var(--c-border);border-radius:var(--r-md);overflow:hidden;cursor:pointer;
  background:var(--c-surface);padding:0;font:inherit;color:inherit;text-align:left;
  transition:transform var(--mo-duration-fast) var(--mo-ease-standard),box-shadow
    var(--mo-duration-fast)}
.swatch:hover{transform:translateY(-3px);box-shadow:var(--sh-md)}
.swatch-color {
  display:block;
  padding:26px 12px;
  font-family:var(--font-mono);
  font-size:12px;
  font-weight:600;
}
.swatch-name {
  display:block;
  padding:10px 12px;
  font-size:13px;
  font-weight:500;
  color:var(--c-text-muted);
}
/* typography */
.type-row {
  display:grid;
  grid-template-columns:240px 1fr;
  gap:24px;
  padding:26px 0;
  border-top:1px solid var(--c-border);
  align-items:baseline;
}
.type-label{display:block;font-weight:700;font-size:14px}
.type-spec {
  display:block;
  font-family:var(--font-mono);
  font-size:12px;
  color:var(--c-text-muted);
  margin-top:4px;
}
.type-sample{line-height:1.2}
.type-display-xl {
  font-family:var(--font-display);
  font-size:var(--tw-display-xl-size,76px);
  font-weight:var(--tw-display-xl-weight,700);
  letter-spacing:var(--tw-display-xl-tracking,-0.025em);
  line-height:var(--tw-display-xl-leading,1.02);
}
.type-display-lg {
  font-family:var(--font-display);
  font-size:var(--tw-display-lg-size,60px);
  font-weight:var(--tw-display-lg-weight,700);
  letter-spacing:var(--tw-display-lg-tracking,-0.025em);
  line-height:var(--tw-display-lg-leading,1.05);
}
.type-display-md {
  font-family:var(--font-display);
  font-size:var(--tw-display-md-size,48px);
  font-weight:var(--tw-display-md-weight,700);
  letter-spacing:var(--tw-display-md-tracking,-0.025em);
  line-height:var(--tw-display-md-leading,1.08);
}
.type-display-sm {
  font-family:var(--font-display);
  font-size:var(--tw-display-sm-size,36px);
  font-weight:var(--tw-display-sm-weight,700);
  letter-spacing:var(--tw-display-sm-tracking,-0.025em);
  line-height:var(--tw-display-sm-leading,1.1);
}
.type-headline-lg {
  font-family:var(--font-display);
  font-size:var(--tw-headline-lg-size,28px);
  font-weight:var(--tw-headline-lg-weight,650);
  letter-spacing:var(--tw-headline-lg-tracking,-0.01em);
  line-height:var(--tw-headline-lg-leading,1.2);
}
.type-headline-md {
  font-family:var(--font-display);
  font-size:var(--tw-headline-md-size,22px);
  font-weight:var(--tw-headline-md-weight,600);
  letter-spacing:var(--tw-headline-md-tracking,-0.008em);
  line-height:var(--tw-headline-md-leading,1.25);
}
.type-headline-sm {
  font-family:var(--font-display);
  font-size:var(--tw-headline-sm-size,18px);
  font-weight:var(--tw-headline-sm-weight,600);
  letter-spacing:var(--tw-headline-sm-tracking,-0.005em);
  line-height:var(--tw-headline-sm-leading,1.3);
}
.type-body-lg{
  font-size:var(--tw-body-lg-size,17px);
  font-weight:var(--tw-body-lg-weight,400);
  letter-spacing:var(--tw-body-lg-tracking,0em);
  line-height:var(--tw-body-lg-leading,1.6);
}
.type-body-md{
  font-size:var(--tw-body-md-size,15px);
  font-weight:var(--tw-body-md-weight,400);
  letter-spacing:var(--tw-body-md-tracking,0em);
  line-height:var(--tw-body-md-leading,1.6);
}
.type-body-sm{
  font-size:var(--tw-body-sm-size,13px);
  font-weight:var(--tw-body-sm-weight,400);
  letter-spacing:var(--tw-body-sm-tracking,.01em);
  line-height:var(--tw-body-sm-leading,1.55);
}
.type-caption {
  font-size:var(--tw-caption-size,12px);
  font-weight:var(--tw-caption-weight,500);
  letter-spacing:var(--tw-caption-tracking,.02em);
  line-height:var(--tw-caption-leading,1.45);
  color:var(--c-text-muted);
}
.type-overline {
  font-size:var(--tw-overline-size,11px);
  font-weight:var(--tw-overline-weight,600);
  letter-spacing:var(--tw-overline-tracking,.12em);
  line-height:var(--tw-overline-leading,1.4);
  color:var(--c-text-muted);
}
/* components */
.demo-row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
.demo-stack{display:grid;gap:10px;max-width:640px}
.demo-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.btn {
  font-family:var(--font-body);
  font-weight:600;
  font-size:15px;
  padding:12px 22px;
  border-radius:var(--r-md);
  border:1px solid transparent;cursor:pointer;transition:all var(--mo-duration-fast)
    var(--mo-ease-standard);
  text-decoration:none;display:inline-block}
.btn-primary{background:var(--c-primary);color:var(--c-primary-text)}
.btn-primary:hover {
  background:var(--c-primary-hover);
  transform:translateY(-1px);
  box-shadow:var(--sh-sm);
}
.btn-secondary{background:var(--c-surface);color:var(--c-text);border-color:var(--c-border-strong)}
.btn-secondary:hover{border-color:var(--c-text-faint)}
.btn-ghost{background:transparent;color:var(--c-primary)}
.btn-ghost:hover{background:var(--c-primary-subtle)}
.btn-danger{background:var(--c-danger);color:#fff}
.btn-danger:hover{filter:brightness(.94)}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none!important;box-shadow:none!important}
.btn-sm{font-size:13px;padding:8px 16px}
.field{display:grid;gap:6px;font-size:14px;font-weight:600}
.field input,.field select,.field
  textarea{font:inherit;font-weight:400;padding:11px 14px;border-radius:var(--r-md);
  border:1px solid
    var(--c-border-strong);background:var(--c-surface);color:var(--c-text);width:100%;
  transition:border-color var(--mo-duration-fast),box-shadow var(--mo-duration-fast)}
.field input:focus,.field select:focus,.field
  textarea:focus{outline:none;border-color:var(--c-primary);
  box-shadow:0 0 0 3px var(--c-primary-subtle)}
.field-error{border-color:var(--c-danger)!important}
.field-hint{font-size:13px;font-weight:400;color:var(--c-text-muted)}
.field-hint.error{color:var(--c-danger)}
.badge {
  display:inline-flex;
  align-items:center;
  font-size:12.5px;
  font-weight:600;
  padding:5px 12px;
  border-radius:var(--r-full);
}
.badge-success{background:var(--c-success-subtle);color:var(--c-success)}
.badge-warning{background:var(--c-warning-subtle);color:var(--c-warning)}
.badge-danger{background:var(--c-danger-subtle);color:var(--c-danger)}
.badge-info{background:var(--c-info-subtle);color:var(--c-info)}
.badge-muted{background:var(--c-surface-raised);color:var(--c-text-muted)}
.alert{padding:14px 18px;border-radius:var(--r-md);font-size:14.5px;border:1px solid}
.alert-success{background:var(--c-success-subtle);border-color:var(--c-success);color:var(--c-text)}
.alert-warning{background:var(--c-warning-subtle);border-color:var(--c-warning);color:var(--c-text)}
.alert-danger{background:var(--c-danger-subtle);border-color:var(--c-danger);color:var(--c-text)}
.alert-info{background:var(--c-info-subtle);border-color:var(--c-info);color:var(--c-text)}
.card{background:var(--c-surface);border:1px solid var(--c-border);border-radius:var(--r-lg);
  overflow:hidden;box-shadow:var(--sh-sm);transition:box-shadow var(--mo-duration-normal)
    var(--mo-ease-standard),transform var(--mo-duration-normal)}
.card:hover{box-shadow:var(--sh-md);transform:translateY(-2px)}
.card-media{height:150px;background:linear-gradient(135deg,var(--c-primary),var(--c-accent-300))}
.card-body{padding:22px}
.card-body
  h4 {
    font-family:var(--font-display);
    font-size:19px;
    font-weight:650;
    letter-spacing:-0.01em;
    margin:8px 0 8px;
  }
.card-text{color:var(--c-text-muted);font-size:14.5px;margin-bottom:16px}
.card-actions{display:flex;gap:10px}
.stat {
  display:flex;
  flex-direction:column;
  gap:2px;
  padding:12px 0;
  border-top:1px solid var(--c-border);
}
.stat:first-child{border-top:0}
.stat-value{font-family:var(--font-display);font-size:30px;font-weight:700;letter-spacing:-0.02em}
.stat-label{font-size:13px;color:var(--c-text-muted)}
.demo-nav {
  display:flex;
  align-items:center;
  gap:4px;
  background:var(--c-surface);
  border:1px solid var(--c-border);
  border-radius:var(--r-lg);padding:10px 16px}
.demo-logo{font-family:var(--font-display);font-weight:800;margin-right:18px}
.demo-nav-link {
  padding:9px 15px;
  border-radius:var(--r-md);
  text-decoration:none;
  color:var(--c-text-muted);
  font-size:14px;font-weight:500}
.demo-nav-link:hover{color:var(--c-text);background:var(--c-surface-raised)}
.demo-nav-link.active{color:var(--c-text);background:var(--c-surface-raised);font-weight:600}
.demo-nav-right{margin-left:auto}
.avatar {
  display:inline-flex;
  align-items:center;
  justify-content:center;
  width:36px;
  height:36px;
  border-radius:50%;
  background:var(--c-primary);color:var(--c-primary-text);font-size:13px;font-weight:700}
.table-wrap{overflow-x:auto;border:1px solid var(--c-border);border-radius:var(--r-lg)}
.demo-table{width:100%;border-collapse:collapse;font-size:14px;background:var(--c-surface)}
.demo-table
  th{text-align:left;padding:13px 18px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--c-text-muted);border-bottom:1px solid
    var(--c-border);background:var(--c-surface-raised);font-weight:600}
.demo-table td{padding:14px 18px;border-bottom:1px solid var(--c-border)}
.demo-table tr:last-child td{border-bottom:0}
.demo-table tr:hover td{background:var(--c-surface-raised)}
.demo-table .num{text-align:right;font-variant-numeric:tabular-nums}
.muted{color:var(--c-text-muted);font-weight:400}
.progress {
  display:inline-block;
  width:90px;
  height:7px;
  border-radius:99px;
  background:var(--c-surface-raised);
  overflow:hidden;vertical-align:middle;margin-right:8px}
.progress span{display:block;height:100%;background:var(--c-primary);border-radius:99px}
/* foundations */
.foundation-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:14px}
.space-demo,.radius-demo,.shadow-demo {
  display:flex;
  flex-direction:column;
  align-items:center;
  gap:10px;
  padding:18px 8px;border:1px solid
    var(--c-border);border-radius:var(--r-md);background:var(--c-surface)}
.space-box{background:var(--c-primary);border-radius:3px}
.space-name,.radius-demo
  .space-name{font-family:var(--font-mono);font-size:11.5px;color:var(--c-text-muted)}
.radius-box {
  width:64px;
  height:64px;
  background:var(--c-primary-subtle);
  border:2px solid var(--c-primary);
}
.shadow-box{width:72px;height:52px;background:var(--c-surface);border-radius:var(--r-md)}
/* motion & 3D (issue #302) */
.motion-controls{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.motion-ladder{display:grid;gap:10px;max-width:640px}
.motion-row{display:grid;grid-template-columns:110px 1fr 64px;gap:12px;align-items:center;
  font-size:13px;font-weight:600}
.motion-bar{height:10px;border-radius:99px;background:var(--c-primary)}
.motion-row span:last-child{font-family:var(--font-mono);font-weight:400;
  color:var(--c-text-muted);text-align:right}
.scene-wrap{position:relative;height:360px;border:1px solid var(--c-border);
  border-radius:var(--r-lg);overflow:hidden;background:var(--c-surface)}
.scene-wrap canvas{position:absolute;inset:0;width:100%;height:100%;display:block;
  touch-action:none;cursor:grab}
.scene-wrap canvas:active{cursor:grabbing}
.scene-fallback{position:absolute;inset:0}
.scene-hidden{display:none}
.scene-caption{color:var(--c-text-muted);font-size:13px;margin:10px 2px 0}
/* footer */
.footer {
  padding:56px 0 72px;
  border-top:1px solid var(--c-border);
  color:var(--c-text-muted);
  font-size:14px;
  display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap}
.copied {
  position:fixed;
  bottom:26px;
  left:50%;
  transform:translateX(-50%) translateY(20px);
  background:var(--c-text);
  color:var(--c-background);padding:10px
    20px;border-radius:var(--r-full);font-size:14px;font-weight:600;
  opacity:0;transition:all .25s;pointer-events:none;z-index:100}
.copied.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media(max-width:720px){
  .type-row{grid-template-columns:1fr;gap:8px}
  .hero-split{grid-template-columns:1fr}
  .wrap,.topbar-inner{padding-left:20px;padding-right:20px}
}
"""


def _motion_section(spec: dict[str, object], scene_spec: dict[str, object]) -> str:
    """The live motion & 3D section (issue #302).

    A duration ladder from the motion spec, the reduced-motion toggle, a
    replay control for the hero entrance, and the interactive 3D scene
    (canvas over the static fallback — the fallback is the default content,
    so no-WebGL, reduced-motion, and no-JS all show a real composition
    instead of a blank box). The inline runtime in the page footer drives
    it all from the same spec the downloadable files use.
    """
    durations = spec.get("durations")
    durations = durations if isinstance(durations, dict) else {}
    rows = []
    for key, label in (
        ("instant", "Instant"),
        ("fast", "Fast"),
        ("normal", "Normal"),
        ("slow", "Slow"),
        ("slower", "Slower"),
        ("stagger", "Stagger"),
    ):
        value = durations.get(key)
        ms = value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
        width = max(4.0, min(100.0, float(ms) / 10.0))
        rows.append(
            f'<div class="motion-row"><span>{label}</span>'
            f'<span class="motion-bar" style="width:{width:.0f}%"></span>'
            f"<span>{ms}ms</span></div>"
        )
    fallback = scene_spec.get("fallback")
    fallback = fallback if isinstance(fallback, dict) else {}
    fallback_css = _css_value(str(fallback.get("css", "")))
    label = html.escape(str(scene_spec.get("label", "3D scene")))
    description = html.escape(str(scene_spec.get("description", "")))
    caption = html.escape(str(fallback.get("caption", "")))
    return f"""
    <section id="motion-3d" class="section" data-reveal>
      <p class="overline">05 — Motion &amp; 3D</p>
      <h2 class="section-title">Motion &amp; 3D</h2>
      <p class="section-desc">Motion is feedback, not decoration: every duration
      below comes from the system's motion tokens, and the scene is built
      from its palette. Drag the scene to orbit it.</p>
      <div class="motion-controls">
        <button class="theme-toggle" id="motionToggle" type="button"
          aria-pressed="false">Reduce motion</button>
        <button class="theme-toggle" id="replayMotion" type="button">
          Replay hero entrance</button>
      </div>
      <h3 class="group-title">Duration ladder</h3>
      <div class="motion-ladder">{"".join(rows)}</div>
      <h3 class="group-title">3D scene — {label}</h3>
      <p class="section-desc">{description}</p>
      <div class="scene-wrap" id="sceneWrap">
        <canvas id="scene3d" width="640" height="360" role="img"
          aria-label="Interactive 3D scene: {label}"></canvas>
        <div class="scene-fallback" id="sceneFallback"
          style="background:{fallback_css}"></div>
      </div>
      <p class="scene-caption">{caption} Drag to orbit; the scene pauses when
      the tab is hidden.</p>
    </section>"""


def render_showcase(
    name: str,
    vibe: VibeProfile,
    tokens: dict[str, object],
    theme: str = "both",
    business: BusinessContext | None = None,
    *,
    scene_type: str | None = None,
    motion_tokens: dict[str, object] | None = None,
) -> str:
    """A complete, self-contained HTML gallery for one design system.

    `tokens` comes from `rich_tokens()` but flat/legacy token sets are
    accepted too (missing ramps degrade to fallbacks, never KeyError).
    `theme` selects which palette is the default ("light", "dark", or "both"
    — both enables the toggle, defaulting to light); an unrecognized theme
    falls back to "light" so the toggle and the dark overrides can never
    disagree. `name` is HTML-escaped: the page is served as text/html.
    When `business` carries an industry, the hero notes what the system was
    built for (issue #283) and the 3D scene matches that industry
    (issue #302); `scene_type` overrides the match. The live motion & 3D
    section runs on a dependency-free inline runtime (the showcase CSP
    forbids third-party origins, so no CDN scripts — see
    ``design_motion3d``), driven by the same motion spec and scene spec
    the downloadable ``motion.js``/``scene.js`` use.
    """
    if theme not in ("light", "dark", "both"):
        theme = "light"
    colors_obj = tokens.get("colors")
    light: dict[str, str] = {}
    dark: dict[str, str] = {}
    if isinstance(colors_obj, dict):
        light_raw = colors_obj.get("light")
        dark_raw = colors_obj.get("dark")
        if isinstance(light_raw, dict) and isinstance(dark_raw, dict):
            light = {str(k): str(v) for k, v in light_raw.items()}
            dark = {str(k): str(v) for k, v in dark_raw.items()}
        else:
            light = {str(k): str(v) for k, v in colors_obj.items()}
            dark = light
    typo_raw = tokens.get("typography")
    typo: dict[str, object] = dict(typo_raw) if isinstance(typo_raw, dict) else {}
    default_theme = "dark" if theme == "dark" else "light"

    css_vars = _css_vars(light, tokens, vibe)
    dark_css = _dark_overrides(dark) if theme != "light" else ""
    hero = _hero(vibe, name, light, business)
    # Display weight/tracking feed the hero + type scale.
    weight = vibe.display_weight

    toggle = (
        '<button class="theme-toggle" id="themeToggle" aria-label="Toggle dark mode">'
        "Dark mode</button>"
        if theme == "both"
        else ""
    )
    safe_name = html.escape(name)
    vibe_name = html.escape(vibe.name)
    vibe_tagline = html.escape(vibe.tagline)

    # Motion & 3D (issue #302): the spec from the token set's motion
    # section (or the explicit override), the scene matched to the
    # business context unless a type was passed.
    motion = motion_tokens if motion_tokens is not None else motion_tokens_from_tokens(tokens)
    motion_spec = build_motion_spec(motion)
    resolved_scene = scene_type
    if resolved_scene is None and business is not None:
        resolved_scene = scene_type_for_context(business)
    scene_spec = build_scene_spec(resolved_scene, tokens=tokens)
    motion_section = _motion_section(motion_spec, scene_spec)
    motion_script = showcase_motion3d_script(motion_spec, scene_spec)

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="{default_theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_name} — Design System Showcase</title>
<style>
{css_vars}
{dark_css}
:root{{--tw-display:{weight};--hero-weight:{weight};--hero-tracking:{vibe.tight_tracking}em}}
{_STYLES}
{reduced_motion_css()}
</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
  <span class="topbar-brand">{safe_name}</span>
  <nav><a href="#palette">Palette</a><a href="#typography">Type</a><a
    href="#components">Components</a><a href="#foundations">Foundations</a><a
    href="#motion-3d">Motion &amp; 3D</a></nav>
  {toggle}
</div></div>
{hero}
<main class="wrap">
{_palette_section(light)}
{_type_section(typo)}
{_components_section()}
{_foundations_section(tokens)}
{motion_section}
</main>
<footer class="footer wrap">
  <span>{safe_name} — generated by Baton Design Studio · {vibe_name} · {vibe_tagline}</span>
  <span>Click any swatch to copy its hex.</span>
</footer>
<div class="copied" id="copied">Copied</div>
<script>
(function(){{
  var btn=document.getElementById('themeToggle');
  if(btn){{
    var update=function(){{
      var dark=document.documentElement.dataset.theme==='dark';
      btn.textContent=dark?'Light mode':'Dark mode';
    }};
    btn.addEventListener('click',function(){{
      var el=document.documentElement;
      el.dataset.theme=el.dataset.theme==='dark'?'light':'dark';
      update();
    }});
    update();
  }}
  var note=document.getElementById('copied'),timer=null;
  document.querySelectorAll('.swatch').forEach(function(sw){{
    sw.addEventListener('click',function(){{
      var hex=sw.dataset.hex;
      function done(){{
        note.classList.add('show');
        clearTimeout(timer);timer=setTimeout(function(){{note.classList.remove('show')}},1400);
      }}
      if(navigator.clipboard&&navigator.clipboard.writeText){{
        navigator.clipboard.writeText(hex).then(done,done);
      }}else{{
        var ta=document.createElement('textarea');ta.value=hex;document.body.appendChild(ta);
        ta.select();try{{document.execCommand('copy')}}catch(e){{}}ta.remove();done();
      }}
    }});
  }});
}})();
</script>
<script>
{motion_script}
</script>
</body>
</html>
"""

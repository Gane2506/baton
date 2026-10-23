"""Showcase HTML renderer for design systems (issue #155, part 2).

`render_showcase()` produces a self-contained, no-dependency HTML gallery for
one generated design system: hero, palette visualization, typography
specimens, component showcase with tokens applied, and a dark/light toggle.
Every section embodies the vibe profile — the page itself is the proof that
the system works.
"""

from __future__ import annotations

from jev_agent.core.design_showcase import VibeProfile, _relative_luminance


def _dict_section(t: dict[str, object], key: str) -> dict[str, str]:
    """A token sub-dict with stringified values, or empty when absent/mistyped."""
    section = t.get(key)
    if not isinstance(section, dict):
        return {}
    return {str(k): str(v) for k, v in section.items()}


def _str_value(section: dict[str, str], key: str) -> str:
    return section.get(key, "")


def _css_vars(colors: dict[str, str], t: dict[str, object], vibe: VibeProfile) -> str:
    """CSS custom properties from the token set (light theme defaults)."""
    lines = [":root {"]
    for key, value in colors.items():
        lines.append(f"  --c-{key.replace('_', '-')}: {value};")
    typo = _dict_section(t, "typography")
    lines.append(f"  --font-display: {_str_value(typo, 'font_display')};")
    lines.append(f"  --font-body: {_str_value(typo, 'font_body')};")
    lines.append(f"  --font-mono: {_str_value(typo, 'font_mono')};")
    spacing = _dict_section(t, "spacing")
    for key, value in spacing.items():
        lines.append(f"  --sp-{key}: {value}px;")
    radii = _dict_section(t, "radii")
    for key, value in radii.items():
        px = "9999px" if value == "9999" else f"{value}px"
        lines.append(f"  --r-{key}: {px};")
    shadows = _dict_section(t, "shadows")
    for key, value in shadows.items():
        lines.append(f"  --sh-{key}: {value};")
    motion = _dict_section(t, "motion")
    for key, value in motion.items():
        lines.append(f"  --mo-{key.replace('_', '-')}: {value};")
    lines.append("}")
    return "\n".join(lines)


def _dark_overrides(dark: dict[str, str]) -> str:
    lines = ['[data-theme="dark"] {']
    for key, value in dark.items():
        lines.append(f"  --c-{key.replace('_', '-')}: {value};")
    # Dark shadow tuning: shadows need more presence on dark surfaces.
    lines.append("  --sh-md: 0 4px 16px rgba(0,0,0,0.35);")
    lines.append("  --sh-lg: 0 12px 32px rgba(0,0,0,0.40);")
    lines.append("}")
    return "\n".join(lines)


def _hero(vibe: VibeProfile, name: str, colors: dict[str, str]) -> str:
    """The hero section, treated per the vibe's hero_style."""
    style = vibe.hero_style
    inner = f"""
      <p class="hero-overline">Design system</p>
      <h1 class="hero-title">{name}</h1>
      <p class="hero-tagline">{vibe.tagline}</p>
      <p class="hero-desc">{vibe.description}</p>
      <div class="hero-actions">
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
        return f"""<header class="hero hero-split">
      <div class="hero-split-text">{inner}</div>
      <div class="hero-split-card" aria-hidden="true">
        <div class="split-swatch" style="background:{colors["primary"]}"></div>
        <div class="split-swatch" style="background:{colors["accent_300"]}"></div>
        <div class="split-swatch" style="background:{colors["neutral_200"]}"></div>
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
        text = "#FFFFFF" if _is_dark(value) else "#101418"
        return (
            f'<button class="swatch" data-hex="{value}" title="Click to copy">'
            f'<span class="swatch-color" style="background:{value};color:{text}">'
            f"{value}</span>"
            f'<span class="swatch-name">{name}</span></button>'
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
    <section id="palette" class="section">
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
    order = [
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
    ]
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
            return str(_d.get(field, ""))

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
    <section id="typography" class="section">
      <p class="overline">02 — Typography</p>
      <h2 class="section-title">Type scale</h2>
      <p class="section-desc">A real hierarchy: display for emotion, headlines for
      structure, body for reading, captions for metadata.</p>
      {"".join(rows)}
    </section>"""


def _components_section() -> str:
    return """
    <section id="components" class="section">
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
        f'<div class="space-demo"><span class="space-box" style="width:{v}px;height:{v}px"></span>'
        f'<span class="space-name">{k} · {v}px</span></div>'
        for k, v in spacing.items()
    )
    ra_items = []
    for k, v in radii.items():
        radius_css = "9999px" if v == "9999" else f"{v}px"
        ra_items.append(
            f'<div class="radius-demo"><span class="radius-box"'
            f' style="border-radius:{radius_css}"></span>'
            f'<span class="space-name">{k}</span></div>'
        )
    ra = "".join(ra_items)
    sh = "".join(
        f'<div class="shadow-demo"><span class="shadow-box" style="box-shadow:{v}"></span>'
        f'<span class="space-name">{k}</span></div>'
        for k, v in shadows.items()
    )
    return f"""
    <section id="foundations" class="section">
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
  font-size:76px;
  font-weight:var(--tw-display,700);
  letter-spacing:-0.025em;
  line-height:1.02;
}
.type-display-lg {
  font-family:var(--font-display);
  font-size:60px;
  font-weight:var(--tw-display,700);
  letter-spacing:-0.025em;
  line-height:1.05;
}
.type-display-md {
  font-family:var(--font-display);
  font-size:48px;
  font-weight:var(--tw-display,700);
  letter-spacing:-0.025em;
  line-height:1.08;
}
.type-display-sm {
  font-family:var(--font-display);
  font-size:36px;
  font-weight:var(--tw-display,700);
  letter-spacing:-0.025em;
  line-height:1.1;
}
.type-headline-lg {
  font-family:var(--font-display);
  font-size:28px;
  font-weight:650;
  letter-spacing:-0.01em;
  line-height:1.2;
}
.type-headline-md {
  font-family:var(--font-display);
  font-size:22px;
  font-weight:600;
  letter-spacing:-0.008em;
  line-height:1.25;
}
.type-headline-sm {
  font-family:var(--font-display);
  font-size:18px;
  font-weight:600;
  letter-spacing:-0.005em;
  line-height:1.3;
}
.type-body-lg{font-size:17px;line-height:1.6}
.type-body-md{font-size:15px;line-height:1.6}
.type-body-sm{font-size:13px;line-height:1.55;letter-spacing:.01em}
.type-caption {
  font-size:12px;
  font-weight:500;
  letter-spacing:.02em;
  line-height:1.45;
  color:var(--c-text-muted);
}
.type-overline {
  font-size:11px;
  font-weight:600;
  letter-spacing:.12em;
  line-height:1.4;
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


def render_showcase(
    name: str,
    vibe: VibeProfile,
    tokens: dict[str, object],
    theme: str = "both",
) -> str:
    """A complete, self-contained HTML gallery for one design system.

    `tokens` comes from `rich_tokens()`; `theme` selects which palette is the
    default ("light", "dark", or "both" — both enables the toggle, defaulting
    to light).
    """
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
    hero = _hero(vibe, name, light)
    # Display weight/tracking feed the hero + type scale.
    weight = vibe.display_weight

    toggle = (
        '<button class="theme-toggle" id="themeToggle" aria-label="Toggle dark mode">'
        "Dark mode</button>"
        if theme == "both"
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="{default_theme}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — Design System Showcase</title>
<style>
{css_vars}
{dark_css}
:root{{--tw-display:{weight};--hero-weight:{weight};--hero-tracking:{vibe.tight_tracking}em}}
{_STYLES}
</style>
</head>
<body>
<div class="topbar"><div class="topbar-inner">
  <span class="topbar-brand">{name}</span>
  <nav><a href="#palette">Palette</a><a href="#typography">Type</a><a
    href="#components">Components</a><a href="#foundations">Foundations</a></nav>
  {toggle}
</div></div>
{hero}
<main class="wrap">
{_palette_section(light)}
{_type_section(typo)}
{_components_section()}
{_foundations_section(tokens)}
</main>
<footer class="footer wrap">
  <span>{name} — generated by Baton Design Studio · {vibe.name} · {vibe.tagline}</span>
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
</body>
</html>
"""

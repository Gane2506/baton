"""Reusable component code generation for Design Studio systems (issue #155).

"Get reusable elements" turns a library design system into production-ready,
drop-in component code — clean, commented, and actually usable, not demo
scaffolding. Two targets:

- ``html-css``: framework-free. ``tokens.css`` (CSS custom properties from the
  design tokens, light + dark), ``components.css`` (the full component
  library), ``index.html`` (a demo page wiring it together).
- ``react-tailwind``: ``tokens.css`` (the same variables),
  ``tailwind.config.js`` (theme extension mapped onto the variables),
  ``components.jsx`` (documented React components built on Tailwind utilities
  + the variables), ``README.md`` (setup notes).

``target_for_stack()`` maps the wizard's stack choice to the closest target.
Everything is stdlib-only; the dashboard serves a per-component copy page and
a "download all" zip built by ``bundle_zip()``.
"""

from __future__ import annotations

import io
import zipfile

CODEGEN_TARGETS: dict[str, dict[str, str]] = {
    "html-css": {
        "label": "HTML + CSS",
        "description": "Framework-free: tokens.css + components.css + a demo page.",
    },
    "react-tailwind": {
        "label": "React + Tailwind",
        "description": "components.jsx + tailwind.config.js theme + tokens.css.",
    },
}

_STACK_TARGETS: tuple[tuple[str, str], ...] = (
    ("tailwind", "react-tailwind"),
    ("react", "react-tailwind"),
    ("css", "html-css"),
)


def target_for_stack(stack: str) -> str:
    """The closest codegen target for a wizard stack choice."""
    lowered = (stack or "").lower()
    for needle, target in _STACK_TARGETS:
        if needle in lowered:
            return target
    return "html-css"


def validate_target(target: str) -> str:
    """The target key, or the default when unknown."""
    return target if target in CODEGEN_TARGETS else "html-css"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _palettes(tokens: dict[str, object]) -> tuple[dict[str, str], dict[str, str]]:
    """(light, dark) flat color dicts from rich ``design_tokens``.

    Falls back to the legacy flat ``colors`` dict when rich tokens are absent
    (older library systems), using it for both themes.
    """
    rich = tokens.get("design_tokens")
    if isinstance(rich, dict):
        colors = rich.get("colors")
        if isinstance(colors, dict):
            light = colors.get("light")
            dark = colors.get("dark")
            if isinstance(light, dict) and isinstance(dark, dict):
                return _str_dict(light), _str_dict(dark)
            if isinstance(light, dict):
                flat = _str_dict(light)
                return flat, flat
    legacy = tokens.get("colors")
    flat = _str_dict(legacy) if isinstance(legacy, dict) else {}
    return flat, flat


def _str_dict(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def _section(tokens: dict[str, object], key: str) -> dict[str, object]:
    rich = tokens.get("design_tokens")
    if isinstance(rich, dict) and isinstance(rich.get(key), dict):
        section = rich[key]
        assert isinstance(section, dict)
        return dict(section)
    value = tokens.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _kebab(name: str) -> str:
    return name.replace("_", "-")


def css_variables(tokens: dict[str, object]) -> str:
    """``:root`` custom properties for the light palette plus a
    ``[data-theme="dark"]`` override block. Spacing/radii/shadows/motion and
    font stacks are included so components never hard-code values.
    """
    light, dark = _palettes(tokens)
    typo = _section(tokens, "typography")
    spacing = _section(tokens, "spacing")
    radii = _section(tokens, "radii")
    shadows = _section(tokens, "shadows")
    motion = _section(tokens, "motion")

    def px(value: object) -> str:
        return f"{value}px" if isinstance(value, (int, float)) else str(value)

    lines = [":root {"]
    for key in sorted(light):
        lines.append(f"  --color-{_kebab(key)}: {light[key]};")
    for key in ("font_display", "font_body", "font_mono"):
        if typo.get(key):
            lines.append(f"  --font-{key[5:]}: {typo[key]};")
    for key, value in spacing.items():
        lines.append(f"  --space-{_kebab(key)}: {px(value)};")
    for key, value in radii.items():
        lines.append(f"  --radius-{_kebab(key)}: {px(value)};")
    for key, value in shadows.items():
        lines.append(f"  --shadow-{_kebab(key)}: {value};")
    for key, value in motion.items():
        lines.append(f"  --motion-{_kebab(key)}: {value};")
    lines.append("}")
    if dark and dark != light:
        lines.append("")
        lines.append('[data-theme="dark"] {')
        for key in sorted(dark):
            lines.append(f"  --color-{_kebab(key)}: {dark[key]};")
        lines.append("}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# HTML + CSS target
# ---------------------------------------------------------------------------

_COMPONENTS_CSS_TEMPLATE = """\
/* Components for __NAME__ — built on tokens.css variables.
   Usage: <link rel="stylesheet" href="tokens.css">
          <link rel="stylesheet" href="components.css">
   Dark mode: set data-theme="dark" on <html>. */

/* ---------- Button ---------- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-family: var(--font-body, inherit);
  font-weight: 600;
  font-size: 15px;
  line-height: 1;
  padding: 12px 22px;
  border-radius: var(--radius-md, 8px);
  border: 1px solid transparent;
  cursor: pointer;
  text-decoration: none;
  transition: background var(--motion-duration-fast, 160ms),
    transform var(--motion-duration-fast, 160ms);
}
.btn:active { transform: translateY(1px); }
.btn:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }
.btn-primary {
  background: var(--color-primary);
  color: var(--color-primary-text, #fff);
}
.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
  box-shadow: var(--shadow-sm);
}
.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text);
  border-color: var(--color-border-strong);
}
.btn-secondary:hover:not(:disabled) { border-color: var(--color-text-faint); }
.btn-ghost { background: transparent; color: var(--color-primary); }
.btn-ghost:hover:not(:disabled) { background: var(--color-primary-subtle); }
.btn-danger { background: var(--color-danger); color: #fff; }
.btn-danger:hover:not(:disabled) { filter: brightness(0.94); }
.btn-sm { font-size: 13px; padding: 8px 16px; }
.btn-lg { font-size: 16px; padding: 14px 28px; }

/* ---------- Field (label + input/textarea/select) ---------- */
.field {
  display: grid;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
}
.field input,
.field textarea,
.field select {
  font-family: inherit;
  font-size: 15px;
  font-weight: 400;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md, 8px);
  padding: 11px 14px;
  width: 100%;
  transition: border-color var(--motion-duration-fast, 160ms),
    box-shadow var(--motion-duration-fast, 160ms);
}
.field input:focus,
.field textarea:focus,
.field select:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-subtle);
}
.field input::placeholder,
.field textarea::placeholder { color: var(--color-text-faint); }
.field .hint { font-size: 13px; font-weight: 400; color: var(--color-text-muted); }
.field .hint.error { color: var(--color-danger); }
.field input.invalid,
.field textarea.invalid,
.field select.invalid { border-color: var(--color-danger); }

/* ---------- Badge ---------- */
.badge {
  display: inline-flex;
  align-items: center;
  font-size: 12.5px;
  font-weight: 600;
  padding: 5px 12px;
  border-radius: var(--radius-full, 999px);
  line-height: 1.2;
  white-space: nowrap;
}
.badge-success {
  background: var(--color-success-subtle);
  color: var(--color-success);
}
.badge-warning {
  background: var(--color-warning-subtle);
  color: var(--color-warning);
}
.badge-danger {
  background: var(--color-danger-subtle);
  color: var(--color-danger);
}
.badge-info { background: var(--color-info-subtle); color: var(--color-info); }
.badge-muted {
  background: var(--color-surface-raised, var(--color-neutral-100));
  color: var(--color-text-muted);
}

/* ---------- Alert ---------- */
.alert {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 14px 18px;
  border-radius: var(--radius-md, 8px);
  font-size: 14.5px;
  border: 1px solid;
  color: var(--color-text);
  background: var(--color-surface);
}
.alert strong { font-weight: 700; }
.alert-success {
  background: var(--color-success-subtle);
  border-color: var(--color-success);
}
.alert-warning {
  background: var(--color-warning-subtle);
  border-color: var(--color-warning);
}
.alert-danger {
  background: var(--color-danger-subtle);
  border-color: var(--color-danger);
}
.alert-info {
  background: var(--color-info-subtle);
  border-color: var(--color-info);
}

/* ---------- Card ---------- */
.card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg, 12px);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}
.card-body { padding: 22px; }
.card-title {
  font-family: var(--font-display, inherit);
  font-size: 19px;
  font-weight: 650;
  letter-spacing: -0.01em;
  margin: 0 0 8px;
}
.card-text {
  color: var(--color-text-muted);
  font-size: 14.5px;
  margin: 0 0 16px;
}
.card-actions { display: flex; gap: 10px; }

/* ---------- Avatar ---------- */
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--color-primary);
  color: var(--color-primary-text, #fff);
  font-size: 14px;
  font-weight: 700;
  flex: none;
}
.avatar-sm { width: 28px; height: 28px; font-size: 11px; }
.avatar-lg { width: 56px; height: 56px; font-size: 19px; }

/* ---------- Modal ---------- */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  z-index: 100;
}
.modal {
  background: var(--color-surface);
  border-radius: var(--radius-lg, 12px);
  box-shadow: var(--shadow-xl);
  max-width: 480px;
  width: 100%;
  padding: 26px;
}
.modal-title {
  font-family: var(--font-display, inherit);
  font-size: 20px;
  font-weight: 700;
  margin: 0 0 8px;
}
.modal-text {
  color: var(--color-text-muted);
  font-size: 14.5px;
  margin: 0 0 20px;
}
.modal-actions { display: flex; gap: 10px; justify-content: flex-end; }

/* ---------- Navbar ---------- */
.navbar {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg, 12px);
  padding: 10px 16px;
}
.navbar-brand {
  font-family: var(--font-display, inherit);
  font-weight: 800;
  margin-right: 16px;
}
.navbar a {
  padding: 9px 15px;
  border-radius: var(--radius-md, 8px);
  text-decoration: none;
  color: var(--color-text-muted);
  font-size: 14px;
  font-weight: 500;
}
.navbar a:hover {
  color: var(--color-text);
  background: var(--color-surface-raised, var(--color-neutral-100));
}
.navbar a.active {
  color: var(--color-text);
  background: var(--color-surface-raised, var(--color-neutral-100));
  font-weight: 600;
}
.navbar .spacer { margin-left: auto; }

/* ---------- Table ---------- */
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg, 12px);
}
table.data {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
  background: var(--color-surface);
}
table.data th {
  text-align: left;
  padding: 13px 18px;
  font-size: 12px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--color-text-muted);
  font-weight: 600;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface-raised, var(--color-neutral-100));
}
table.data td {
  padding: 14px 18px;
  border-bottom: 1px solid var(--color-border);
}
table.data tr:last-child td { border-bottom: 0; }
table.data tbody tr:hover td {
  background: var(--color-surface-raised, var(--color-neutral-100));
}
table.data td.num,
table.data th.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

/* ---------- Progress ---------- */
.progress {
  height: 8px;
  border-radius: 99px;
  background: var(--color-surface-raised, var(--color-neutral-100));
  overflow: hidden;
}
.progress > span {
  display: block;
  height: 100%;
  background: var(--color-primary);
  border-radius: 99px;
}

/* ---------- Toggle (checkbox-based switch) ---------- */
.toggle {
  position: relative;
  display: inline-block;
  width: 46px;
  height: 26px;
  flex: none;
}
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle .track {
  position: absolute;
  inset: 0;
  border-radius: 99px;
  cursor: pointer;
  background: var(--color-border-strong);
  transition: background var(--motion-duration-fast, 160ms);
}
.toggle .track::before {
  content: "";
  position: absolute;
  width: 20px;
  height: 20px;
  left: 3px;
  top: 3px;
  border-radius: 50%;
  background: #fff;
  box-shadow: var(--shadow-sm);
  transition: transform var(--motion-duration-fast, 160ms);
}
.toggle input:checked + .track { background: var(--color-primary); }
.toggle input:checked + .track::before { transform: translateX(20px); }
.toggle input:focus-visible + .track {
  box-shadow: 0 0 0 3px var(--color-primary-subtle);
}
"""


def _html_css_files(name: str, tokens: dict[str, object]) -> dict[str, str]:
    files: dict[str, str] = {}
    files["tokens.css"] = (
        f"/* Design tokens for {name} — generated by Baton Design Studio.\n"
        '   Override with [data-theme="dark"] on <html> for dark mode. */\n'
        + css_variables(tokens)
    )
    files["components.css"] = _COMPONENTS_CSS_TEMPLATE.replace("__NAME__", name)
    files["index.html"] = _html_demo_page(name)
    files["README.md"] = (
        f"# {name} — reusable elements (HTML + CSS)\n\n"
        "1. Copy `tokens.css` and `components.css` into your project.\n"
        "2. Link both stylesheets (tokens first).\n"
        "3. Use the classes documented in `index.html`.\n\n"
        'Dark mode: set `data-theme="dark"` on the `<html>` element.\n'
    )
    return files


def _html_demo_page(name: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — components demo</title>
<link rel="stylesheet" href="tokens.css">
<link rel="stylesheet" href="components.css">
<style>
  body {{
    font-family: var(--font-body);
    background: var(--color-background);
    color: var(--color-text);
    margin: 0;
    padding: 40px 24px;
  }}
  .demo {{
    max-width: 880px;
    margin: 0 auto;
    display: grid;
    gap: 36px;
  }}
  .demo h2 {{
    font-family: var(--font-display);
    font-size: 22px;
    margin: 0 0 14px;
  }}
  .row {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }}
</style>
</head>
<body>
<main class="demo">
  <h1 style="font-family: var(--font-display)">{name} components</h1>

  <section><h2>Buttons</h2><div class="row">
    <button class="btn btn-primary">Primary</button>
    <button class="btn btn-secondary">Secondary</button>
    <button class="btn btn-ghost">Ghost</button>
    <button class="btn btn-danger">Danger</button>
    <button class="btn btn-primary" disabled>Disabled</button>
  </div></section>

  <section><h2>Field</h2>
    <label class="field">Email
      <input type="email" placeholder="you@example.com">
      <span class="hint">We&apos;ll never share it.</span>
    </label>
  </section>

  <section><h2>Badges</h2><div class="row">
    <span class="badge badge-success">Success</span>
    <span class="badge badge-warning">Warning</span>
    <span class="badge badge-danger">Danger</span>
    <span class="badge badge-info">Info</span>
    <span class="badge badge-muted">Muted</span>
  </div></section>

  <section><h2>Alert</h2>
    <div class="alert alert-success">
      <strong>Done.</strong> Your changes were saved.
    </div>
  </section>

  <section><h2>Card</h2>
    <article class="card"><div class="card-body">
      <h3 class="card-title">Card title</h3>
      <p class="card-text">Supporting copy goes here.</p>
      <div class="card-actions">
        <button class="btn btn-primary btn-sm">Action</button>
        <button class="btn btn-ghost btn-sm">Cancel</button>
      </div>
    </div></article>
  </section>

  <section><h2>Navbar</h2>
    <nav class="navbar">
      <span class="navbar-brand">{name}</span>
      <a href="#" class="active">Overview</a>
      <a href="#">Projects</a>
      <a href="#">Settings</a>
      <span class="spacer"></span>
      <span class="avatar avatar-sm">AK</span>
    </nav>
  </section>

  <section><h2>Table</h2>
    <div class="table-wrap"><table class="data">
      <thead><tr>
        <th>Project</th><th>Status</th><th class="num">Budget</th>
      </tr></thead>
      <tbody>
        <tr>
          <td>Atlas</td>
          <td><span class="badge badge-success">Live</span></td>
          <td class="num">$48,200</td>
        </tr>
        <tr>
          <td>Beacon</td>
          <td><span class="badge badge-warning">Review</span></td>
          <td class="num">$12,750</td>
        </tr>
      </tbody>
    </table></div>
  </section>

  <section><h2>Progress &amp; toggle</h2><div class="row">
    <div class="progress" style="width: 200px">
      <span style="width: 64%"></span>
    </div>
    <label class="toggle">
      <input type="checkbox" checked><span class="track"></span>
    </label>
  </div></section>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_files(target: str, name: str, tokens: dict[str, object]) -> dict[str, str]:
    """All generated files for `target`: filename -> content."""
    from jev_agent.core.design_codegen_react import _react_tailwind_files

    target = validate_target(target)
    if target == "react-tailwind":
        return _react_tailwind_files(name, tokens)
    return _html_css_files(name, tokens)


def bundle_zip(target: str, name: str, tokens: dict[str, object]) -> bytes:
    """A zip of every generated file, for the "download all" button."""
    files = generate_files(target, name, tokens)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(f"{name}-{validate_target(target)}/{filename}", content)
    return buffer.getvalue()


_COMPONENT_CATALOG: dict[str, list[dict[str, str]]] = {
    "html-css": [
        {
            "filename": "tokens.css",
            "title": "Design tokens",
            "description": (
                "CSS custom properties — colors, type, spacing, radii, "
                "shadows, motion. Light + dark."
            ),
        },
        {
            "filename": "components.css",
            "title": "Component library",
            "description": (
                "Buttons, fields, badges, alerts, cards, avatars, modals, "
                "navbar, tables, progress, toggles."
            ),
        },
        {
            "filename": "index.html",
            "title": "Demo page",
            "description": "A working demo wiring every component together.",
        },
        {
            "filename": "README.md",
            "title": "Setup guide",
            "description": "Three-step integration instructions.",
        },
    ],
    "react-tailwind": [
        {
            "filename": "tokens.css",
            "title": "Design tokens",
            "description": (
                "CSS custom properties backing the Tailwind theme. "
                "Import once."
            ),
        },
        {
            "filename": "tailwind.config.js",
            "title": "Tailwind theme",
            "description": (
                "theme.extend block mapping utilities onto the token variables."
            ),
        },
        {
            "filename": "components.jsx",
            "title": "React components",
            "description": (
                "Button, Field/Input/Textarea/Select, Badge, Alert, Card, "
                "Avatar, Modal, Navbar, Table, Progress, Toggle — "
                "documented props."
            ),
        },
        {
            "filename": "README.md",
            "title": "Setup guide",
            "description": "Integration steps and dark-mode notes.",
        },
    ],
}


def component_catalog(target: str) -> list[dict[str, str]]:
    """The file list for the code page: filename, title, description."""
    return _COMPONENT_CATALOG[validate_target(target)]


def codegen_summary(tokens: dict[str, object]) -> dict[str, int]:
    """Counts for the code page header: variables, components, files."""
    light, _dark = _palettes(tokens)
    return {
        "variables": len(light),
        "components": 11,
        "files": 4,
    }

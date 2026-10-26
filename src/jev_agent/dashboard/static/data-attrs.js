/* Baton web UI: values that used to live in inline style="" attributes (issue #200).
 *
 * The dashboard's Content-Security-Policy forbids inline styles, so a
 * style="width: 42%" or style="background: #abc" in a template was silently
 * dropped by the browser: stat bars rendered empty, evidence swatches blank.
 * Templates now carry the value in a data attribute instead, and this script
 * (loaded with defer from base.html, so it runs on every page) applies it
 * through the style property — which CSP does not block for scripts from
 * an allowed source. Values are validated here, never trusted blindly:
 * percentages are clamped to 0..100, colors must be a #rgb/#rrggbb hex.
 *
 * Attributes: data-bar-pct (width %), data-swatch-color (background),
 * data-bg-color (background, Design Studio landing previews), data-fg-color
 * (text color, same previews).
 */
(function () {
  "use strict";

  document.querySelectorAll("[data-bar-pct]").forEach(function (el) {
    var pct = parseFloat(el.getAttribute("data-bar-pct"));
    if (!isFinite(pct)) { pct = 0; }
    el.style.width = Math.max(0, Math.min(100, pct)) + "%";
  });

  document.querySelectorAll("[data-swatch-color]").forEach(function (el) {
    var hex = el.getAttribute("data-swatch-color") || "";
    if (/^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(hex)) {
      el.style.backgroundColor = hex;
    }
  });

  /* Design Studio landing previews (issue #281): a system's own colors, same
     mechanism — the template carries validated hex in data attributes, this
     script applies it. */
  document.querySelectorAll("[data-bg-color]").forEach(function (el) {
    var bg = el.getAttribute("data-bg-color") || "";
    if (/^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(bg)) {
      el.style.backgroundColor = bg;
    }
  });

  document.querySelectorAll("[data-fg-color]").forEach(function (el) {
    var fg = el.getAttribute("data-fg-color") || "";
    if (/^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(fg)) {
      el.style.color = fg;
    }
  });
})();

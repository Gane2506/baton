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
 * (text color, same previews), data-px-width (width px), data-px-radius
 * (border-radius px), data-shadow (box-shadow, strict shape only),
 * data-font-size (px), data-font-weight, data-line-height,
 * data-font-family (letters, digits, spaces, quotes, commas, hyphens only),
 * data-pattern (dots/grid/stripes, drawn from data-pattern-color on
 * data-pattern-bg) — the Design Studio system page's live token renderings
 * (issue #285).
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

  /* Design Studio system page (issue #285): live token renderings. Every
     value is validated before it touches the style property. */
  var hexRe = /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/;

  document.querySelectorAll("[data-px-width]").forEach(function (el) {
    var w = parseFloat(el.getAttribute("data-px-width"));
    if (isFinite(w)) { el.style.width = Math.max(0, Math.min(1200, w)) + "px"; }
  });

  document.querySelectorAll("[data-px-radius]").forEach(function (el) {
    var r = parseFloat(el.getAttribute("data-px-radius"));
    if (isFinite(r)) { el.style.borderRadius = Math.max(0, Math.min(9999, r)) + "px"; }
  });

  document.querySelectorAll("[data-shadow]").forEach(function (el) {
    var s = el.getAttribute("data-shadow") || "";
    if (s === "none" || /^0 \d+px \d+px rgba\(0,0,0,0\.\d+\)$/.test(s)) {
      el.style.boxShadow = s;
    }
  });

  document.querySelectorAll("[data-font-size]").forEach(function (el) {
    var s = parseFloat(el.getAttribute("data-font-size"));
    if (isFinite(s)) { el.style.fontSize = Math.max(8, Math.min(120, s)) + "px"; }
  });

  document.querySelectorAll("[data-font-weight]").forEach(function (el) {
    var w = parseInt(el.getAttribute("data-font-weight"), 10);
    if (isFinite(w)) { el.style.fontWeight = Math.max(100, Math.min(900, w)); }
  });

  document.querySelectorAll("[data-line-height]").forEach(function (el) {
    var lh = parseFloat(el.getAttribute("data-line-height"));
    if (isFinite(lh)) { el.style.lineHeight = Math.max(0.8, Math.min(3, lh)); }
  });

  document.querySelectorAll("[data-font-family]").forEach(function (el) {
    var f = el.getAttribute("data-font-family") || "";
    if (/^[A-Za-z0-9\s,'"\-]+$/.test(f)) { el.style.fontFamily = f; }
  });

  document.querySelectorAll("[data-ease]").forEach(function (el) {
    var e = el.getAttribute("data-ease") || "";
    if (/^cubic-bezier\([0-9., \-]+\)$/.test(e)) { el.style.transitionTimingFunction = e; }
  });

  document.querySelectorAll("[data-pattern]").forEach(function (el) {
    var kind = el.getAttribute("data-pattern") || "";
    var fg = el.getAttribute("data-pattern-color") || "";
    var bg = el.getAttribute("data-pattern-bg") || "";
    if (["dots", "grid", "stripes"].indexOf(kind) === -1) { return; }
    if (!hexRe.test(fg) || !hexRe.test(bg)) { return; }
    el.style.backgroundColor = bg;
    if (kind === "dots") {
      el.style.backgroundImage = "radial-gradient(" + fg + " 1.5px, transparent 1.5px)";
      el.style.backgroundSize = "16px 16px";
    } else if (kind === "grid") {
      el.style.backgroundImage =
        "linear-gradient(" + fg + " 1px, transparent 1px), linear-gradient(90deg, " + fg + " 1px, transparent 1px)";
      el.style.backgroundSize = "20px 20px";
    } else {
      el.style.backgroundImage = "repeating-linear-gradient(45deg, " + fg + " 0 6px, transparent 6px 14px)";
    }
  });
})();

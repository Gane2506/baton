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
})();

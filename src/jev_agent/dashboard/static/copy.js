/* Baton web UI: the reusable-elements page's Copy buttons (issue #200).
 *
 * Moved here from an inline <script> block in templates/design_system_code.html:
 * the dashboard's Content-Security-Policy forbids inline scripts, so the
 * inline block silently never ran and every Copy button was dead. Buttons
 * carry data-copy-target="<id of the <pre> to copy>"; this script (loaded
 * with defer, so the DOM is ready) wires them up the same way live.js wires
 * [data-copy] buttons elsewhere.
 */
(function () {
  "use strict";

  document.querySelectorAll(".copy-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = document.getElementById(btn.dataset.copyTarget);
      var text = target ? target.innerText : "";
      function done(ok) {
        btn.textContent = ok ? "Copied ✓" : "Copy failed";
        setTimeout(function () { btn.textContent = "Copy"; }, 1600);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () { done(true); },
          function () { done(false); }
        );
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { done(document.execCommand("copy")); } catch (e) { done(false); }
        document.body.removeChild(ta);
      }
    });
  });
})();

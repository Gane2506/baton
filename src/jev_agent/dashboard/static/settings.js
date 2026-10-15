/* Settings page: "unrestricted" needs the saved acknowledgment.
   Moved here from an inline <script> block in templates/settings.html (2026-10-15):
   the dashboard CSP forbids inline scripts, so the inline block never ran.
   Same logic, no JS dependency beyond the DOM: nudges here, enforced server-side
   (the config validator rejects "unrestricted" without the ack, so a bypassed
   check still fails). Loaded with defer from settings.html's extra_head block. */
(function () {
  "use strict";
  document.querySelectorAll("form.field-row").forEach(function (form) {
    var pathInput = form.querySelector('input[name="path"]');
    if (!pathInput || pathInput.value !== "autonomy.access_tier") return;
    var select = form.querySelector("[data-tier-select]");
    var warning = form.querySelector("[data-tier-warning]");
    function ackSaved() {
      var ack = document.querySelector("[data-ack-checkbox]");
      return !!ack && ack.getAttribute("data-ack-saved") === "true";
    }
    function syncWarning() {
      if (warning && select) warning.hidden = select.value !== "unrestricted";
    }
    if (select) select.addEventListener("change", syncWarning);
    form.addEventListener("submit", function (event) {
      if (select && select.value === "unrestricted" && !ackSaved()) {
        event.preventDefault();
        syncWarning();
        var ack = document.querySelector("[data-ack-checkbox]");
        if (ack) ack.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    });
    syncWarning();
  });
})();

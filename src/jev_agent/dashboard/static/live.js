/* Baton web UI: live regions, confirmations, copy buttons, submit guards.
 * No dependencies, no inline script (the CSP allows only this same-origin file).
 *
 * Contracts kept from v1:
 * - <div data-live="/fragments/board" data-live-every="3000"> is replaced, every few
 *   seconds, by the HTML the server renders at that URL. Updates pause while the tab is
 *   hidden or while focus is inside the region (so a form is not wiped).
 * - <form data-confirm="Really?"> asks before submitting.
 *
 * Added:
 * - [data-copy="text"] buttons copy their value and flash a check for a moment.
 * - Submitting a form disables its submit button until the page navigates (no double POST).
 */
(function () {
  "use strict";

  /* ---- live regions ------------------------------------------------------ */

  function refresh(el) {
    if (document.hidden || el.contains(document.activeElement)) {
      return;
    }
    fetch(el.getAttribute("data-live"), {
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.text();
      })
      .then(function (html) {
        if (!el.contains(document.activeElement)) {
          el.innerHTML = html;
        }
        el.removeAttribute("data-stale");
      })
      .catch(function () {
        el.setAttribute("data-stale", "true");
      });
  }

  document.querySelectorAll("[data-live]").forEach(function (el) {
    var every = parseInt(el.getAttribute("data-live-every") || "3000", 10);
    window.setInterval(function () {
      refresh(el);
    }, Math.max(every || 3000, 1000));
  });

  /* ---- confirmations ----------------------------------------------------- */

  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    // Guard against double submits: lock the button that was used.
    if (event.submitter) {
      event.submitter.disabled = true;
      window.setTimeout(function () {
        event.submitter.disabled = false;
      }, 15000);
    }
  });

  /* ---- copy buttons ------------------------------------------------------ */

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed"; // 1-file exception: no stylesheet rule can apply to this node
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
    } catch (error) {
      /* nothing copied; the button simply does not flash */
    }
    document.body.removeChild(area);
  }

  function copied(button) {
    button.classList.add("copied");
    var label = button.getAttribute("aria-label");
    button.setAttribute("aria-label", "Copied");
    window.setTimeout(function () {
      button.classList.remove("copied");
      if (label) {
        button.setAttribute("aria-label", label);
      }
    }, 1200);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) {
      return;
    }
    event.preventDefault();
    var text = button.getAttribute("data-copy");
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () {
          copied(button);
        },
        function () {
          fallbackCopy(text);
          copied(button);
        }
      );
    } else {
      fallbackCopy(text);
      copied(button);
    }
  });

  /* ---- settings page: beginner / advanced and the quality slider ---------------- */

  // The Advanced toggle reveals the full settings page; the choice survives navigation.
  var advancedToggle = document.querySelector("[data-advanced-toggle]");
  var advancedBox = document.getElementById("advanced-settings");
  function applyAdvanced() {
    if (!advancedToggle || !advancedBox) {
      return;
    }
    var show = advancedToggle.checked;
    advancedBox.hidden = !show;
    try {
      window.localStorage.setItem("jev-settings-advanced", show ? "1" : "0");
    } catch (error) {
      /* private mode: the toggle still works for this page */
    }
  }
  if (advancedToggle && advancedBox) {
    var saved = null;
    try {
      saved = window.localStorage.getItem("jev-settings-advanced");
    } catch (error) {
      /* ignore */
    }
    advancedToggle.checked = saved === "1";
    advancedToggle.addEventListener("change", applyAdvanced);
    applyAdvanced();
  }

  // Sliders keep their numeric readout in sync as they move.
  document.querySelectorAll('input[type="range"][data-slider-output]').forEach(function (el) {
    var out = document.getElementById(el.getAttribute("data-slider-output"));
    el.addEventListener("input", function () {
      if (out) {
        out.textContent = el.value;
      }
    });
  });
})();

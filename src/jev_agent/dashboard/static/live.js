/* jev dashboard: live regions and confirmations. No dependencies, no inline script needed.
 *
 * <div data-live="/fragments/board" data-live-every="3000"> is replaced, every few seconds,
 * by the HTML the server renders at that URL (escaped server-side, same origin). Updates pause
 * while the tab is hidden or while focus is inside the region (so a form is not wiped).
 * <form data-confirm="Really?"> asks before submitting.
 */
(function () {
  "use strict";

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

  document.addEventListener("submit", function (event) {
    var message = event.target.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });
})();

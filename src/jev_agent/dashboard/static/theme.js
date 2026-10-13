/* Baton web UI: light/dark theme control.
 *
 * Pages follow the OS by default (`prefers-color-scheme`). The sidebar's Theme control stores
 * an explicit choice in localStorage; this script applies it before the first paint (it is
 * loaded without `defer`, unlike live.js), so an explicit theme never flashes.
 */
(function () {
  "use strict";

  var KEY = "jev-theme";
  var root = document.documentElement;

  try {
    var stored = window.localStorage.getItem(KEY);
    if (stored === "light" || stored === "dark") {
      root.setAttribute("data-theme", stored);
    }
  } catch (error) {
    /* storage blocked: the OS setting still applies */
  }

  function current() {
    var value = root.getAttribute("data-theme");
    return value === "light" || value === "dark" ? value : "auto";
  }

  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!select || select.id !== "theme") {
      return;
    }
    var value = select.value;
    if (value === "light" || value === "dark") {
      root.setAttribute("data-theme", value);
    } else {
      root.removeAttribute("data-theme");
      value = "auto";
    }
    try {
      if (value === "auto") {
        window.localStorage.removeItem(KEY);
      } else {
        window.localStorage.setItem(KEY, value);
      }
    } catch (error) {
      /* the choice still applies for this page */
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    var select = document.getElementById("theme");
    if (select) {
      select.value = current();
    }
  });
})();

/* Baton web UI: theme profile control.
 *
 * Six choices: five named profiles (dark, light, oled, solarized,
 * high-contrast) plus "auto", which follows the OS via a CSS media query
 * (mapping to dark or light). The stored choice lives in localStorage under
 * "jev-theme"; "auto" (the default) removes the key. An explicit profile sets
 * <html data-theme="...">; "auto" removes the attribute.
 *
 * The stored choice is applied before the first paint (this script is loaded
 * without `defer`, unlike live.js), so an explicit theme never flashes.
 *
 * Public API for the sidebar (D1 owns the UI):
 *   JevTheme.list()      -> [{name, label}, ...]
 *   JevTheme.set(name)   -> the profile in effect after the call;
 *                           unknown names fall back to "auto"
 *   JevTheme.current()   -> the stored choice ("auto" when nothing stored)
 *   JevTheme.effective() -> the profile actually rendered; "auto" resolves
 *                           through the OS preference to "light" or "dark"
 *   JevTheme.sync()      -> refresh every `select#theme` to the stored choice
 */
(function () {
  "use strict";

  var KEY = "jev-theme";

  var PROFILES = [
    { name: "auto", label: "Auto (OS)" },
    { name: "dark", label: "Dark" },
    { name: "light", label: "Light" },
    { name: "oled", label: "OLED black" },
    { name: "solarized", label: "Solarized" },
    { name: "high-contrast", label: "High contrast" }
  ];

  var root = document.documentElement;

  function isProfile(name) {
    if (name === "auto") {
      return false;
    }
    return PROFILES.some(function (profile) {
      return profile.name === name;
    });
  }

  function apply(name) {
    if (isProfile(name)) {
      root.setAttribute("data-theme", name);
    } else {
      root.removeAttribute("data-theme");
    }
  }

  function store(name) {
    try {
      if (name === "auto") {
        window.localStorage.removeItem(KEY);
      } else {
        window.localStorage.setItem(KEY, name);
      }
    } catch (error) {
      /* storage blocked: the choice still applies for this page */
    }
  }

  function load() {
    try {
      var stored = window.localStorage.getItem(KEY);
      return isProfile(stored) ? stored : "auto";
    } catch (error) {
      /* storage blocked: the OS setting still applies */
      return "auto";
    }
  }

  apply(load());

  function set(name) {
    var next = isProfile(name) ? name : "auto";
    apply(next);
    store(next);
    return next;
  }

  function current() {
    var value = root.getAttribute("data-theme");
    return isProfile(value) ? value : "auto";
  }

  function effective() {
    var stored = current();
    if (stored !== "auto") {
      return stored;
    }
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
      return "light";
    }
    return "dark";
  }

  function list() {
    return PROFILES.map(function (profile) {
      return { name: profile.name, label: profile.label };
    });
  }

  function sync() {
    var selects = document.querySelectorAll("select#theme");
    var value = current();
    for (var i = 0; i < selects.length; i += 1) {
      selects[i].value = value;
    }
  }

  function populateThemeSelects() {
    var profiles = list();
    var selects = document.querySelectorAll("select#theme");
    for (var i = 0; i < selects.length; i += 1) {
      var select = selects[i];
      select.innerHTML = "";
      for (var j = 0; j < profiles.length; j += 1) {
        var option = document.createElement("option");
        option.value = profiles[j].name;
        option.textContent = profiles[j].label;
        select.appendChild(option);
      }
    }
  }

  window.JevTheme = {
    set: set,
    list: list,
    current: current,
    effective: effective,
    sync: sync
  };

  /* The sidebar's Theme control (a plain `select#theme`): keeps working no
   * matter which profile options the markup carries. Unknown values fall back
   * to "auto". */
  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!select || select.id !== "theme") {
      return;
    }
    set(select.value);
  });

  document.addEventListener("DOMContentLoaded", function () {
    populateThemeSelects();
    sync();
  });
})();

/* Command palette (Cmd+K / Ctrl+K): fuzzy search over a static registry.
 *
 * Vanilla JS, no dependencies, same-origin only (the CSP allows only this file
 * and the other vendored scripts). The registry is rendered into base.html as
 * JSON (script[type=application/json]#palette-registry): nav entries deep-link,
 * action entries POST to the whitelisted routes their pages use (with the same
 * confirm flows — Tier 2 entries open the confirm page, they never fire
 * directly), CLI-only entries are greyed and copy their command on click.
 * Progressive enhancement: without JS the button does nothing and every page
 * still works.
 */
(function () {
  "use strict";

  /* Subsequence match with a small bonus for consecutive characters; -1 when the
     query is not a subsequence of the text. */
  function fuzzy(query, text) {
    query = query.toLowerCase();
    text = text.toLowerCase();
    var qi = 0;
    var score = 0;
    var last = -1;
    for (var ti = 0; ti < text.length && qi < query.length; ti++) {
      if (text[ti] === query[qi]) {
        score += last === ti - 1 ? 2 : 1;
        last = ti;
        qi++;
      }
    }
    return qi === query.length ? score : -1;
  }

  var registryEl = document.getElementById("palette-registry");
  var dialog = document.getElementById("palette");
  var input = document.getElementById("palette-input");
  var list = document.getElementById("palette-list");
  if (!registryEl || !dialog || !input || !list) {
    return;
  }
  var registry;
  try {
    registry = JSON.parse(registryEl.textContent);
  } catch (error) {
    return;
  }
  var entries = registry.entries || [];
  var openers = document.querySelectorAll("[data-palette-open]");
  if (!openers.length) {
    return;
  }

  var current = [];
  var selected = 0;

  function kindLabel(kind) {
    return kind === "nav" ? "Page" : kind === "action" ? "Action" : "Terminal";
  }

  function render() {
    list.innerHTML = "";
    current.forEach(function (entry, index) {
      var item = document.createElement("li");
      item.setAttribute("role", "option");
      item.className =
        "palette-item kind-" + entry.kind + (index === selected ? " selected" : "");
      item.setAttribute("aria-selected", index === selected ? "true" : "false");
      var label = document.createElement("span");
      label.className = "palette-label";
      label.textContent = entry.label;
      item.appendChild(label);
      if (entry.kind === "cli" && entry.command) {
        var command = document.createElement("code");
        command.className = "palette-command";
        command.textContent = entry.command;
        item.appendChild(command);
      }
      var tag = document.createElement("span");
      tag.className = "palette-kind";
      tag.textContent = kindLabel(entry.kind);
      item.appendChild(tag);
      item.addEventListener("click", function () {
        activate(entry);
      });
      item.addEventListener("mousemove", function () {
        if (selected !== index) {
          selected = index;
          render();
        }
      });
      list.appendChild(item);
    });
    if (!current.length) {
      var empty = document.createElement("li");
      empty.className = "palette-empty";
      empty.textContent = "No matching commands.";
      list.appendChild(empty);
    }
    var active = list.querySelector(".selected");
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ block: "nearest" });
    }
  }

  function filter() {
    var query = input.value.trim();
    var scored = [];
    entries.forEach(function (entry) {
      if (!query) {
        scored.push([0, entry]);
        return;
      }
      var haystack = entry.label + " " + (entry.command || "") + " " + (entry.target || "");
      var score = fuzzy(query, haystack);
      if (score >= 0) {
        scored.push([score, entry]);
      }
    });
    scored.sort(function (a, b) {
      return b[0] - a[0];
    });
    current = scored
      .map(function (pair) {
        return pair[1];
      })
      .slice(0, 20);
    selected = 0;
    render();
  }

  function copyCommand(command) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(command).catch(function () {});
      return;
    }
    var area = document.createElement("textarea");
    area.value = command;
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
    } catch (error) {
      /* clipboard unavailable: the command stays visible in the palette */
    }
    document.body.removeChild(area);
  }

  function activate(entry) {
    if (dialog.open) {
      dialog.close();
    }
    if (entry.kind === "nav" && entry.target) {
      window.location.href = entry.target;
    } else if (entry.kind === "action" && entry.target) {
      // The same POST the page's own button sends (CSRF token included), so the
      // action inherits its route's tier and confirm flow.
      var form = document.createElement("form");
      form.method = "post";
      form.action = entry.target;
      var token = document.createElement("input");
      token.type = "hidden";
      token.name = registry.csrf_field;
      token.value = registry.csrf_token;
      form.appendChild(token);
      document.body.appendChild(form);
      form.submit();
    } else if (entry.kind === "cli" && entry.command) {
      copyCommand(entry.command);
    }
  }

  function open() {
    input.value = "";
    filter();
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    input.focus();
  }

  openers.forEach(function (opener) {
    opener.addEventListener("click", open);
  });
  document.addEventListener("keydown", function (event) {
    var modifier = event.metaKey || event.ctrlKey;
    if (modifier && (event.key === "k" || event.key === "K")) {
      event.preventDefault();
      if (dialog.open) {
        dialog.close();
      } else {
        open();
      }
    }
  });
  input.addEventListener("input", filter);
  input.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (selected < current.length - 1) {
        selected += 1;
        render();
      }
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (selected > 0) {
        selected -= 1;
        render();
      }
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (current[selected]) {
        activate(current[selected]);
      }
    }
  });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) {
      dialog.close();
    }
  });
})();

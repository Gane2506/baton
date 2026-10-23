/* Baton web UI: mission-control shell behavior.
 *
 * Vanilla JS, no dependencies, same-origin only (the CSP allows only this file
 * and live.js). Progressive enhancement: without JS every page still works —
 * panes stay visible and static, the chat form posts normally.
 *
 * What this file does:
 * - Pane collapse toggles ([data-pane-toggle]); the choice persists per pane.
 * - Drag + arrow-key resizing of the panes column ([data-resize="panes"]) and of
 *   the preview/terminal split ([data-resize="split"]); sizes persist.
 * - A refresh hook for the live panes, for workstreams E (terminal) and F
 *   (preview) to mount their fragments:
 *
 *       window.JevShell.mountPane("preview", "/fragments/preview", 3000)
 *       window.JevShell.mountPane("terminal", "/fragments/terminal", 2000)
 *       window.JevShell.unmountPane("preview")
 *
 *   mountPane polls `url` every `ms` (at least 1000) and swaps the pane's
 *   [data-pane-live] content, hiding [data-pane-empty]. Polling pauses while the
 *   tab is hidden or focus is inside the pane — the same contract as the
 *   live.js [data-live] regions. unmountPane stops polling and restores the
 *   empty state.
 * - A sidebar connection indicator (#conn-status): "Live" normally,
 *   "Reconnecting…" when a live region reports a failed refresh or the browser
 *   goes offline.
 * - Chat history sticks to the bottom on new messages (only if the reader was
 *   already near the bottom, so reading history is never yanked away).
 */
(function () {
  "use strict";

  var store = {
    get: function (key) {
      try {
        return window.localStorage.getItem(key);
      } catch (error) {
        return null;
      }
    },
    set: function (key, value) {
      try {
        window.localStorage.setItem(key, value);
      } catch (error) {
        /* private mode: the control still works for this page */
      }
    },
  };

  /* ---- pane collapse ------------------------------------------------------ */

  function setCollapsed(pane, collapsed) {
    var toggle = pane.querySelector("[data-pane-toggle]");
    pane.setAttribute("data-collapsed", collapsed ? "true" : "false");
    if (toggle) {
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      toggle.setAttribute(
        "aria-label",
        (collapsed ? "Expand " : "Collapse ") + pane.getAttribute("data-pane") + " pane"
      );
      // Flip the chevron so sighted users get a direction cue too: up while
      // expanded (collapse upward), down while collapsed (expand downward).
      var iconUse = toggle.querySelector("use");
      if (iconUse) {
        iconUse.setAttribute("href", collapsed ? "#i-down" : "#i-up");
      }
    }
    store.set("jev-shell-collapsed-" + pane.getAttribute("data-pane"), collapsed ? "1" : "0");
  }

  document.querySelectorAll("[data-pane]").forEach(function (pane) {
    var toggle = pane.querySelector("[data-pane-toggle]");
    if (!toggle) {
      return;
    }
    if (store.get("jev-shell-collapsed-" + pane.getAttribute("data-pane")) === "1") {
      setCollapsed(pane, true);
    }
    toggle.addEventListener("click", function () {
      setCollapsed(pane, pane.getAttribute("data-collapsed") !== "true");
    });
  });

  /* ---- resize ---------------------------------------------------------------
   * Pointer drag plus arrow keys. Column width clamps to 240–720px, the split
   * to 15–85% of the panes column height. Dragging is a no-op on narrow
   * screens, where the handles are hidden and panes stack.
   */

  var root = document.documentElement;
  var panesCol = document.getElementById("side-panes");

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function dragResize(handle, onDrag, onKeys) {
    handle.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) {
        return;
      }
      event.preventDefault();
      try {
        handle.setPointerCapture(event.pointerId);
      } catch (error) {
        /* older browsers: the move listener still works */
      }
      function move(ev) {
        onDrag(ev);
      }
      function done() {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", done);
        handle.removeEventListener("pointercancel", done);
      }
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", done);
      handle.addEventListener("pointercancel", done);
    });
    handle.addEventListener("keydown", function (event) {
      if (onKeys(event.key)) {
        event.preventDefault();
      }
    });
  }

  var colHandle = document.querySelector('[data-resize="panes"]');
  if (colHandle && panesCol) {
    var savedW = parseInt(store.get("jev-shell-panes-w") || "", 10);
    if (savedW >= 240 && savedW <= 720) {
      root.style.setProperty("--panes-w", savedW + "px");
      colHandle.setAttribute("aria-valuenow", String(savedW));
    }
    var stepCol = 24;
    dragResize(
      colHandle,
      function (event) {
        var w = clamp(Math.round(window.innerWidth - event.clientX), 240, 720);
        root.style.setProperty("--panes-w", w + "px");
        colHandle.setAttribute("aria-valuenow", String(w));
        store.set("jev-shell-panes-w", String(w));
      },
      function (key) {
        var current = parseInt(
          window.getComputedStyle(panesCol).width || "360",
          10
        );
        var next = null;
        if (key === "ArrowLeft") {
          next = current + stepCol; // handle is on the pane's left edge
        } else if (key === "ArrowRight") {
          next = current - stepCol;
        }
        if (next === null) {
          return false;
        }
        next = clamp(next, 240, 720);
        root.style.setProperty("--panes-w", next + "px");
        colHandle.setAttribute("aria-valuenow", String(next));
        store.set("jev-shell-panes-w", String(next));
        return true;
      }
    );
  }

  var splitHandle = document.querySelector('[data-resize="split"]');
  var previewPane = document.querySelector('[data-pane="preview"]');
  if (splitHandle && previewPane && panesCol) {
    var savedSplit = parseFloat(store.get("jev-shell-split") || "");
    if (savedSplit >= 15 && savedSplit <= 85) {
      previewPane.style.flexBasis = savedSplit + "%";
      splitHandle.setAttribute("aria-valuenow", String(Math.round(savedSplit)));
    }
    var stepSplit = 4;
    dragResize(
      splitHandle,
      function (event) {
        var rect = panesCol.getBoundingClientRect();
        var pct = clamp(((event.clientY - rect.top) / rect.height) * 100, 15, 85);
        previewPane.style.flexBasis = pct.toFixed(1) + "%";
        splitHandle.setAttribute("aria-valuenow", String(Math.round(pct)));
        store.set("jev-shell-split", pct.toFixed(1));
      },
      function (key) {
        var basis = parseFloat(previewPane.style.flexBasis || "55");
        var next = null;
        if (key === "ArrowUp") {
          next = basis - stepSplit;
        } else if (key === "ArrowDown") {
          next = basis + stepSplit;
        }
        if (next === null) {
          return false;
        }
        next = clamp(next, 15, 85);
        previewPane.style.flexBasis = next + "%";
        splitHandle.setAttribute("aria-valuenow", String(Math.round(next)));
        store.set("jev-shell-split", String(next));
        return true;
      }
    );
  }

  /* ---- live pane mounting (workstreams E/F) --------------------------------- */

  var pollers = {};

  function paneParts(name) {
    var pane = document.querySelector('[data-pane="' + name + '"]');
    if (!pane) {
      return null;
    }
    return {
      pane: pane,
      live: pane.querySelector("[data-pane-live]"),
      empty: pane.querySelector("[data-pane-empty]"),
    };
  }

  function refreshPane(name) {
    var job = pollers[name];
    if (!job || document.hidden || job.pane.contains(document.activeElement)) {
      return;
    }
    window
      .fetch(job.url, {
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
        if (!job.pane.contains(document.activeElement)) {
          job.live.innerHTML = html;
        }
        job.live.hidden = false;
        if (job.empty) {
          job.empty.hidden = true;
        }
        job.pane.removeAttribute("data-stale");
      })
      .catch(function () {
        job.pane.setAttribute("data-stale", "true");
      });
  }

  function mountPane(name, url, everyMs) {
    var parts = paneParts(name);
    if (!parts || !parts.live || typeof url !== "string" || !url) {
      return false;
    }
    unmountPane(name);
    var every = Math.max(parseInt(everyMs, 10) || 3000, 1000);
    pollers[name] = {
      pane: parts.pane,
      live: parts.live,
      empty: parts.empty,
      url: url,
      timer: window.setInterval(function () {
        refreshPane(name);
      }, every),
    };
    refreshPane(name);
    return true;
  }

  function unmountPane(name) {
    var job = pollers[name];
    if (!job) {
      return false;
    }
    window.clearInterval(job.timer);
    delete pollers[name];
    job.live.innerHTML = "";
    job.live.hidden = true;
    if (job.empty) {
      job.empty.hidden = false;
    }
    job.pane.removeAttribute("data-stale");
    return true;
  }

  window.JevShell = {
    mountPane: mountPane,
    unmountPane: unmountPane,
    togglePane: function (name, force) {
      var parts = paneParts(name);
      if (!parts) {
        return false;
      }
      var collapsed =
        typeof force === "boolean"
          ? force
          : parts.pane.getAttribute("data-collapsed") !== "true";
      setCollapsed(parts.pane, collapsed);
      return true;
    },
  };

  /* ---- connection status ------------------------------------------------------ */

  var conn = document.getElementById("conn-status");

  // Server reachability, owned by conn.js: null until its first poll lands.
  function connAggregate() {
    var api = window.JevConn;
    return api && typeof api.aggregate === "function" ? api.aggregate() : null;
  }

  function refreshConn() {
    if (!conn) {
      return;
    }
    var label = conn.querySelector(".conn-label");
    var agg = connAggregate();
    var state = "live";
    var text = "Live";
    if (!window.navigator.onLine) {
      state = "offline";
      text = "Offline";
    } else if (document.querySelector("[data-live][data-stale], [data-pane][data-stale]")) {
      state = "stale";
      text = "Reconnecting…";
    } else if (agg) {
      state = agg.state;
      text = agg.label;
    }
    conn.setAttribute("data-conn", state);
    if (label && label.textContent !== text) {
      label.textContent = text;
    }
  }

  if (conn && "MutationObserver" in window) {
    new MutationObserver(refreshConn).observe(document.body, {
      attributes: true,
      attributeFilter: ["data-stale"],
      subtree: true,
    });
  }
  window.addEventListener("online", refreshConn);
  window.addEventListener("offline", refreshConn);
  window.addEventListener("jev-conn", refreshConn); // conn.js: fresh poll results
  refreshConn();

  /* ---- chat: keep the transcript pinned to the bottom --------------------------- */

  var history = document.querySelector(".chat-history[data-live]");
  if (history && "MutationObserver" in window) {
    history.scrollTop = history.scrollHeight;
    var stick = true;
    history.addEventListener("scroll", function () {
      stick = history.scrollHeight - history.scrollTop - history.clientHeight < 80;
    });
    new MutationObserver(function () {
      if (stick) {
        history.scrollTop = history.scrollHeight;
      }
    }).observe(history, { childList: true, subtree: true });
  }
  /* ---- mobile nav drawer ----------------------------------------------------
   * Below ~1080px the sidebar is an overlay drawer opened by #nav-toggle in
   * the mobile top bar (or by the chat header's #sidebar-toggle, wired in
   * composer.js). The scrim and Escape close it; focus moves into the drawer
   * on open and returns to the opener on close. A "jev:nav" event announces
   * state changes so other scripts can keep their own toggles in sync.
   */

  (function () {
    var toggle = document.getElementById("nav-toggle");
    var scrim = document.getElementById("nav-scrim");
    var sidebar = document.getElementById("app-sidebar");
    if (!toggle || !scrim || !sidebar) {
      return;
    }
    var opener = null;

    function isOpen() {
      return document.body.classList.contains("nav-open");
    }

    function setNav(open, refocus) {
      document.body.classList.toggle("nav-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        opener = document.activeElement;
        var first = sidebar.querySelector("nav a, .sidebar-new, select");
        (first || sidebar).focus();
      } else if (refocus !== false && opener && document.contains(opener)) {
        opener.focus();
      }
      document.dispatchEvent(
        new CustomEvent("jev:nav", { detail: { open: open } })
      );
    }

    toggle.addEventListener("click", function () {
      setNav(!isOpen());
    });
    scrim.addEventListener("click", function () {
      setNav(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && isOpen()) {
        setNav(false);
      }
    });

    window.JevShell = window.JevShell || {};
    window.JevShell.nav = {
      open: function () {
        setNav(true);
      },
      close: function () {
        setNav(false);
      },
      toggle: function () {
        setNav(!isOpen());
      },
      isOpen: isOpen,
    };
  })();

  /* ---- two-level nav: submenu panels ----------------------------------------
   * A nav entry with children ([data-submenu-toggle]) opens its panel
   * (aria-controls) adjacent to the sidebar; the sidebar collapses to
   * icon-only via body.submenu-open (desktop only — see style.css). Escape
   * closes the open panel and returns focus to its toggle; clicking the
   * toggle again closes it; navigating to a non-child page is a full page
   * load, so the server renders the closed state. The Escape listener runs in
   * the capture phase so it wins over the mobile drawer's own Escape handler
   * when both a panel and the drawer are open.
   */

  (function () {
    var sidebar = document.getElementById("app-sidebar");
    var toggles = Array.prototype.slice.call(
      document.querySelectorAll("[data-submenu-toggle]")
    );
    if (!sidebar || !toggles.length) {
      return;
    }
    var DESKTOP = window.matchMedia("(min-width: 1081px)");

    function panelFor(toggle) {
      var id = toggle.getAttribute("aria-controls");
      return id ? document.getElementById(id) : null;
    }

    function isOpenToggle(toggle) {
      return toggle.getAttribute("aria-expanded") === "true";
    }

    function openSubmenu(toggle) {
      toggles.forEach(function (other) {
        if (other !== toggle && isOpenToggle(other)) {
          closeSubmenu(other, false);
        }
      });
      var panel = panelFor(toggle);
      if (!panel) {
        return;
      }
      toggle.setAttribute("aria-expanded", "true");
      panel.hidden = false;
      if (DESKTOP.matches) {
        document.body.classList.add("submenu-open");
      }
      var first = panel.querySelector("a, button");
      if (first) {
        first.focus();
      }
      document.dispatchEvent(
        new CustomEvent("jev:submenu", { detail: { open: true } })
      );
    }

    function closeSubmenu(toggle, refocus) {
      var panel = panelFor(toggle);
      if (!panel || panel.hidden) {
        return;
      }
      toggle.setAttribute("aria-expanded", "false");
      panel.hidden = true;
      document.body.classList.remove("submenu-open");
      if (refocus) {
        toggle.focus();
      }
      document.dispatchEvent(
        new CustomEvent("jev:submenu", { detail: { open: false } })
      );
    }

    toggles.forEach(function (toggle) {
      toggle.addEventListener("click", function () {
        if (isOpenToggle(toggle)) {
          closeSubmenu(toggle, false);
        } else {
          openSubmenu(toggle);
        }
      });
      toggle.addEventListener("keydown", function (event) {
        if (event.key === "ArrowDown" || event.key === "ArrowRight") {
          event.preventDefault();
          openSubmenu(toggle);
        }
      });
    });

    document.addEventListener(
      "keydown",
      function (event) {
        if (event.key !== "Escape") {
          return;
        }
        var opened = toggles.filter(isOpenToggle);
        if (opened.length) {
          event.stopPropagation();
          closeSubmenu(opened[0], true);
        }
      },
      true
    );

    // A same-page click on a plain nav link closes any open panel. (Full page
    // loads reset server-side anyway.)
    sidebar.addEventListener("click", function (event) {
      var target = event.target;
      if (target instanceof Element && target.closest("[data-submenu-toggle]")) {
        return;
      }
      toggles.forEach(function (toggle) {
        closeSubmenu(toggle, false);
      });
    });
  })();
})();

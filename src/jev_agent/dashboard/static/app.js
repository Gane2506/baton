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
})();

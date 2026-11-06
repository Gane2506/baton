/* Baton web UI: the sidebar connection-status slot and the connections panel.
 *
 * The slot (#conn-status) shows the aggregate server reachability on top of the
 * dashboard's own freshness: "All connected", "N unreachable", "Not checked yet",
 * or "Live" when no servers are configured. "Offline" (the browser is offline) and
 * "Reconnecting…" (a live fragment went stale) keep their existing meaning and win
 * over the server aggregate; that merge lives in app.js's connection-indicator area.
 *
 * Data comes from /connections/live (JSON), polled every 15 seconds. Polling pauses
 * while the tab is hidden, like the live.js regions. The endpoint only ever serves
 * cached state: a GET never opens an SSH connection (a re-check is the explicit
 * POST on the connections page).
 *
 * Contract with app.js:
 * - window.JevConn.aggregate() returns {state, label} | null. state is one of the
 *   data-conn values in shell.css ("live" / "degraded"); label is the slot text.
 * - After every poll, conn.js dispatches a "jev-conn" event so app.js re-runs its
 *   merge with the fresh aggregate.
 */
(function () {
  "use strict";

  var POLL_MS = 15000;

  var slot = document.getElementById("conn-status");
  var url = slot ? slot.getAttribute("data-conn-live") : null;
  var data = null; // the last poll result; the slot keeps showing it on failure
  var lastFlowState = null; // the codex device flow's last seen state

  function aggregate() {
    if (!data || !data.ok) {
      return null;
    }
    return {
      state: data.state === "ok" ? "live" : "degraded",
      label: data.label,
    };
  }

  function detail() {
    // "alpha: reachable · beta: unreachable" for the slot's tooltip.
    if (!data || !data.ok || !data.servers) {
      return "";
    }
    return data.servers
      .map(function (s) {
        return s.name + ": " + s.state;
      })
      .join(" · ");
  }

  function announce() {
    var tip = detail();
    if (slot && tip) {
      slot.setAttribute("title", tip);
    }
    window.dispatchEvent(new Event("jev-conn"));
  }

  function updatePanel(json) {
    // The connections page's per-server rows, refreshed from the same poll.
    if (!json.servers) {
      return;
    }
    var byName = {};
    json.servers.forEach(function (s) {
      byName[s.name] = s;
    });
    document.querySelectorAll("[data-conn-row]").forEach(function (row) {
      var s = byName[row.getAttribute("data-conn-row")];
      if (!s) {
        return;
      }
      var badge = row.querySelector(".c-state .badge");
      var checked = row.querySelector(".c-checked");
      if (badge) {
        var text =
          s.state === "reachable"
            ? "Reachable"
            : s.state === "unreachable"
              ? "Unreachable"
              : "Not checked yet";
        var cls =
          s.state === "reachable" ? "s-ok" : s.state === "unreachable" ? "s-fail" : "s-note";
        badge.textContent = text;
        badge.className = "badge " + cls;
      }
      if (checked) {
        checked.textContent = s.last_check ? new Date(s.last_check).toLocaleString() : "-";
      }
    });
    var summary = document.getElementById("conn-summary");
    if (summary) {
      summary.textContent = json.label;
    }
    updateProviders(json);
  }

  function updateProviders(json) {
    // The connections page's provider badges, refreshed from the same poll.
    // Names and set/not-set only; values never travel this endpoint.
    if (json.providers) {
      var byProvider = {};
      json.providers.forEach(function (p) {
        byProvider[p.provider] = p;
      });
      document.querySelectorAll("[data-provider-auth]").forEach(function (wrap) {
        var p = byProvider[wrap.getAttribute("data-provider-auth")];
        var badgeEl = wrap.querySelector(".badge");
        if (!p || !badgeEl) {
          return;
        }
        badgeEl.textContent = p.label;
        var cls =
          p.auth === "none" ? "s-warn" : p.auth === "api_key" ? "s-info" : "s-ok";
        badgeEl.className = "badge " + cls;
      });
      updateUsage(byProvider);
    }
    // A finished device flow changes which card the server renders, so reload
    // once when it leaves the waiting state while the waiting box is showing.
    var flow = json.codex_flow;
    if (flow && flow.state !== lastFlowState) {
      lastFlowState = flow.state;
      if (
        (flow.state === "done" ||
          flow.state === "failed" ||
          flow.state === "cancelled") &&
        document.getElementById("codex-flow-waiting")
      ) {
        window.location.reload();
      }
    }
  }

  // Issue #396: provider usage, refreshed from the same poll. The poll
  // carries the cached usage nested in each providers[] entry; this
  // updates the card's usage block in place and never triggers a fetch.
  // The reason wording lives server-side, so a failed poll shows the
  // generic D17 "Usage unavailable" line — the specific reason and next
  // step refresh on the next page load.
  var USAGE_LABELS = {
    session: "Session",
    weekly: "Weekly",
    monthly: "Monthly",
    weekly_opus: "Weekly (Opus)",
    weekly_sonnet: "Weekly (Sonnet)",
  };

  function usageRelTime(deltaMs) {
    // Mirrors the server-side D14 relative phrasing (usage/__init__.py).
    var seconds = Math.max(0, Math.floor(deltaMs / 1000));
    if (seconds < 60) {
      return "just now";
    }
    if (seconds < 3600) {
      return Math.floor(seconds / 60) + " min";
    }
    if (seconds < 86400) {
      var hours = Math.floor(seconds / 3600);
      var minutes = Math.floor((seconds % 3600) / 60);
      return minutes > 0 ? hours + " h " + minutes + " min" : hours + " h";
    }
    return Math.floor(seconds / 86400) + " d";
  }

  function usageResetText(resetsAt, now) {
    if (!resetsAt) {
      return "no reset reported";
    }
    var reset = new Date(resetsAt);
    if (reset <= now) {
      return "resetting…";
    }
    return "resets in " + usageRelTime(reset - now);
  }

  function usageCompactLine(windows, now) {
    // Mirrors the server-side compact_line: one "{Label} {left}% left ·
    // {reset}" per window, joined by " · ".
    if (!windows || windows.length === 0) {
      return "";
    }
    return windows
      .map(function (w) {
        return (
          (USAGE_LABELS[w.kind] || w.kind) +
          " " +
          w.left_percent +
          "% left · " +
          usageResetText(w.resets_at, now)
        );
      })
      .join(" · ");
  }

  function updateUsage(byProvider) {
    var now = new Date();
    document.querySelectorAll("[data-provider-usage]").forEach(function (block) {
      var p = byProvider[block.getAttribute("data-provider-usage")];
      if (!p || !p.usage) {
        return;
      }
      var u = p.usage;
      var lineEl = block.querySelector("[data-usage-line]");
      var line = "";
      if (u.state === "collecting") {
        var compact = usageCompactLine(u.windows, now);
        line = compact ? compact + " · Checking…" : "Checking…";
      } else if (u.state === "ready") {
        line = usageCompactLine(u.windows, now);
      } else if (u.state === "stale") {
        line = u.fetched_at
          ? "last seen " +
            usageRelTime(now - new Date(u.fetched_at)) +
            " — check again before a long run"
          : "last seen — check again before a long run";
      } else if (u.state === "failed") {
        line = "Usage unavailable";
      } else if (u.state === "never" && u.reason === "no_login") {
        line = "No login found — sign in first";
      } else if (u.state === "never") {
        line = "Usage not checked yet";
      }
      if (lineEl && line) {
        lineEl.textContent = line;
      }
      var byKind = {};
      (u.windows || []).forEach(function (w) {
        byKind[w.kind] = w;
      });
      var details = block.querySelector("details");
      var anyWindow = false;
      block.querySelectorAll("[data-window-bar]").forEach(function (bar) {
        var w = byKind[bar.getAttribute("data-window-kind")];
        if (!w) {
          // The window set changed (e.g. a plan change): hide the kind
          // the poll no longer carries; the next page load re-renders it.
          bar.hidden = true;
          return;
        }
        anyWindow = true;
        bar.hidden = false;
        var label = bar.querySelector("[data-window-label]");
        if (label) {
          label.textContent =
            (USAGE_LABELS[w.kind] || w.kind) + " " + w.left_percent + "% left";
        }
        var fill = bar.querySelector("[data-window-fill]");
        if (fill) {
          var pct = Math.max(0, Math.min(100, w.left_percent));
          fill.setAttribute("data-bar-pct", String(pct));
          fill.style.width = pct + "%";
        }
        var reset = bar.querySelector("[data-window-reset]");
        if (reset) {
          reset.textContent = usageResetText(w.resets_at, now);
        }
      });
      if (details) {
        details.hidden = !anyWindow;
      }
      var updatedEl = block.querySelector("[data-usage-updated]");
      if (updatedEl) {
        if (!u.fetched_at) {
          updatedEl.textContent = "";
        } else {
          var age = usageRelTime(now - new Date(u.fetched_at));
          updatedEl.textContent =
            age === "just now" ? "updated just now" : "updated " + age + " ago";
        }
      }
      var button = block.querySelector("[data-usage-button]");
      if (button) {
        if (u.state === "collecting") {
          button.textContent = "Checking…";
          button.disabled = true;
        } else {
          button.textContent = u.fetched_at ? "Refresh usage" : "Check usage";
          button.disabled = false;
        }
      }
    });
  }

  function poll() {
    if (!url || document.hidden) {
      return;
    }
    fetch(url, {
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch", Accept: "application/json" },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (json) {
        data = json;
        updatePanel(json);
        announce();
      })
      .catch(function () {
        /* unreachable dashboard: keep the last-known state on the slot */
      });
  }

  window.JevConn = { aggregate: aggregate };
  poll();
  window.setInterval(function () {
    poll();
  }, POLL_MS);
})();

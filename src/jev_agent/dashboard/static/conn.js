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

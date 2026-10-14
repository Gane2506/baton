/* Baton web UI: chat work-product panel ("run cards").
 *
 * Vanilla JS, no dependencies, same-origin only (the CSP allows only
 * same-origin scripts). Loaded with defer on the chat page; every feature
 * guards on its own elements so other pages (or partial markup) never throw.
 *
 * Backend contract (Worker 3):
 * - GET /chats/{id}/work -> {"run_id": str|null,
 *       "tabs": {"plan": {"available": bool, "active": bool}, ...}}
 * - GET /chats/{id}/work/{tab} -> HTML fragment for that tab's body
 *
 * Template contract (Worker 1):
 * - #work-panel            the panel. JS drives visibility with the `hidden`
 *                          attribute (source of truth; works with zero CSS)
 *                          plus data-collapsed (manual state) and
 *                          data-has-activity (dot hook) for styling.
 * - #work-tabs             container for the tab buttons (role="tablist"
 *                          added if missing).
 * - #work-body-{tab}       body container per tab; created if missing.
 * - #work-panel-toggle     manual collapse/expand button (wherever it lives).
 * - .work-dot              per-tab "stream active" dot, always in the DOM;
 *                          CSS shows/pulses it under .work-tab.is-live.
 *                          The dot is aria-hidden, so the button also carries
 *                          a .sr-only " (active)" text node, unhidden while
 *                          the tab's stream is active (Worker 1 adds .sr-only).
 *
 * Visibility follows the activity directive from the UX audit: the panel
 * appears when any tab is available && active, and once shown it NEVER
 * auto-hides while the page is open (the "watching" latch) — even if every
 * stream goes quiet. It hides only on manual toggle (sticky in localStorage
 * under "jev-chat-panel-collapsed") or on page navigation.
 */
(function () {
  "use strict";

  var TABS = ["plan", "diff", "tests", "preview", "terminal"];
  var TAB_LABELS = {
    plan: "Plan",
    diff: "Diff",
    tests: "Tests",
    preview: "Preview",
    terminal: "Terminal",
  };
  var POLL_MS = 2000; // work-state poll, same cadence style as live.js
  var BODY_REFRESH_MS = 3000; // re-fetch stream-active tab bodies
  var STORE_KEY = "jev-chat-panel-collapsed";

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
        /* private mode: the toggle still works for this page */
      }
    },
  };

  var panel = document.getElementById("work-panel");
  if (!panel) {
    return; // not the chat page (or the panel markup is absent): do nothing.
  }
  var tabsEl = document.getElementById("work-tabs");
  var toggle = document.getElementById("work-panel-toggle");
  var transcript = document.getElementById("chat-transcript");

  /* ---- chat id ---------------------------------------------------------------
   * Parsed from the transcript's data-live ("/chats/{id}/live"), falling back
   * to #chat-layout[data-chat-id] and the composer form action. Without an id
   * there is nothing to poll, so bail out quietly.
   */

  function chatId() {
    var layout = document.getElementById("chat-layout");
    var fromLayout = layout && layout.getAttribute("data-chat-id");
    if (fromLayout) {
      return fromLayout;
    }
    var live = transcript && transcript.getAttribute("data-live");
    var m = live && live.match(/\/chats\/([^/?#]+)/);
    if (m) {
      return m[1];
    }
    var form = document.getElementById("chat-composer");
    var action = form && form.getAttribute("action");
    var m2 = action && action.match(/\/chats\/([^/?#]+)/);
    return m2 ? m2[1] : null;
  }

  var id = chatId();
  if (!id) {
    return;
  }
  var workUrl = "/chats/" + encodeURIComponent(id) + "/work";

  /* ---- state ------------------------------------------------------------------- */

  var collapsed = store.get(STORE_KEY) === "1"; // manual state, sticky across pages
  var everShown = false; // watching latch: once true, polls never hide the panel
  var lastAnyActive = false;
  var lastAnyAvailable = false;
  var tabState = {}; // name -> {available, active}
  var selected = null; // selected tab name
  var bodies = {}; // name -> cached HTML fragment
  var fetching = {}; // name -> true while a body fetch is in flight

  function tabButton(name) {
    return tabsEl && tabsEl.querySelector('[data-work-tab="' + name + '"]');
  }

  function bodyEl(name) {
    return document.getElementById("work-body-" + name);
  }

  function ensureBody(name) {
    var body = bodyEl(name);
    if (body) {
      return body;
    }
    // The template may not render body containers up front; create them so a
    // tab click always has somewhere to paint, in the same place the template
    // would put them (right after the tab bar).
    body = document.createElement("div");
    body.id = "work-body-" + name;
    body.className = "work-body";
    body.setAttribute("role", "tabpanel");
    body.hidden = name !== selected;
    if (tabsEl && tabsEl.parentNode === panel) {
      tabsEl.insertAdjacentElement("afterend", body);
    } else {
      panel.appendChild(body);
    }
    return body;
  }

  /* ---- visibility ----------------------------------------------------------------
   * `hidden` is the source of truth for display (the UA stylesheet hides it
   * even with no CSS). data-collapsed mirrors the manual toggle for styling
   * the toggle icon; data-has-activity lets CSS put an attention dot on the
   * toggle while a manually-collapsed panel still has a live stream.
   */

  function applyPanel() {
    panel.setAttribute("data-collapsed", collapsed ? "true" : "false");
    panel.setAttribute("data-has-activity", lastAnyActive ? "true" : "false");
    if (toggle) {
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      if (!toggle.getAttribute("aria-controls")) {
        toggle.setAttribute("aria-controls", "work-panel");
      }
    }
  }

  // Start hidden: the panel appears on activity (or manual expand), never
  // before. A persisted manual collapse starts collapsed.
  panel.hidden = true;
  applyPanel();

  if (toggle) {
    toggle.addEventListener("click", function () {
      collapsed = !collapsed;
      store.set(STORE_KEY, collapsed ? "1" : "0");
      if (collapsed) {
        panel.hidden = true;
      } else if (lastAnyAvailable || everShown) {
        // Manual expand is a choice to watch: latch it like activity would,
        // so later polls can never auto-hide what the user asked to see.
        everShown = true;
        panel.hidden = false;
      }
      // Expanding with no available tabs leaves the panel hidden until the
      // next poll reports something to show — an empty panel is useless.
      applyPanel();
    });
  }

  /* ---- tabs ----------------------------------------------------------------------
   * A button renders only for tabs with available=true, in canonical order.
   * The .work-dot stays in the DOM; the button's is-live class (active=true)
   * is what CSS keys the pulsing dot off.
   */

  function insertTabButton(btn, name) {
    // Keep plan/diff/tests/preview/terminal order even when a tab disappears
    // and reappears later: insert before the next ordered sibling.
    var order = TABS.indexOf(name);
    var next = null;
    for (var i = order + 1; i < TABS.length; i++) {
      var sib = tabButton(TABS[i]);
      if (sib) {
        next = sib;
        break;
      }
    }
    if (next) {
      tabsEl.insertBefore(btn, next);
    } else {
      tabsEl.appendChild(btn);
    }
  }

  function syncTabs(tabs) {
    if (!tabsEl) {
      return;
    }
    if (!tabsEl.getAttribute("role")) {
      tabsEl.setAttribute("role", "tablist");
    }
    TABS.forEach(function (name) {
      var info = tabs[name] || { available: false, active: false };
      tabState[name] = { available: !!info.available, active: !!info.active };
      var btn = tabButton(name);
      if (!tabState[name].available) {
        if (btn) {
          btn.remove();
        }
        var old = bodyEl(name);
        if (old) {
          old.remove();
        }
        delete bodies[name];
        delete fetching[name];
        if (selected === name) {
          selected = null;
        }
        return;
      }
      if (!btn) {
        btn = document.createElement("button");
        btn.type = "button";
        btn.className = "work-tab";
        btn.setAttribute("data-work-tab", name);
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", "false");
        btn.setAttribute("aria-controls", "work-body-" + name);
        var label = document.createElement("span");
        label.className = "work-tab-label";
        label.textContent = TAB_LABELS[name];
        var dot = document.createElement("span");
        dot.className = "work-dot";
        dot.setAttribute("aria-hidden", "true");
        // A4: the pulsing dot is aria-hidden, so expose the live state to
        // assistive tech as visually-hidden text inside the button's name.
        var liveText = document.createElement("span");
        liveText.className = "sr-only";
        liveText.textContent = " (active)";
        liveText.hidden = true;
        btn.appendChild(label);
        btn.appendChild(dot);
        btn.appendChild(liveText);
        btn.addEventListener("click", function () {
          selectTab(name);
        });
        insertTabButton(btn, name);
      }
      btn.classList.toggle("is-live", tabState[name].active);
      var liveLabel = btn.querySelector(".sr-only");
      if (liveLabel) {
        liveLabel.hidden = !tabState[name].active;
      }
      btn.classList.toggle("is-selected", selected === name);
      btn.setAttribute("aria-selected", selected === name ? "true" : "false");
      ensureBody(name).hidden = selected !== name;
    });
  }

  function selectTab(name) {
    var info = tabState[name];
    if (!info || !info.available) {
      return;
    }
    selected = name;
    TABS.forEach(function (n) {
      var btn = tabButton(n);
      var body = bodyEl(n);
      var on = n === name;
      if (btn) {
        btn.classList.toggle("is-selected", on);
        btn.setAttribute("aria-selected", on ? "true" : "false");
      }
      if (body) {
        body.hidden = !on;
      }
    });
    loadBody(name);
  }

  function autoSelect(tabs) {
    var avail = TABS.filter(function (n) {
      return tabs[n] && tabs[n].available;
    });
    if (!avail.length) {
      selected = null;
      return;
    }
    // Prefer the tab with live activity; otherwise the first available.
    var live = avail.filter(function (n) {
      return tabs[n].active;
    });
    selectTab(live[0] || avail[0]);
  }

  /* ---- tab bodies ------------------------------------------------------------------
   * Bodies are cached HTML fragments. A click paints instantly from cache and
   * refreshes in the background; stream-active tabs re-fetch on a timer so a
   * later visit to the tab is already fresh.
   */

  function renderBody(name, html) {
    var body = ensureBody(name);
    // Don't yank the reader: if they were tailing the stream, keep tailing;
    // otherwise restore their scroll position across the swap.
    var nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    var prevTop = body.scrollTop;
    body.innerHTML = html;
    body.scrollTop = nearBottom ? body.scrollHeight : Math.min(prevTop, body.scrollHeight);
  }

  function loadBody(name) {
    var info = tabState[name];
    if (!info || !info.available || fetching[name]) {
      return;
    }
    if (bodies[name] !== undefined) {
      renderBody(name, bodies[name]);
    }
    fetching[name] = true;
    fetch(workUrl + "/" + name, {
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
        fetching[name] = false;
        var current = tabState[name];
        if (!current || !current.available) {
          return; // the tab went away mid-fetch; drop the fragment
        }
        bodies[name] = html;
        renderBody(name, html);
      })
      .catch(function () {
        fetching[name] = false;
      });
  }

  /* ---- polling -----------------------------------------------------------------------
   * Same contract as live.js: skip the poll while the tab is hidden. Poll
   * failures mark the panel data-stale (picked up by app.js's conn status)
   * instead of throwing.
   */

  function poll() {
    if (document.hidden) {
      return;
    }
    fetch(workUrl, {
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        var tabs = (data && data.tabs) || {};
        panel.removeAttribute("data-stale");
        syncTabs(tabs);
        lastAnyAvailable = TABS.some(function (n) {
          return tabs[n] && tabs[n].available;
        });
        lastAnyActive = TABS.some(function (n) {
          return tabs[n] && tabs[n].available && tabs[n].active;
        });
        if (lastAnyActive && !collapsed && !everShown) {
          // Watching latch: once the user sees activity, never auto-hide.
          everShown = true;
          panel.hidden = false;
        }
        applyPanel();
        // Never steal the user's selection on later polls; only pick when
        // nothing is selected or the selected tab went away.
        if (selected === null || !(tabs[selected] && tabs[selected].available)) {
          autoSelect(tabs);
        }
      })
      .catch(function () {
        panel.setAttribute("data-stale", "true");
      });
  }

  window.setInterval(poll, POLL_MS);
  poll();

  // Keep stream-active tabs fresh while they stream; skip while hidden or
  // manually collapsed (there is nothing visible to keep fresh).
  window.setInterval(function () {
    if (document.hidden || collapsed) {
      return;
    }
    TABS.forEach(function (name) {
      var info = tabState[name];
      if (info && info.available && info.active) {
        loadBody(name);
      }
    });
  }, BODY_REFRESH_MS);
})();

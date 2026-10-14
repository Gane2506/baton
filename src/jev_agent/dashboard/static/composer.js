/* Baton web UI: chat composer and fullscreen-chat chrome.
 *
 * Vanilla JS, no dependencies, same-origin only (the CSP allows only
 * same-origin scripts). Loaded with defer on the chat page. Each behavior
 * guards on its own elements, so pages without the chat markup (or with only
 * part of it) never throw. No-JS fallback is untouched: the forms are real
 * POSTs and everything here is progressive enhancement.
 *
 * Template contract (Worker 1):
 * - #chat-composer   the message form (normal POST to /chats/{id}/send)
 * - #composer-text   the textarea
 * - #composer-send   submit button; #composer-stop is hidden unless busy
 * - #chat-transcript the transcript (data-live polling, like live.js)
 * - #suggestion-chips container of <button> suggestion chips
 * - #rail-toggle / #chat-rail            collapsible conversation rail
 * - #sidebar-toggle                      toggles body.chat-fullscreen
 *
 * Retry buttons are plain form POSTs already — deliberately no JS here.
 * <details class="step-block"> is native — no JS needed for expand/collapse.
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
        /* private mode: the toggle still works for this page */
      }
    },
  };

  /* ---- chat id -----------------------------------------------------------------
   * Same extraction as chat.js: the transcript's data-live
   * ("/chats/{id}/live"), then #chat-layout[data-chat-id], then the composer
   * form action.
   */

  function chatId() {
    var transcript = document.getElementById("chat-transcript");
    var live = transcript && transcript.getAttribute("data-live");
    var m = live && live.match(/\/chats\/([^/?#]+)/);
    if (m) {
      return m[1];
    }
    var layout = document.getElementById("chat-layout");
    var fromLayout = layout && layout.getAttribute("data-chat-id");
    if (fromLayout) {
      return fromLayout;
    }
    var form = document.getElementById("chat-composer");
    var action = form && form.getAttribute("action");
    var m2 = action && action.match(/\/chats\/([^/?#]+)/);
    return m2 ? m2[1] : null;
  }

  /* ---- enter-to-send ------------------------------------------------------------- */

  var form = document.getElementById("chat-composer");
  var text = document.getElementById("composer-text");
  var send = document.getElementById("composer-send");
  var stop = document.getElementById("composer-stop");

  if (form && text) {
    text.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" || event.shiftKey) {
        return; // Shift+Enter keeps its native newline
      }
      if (event.isComposing) {
        return; // IME composition: Enter confirms the composition, not send
      }
      // Don't hijack when the send button is disabled (a turn is already
      // running or the form is otherwise not submittable).
      if (send && send.disabled) {
        return;
      }
      event.preventDefault();
      // requestSubmit fires the submit event (and native validation, so an
      // empty required textarea still blocks). Passing the send button keeps
      // live.js's double-submit guard working via event.submitter.
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit(send || undefined);
      } else {
        form.submit();
      }
    });
  }

  /* ---- optimistic echo + typing indicator + stop -----------------------------------
   * The form still POSTs normally; this only improves the wait before the
   * browser navigates. If navigation happens, the optimistic nodes vanish
   * with the page — harmless. (Known race: the transcript's own data-live
   * poll can replace innerHTML and wipe the echo before navigation; the next
   * poll restores the server-rendered message, so it self-heals.)
   */

  var transcript = document.getElementById("chat-transcript");

  function restoreButtons() {
    if (stop) {
      stop.hidden = true;
      stop.disabled = false;
    }
    if (send) {
      send.hidden = false;
    }
  }

  function removeTyping() {
    var typing = transcript && transcript.querySelector('[data-typing="true"]');
    if (typing) {
      typing.remove();
    }
  }

  if (form && text && transcript) {
    form.addEventListener("submit", function () {
      if (!text.value.trim()) {
        return; // let native validation handle the empty case
      }
      // Optimistic user turn. textContent, never innerHTML: the message is
      // untrusted user input and must not be parsed as markup.
      var turn = document.createElement("div");
      turn.className = "turn turn--user";
      turn.setAttribute("data-optimistic", "true");
      var body = document.createElement("div");
      body.className = "turn-body";
      body.textContent = text.value;
      turn.appendChild(body);
      transcript.appendChild(turn);

      // Typing indicator until the next transcript poll replaces it.
      var typing = document.createElement("div");
      typing.className = "turn turn--assistant";
      typing.setAttribute("data-optimistic", "true");
      typing.setAttribute("data-typing", "true");
      var dots = document.createElement("div");
      dots.className = "typing-indicator";
      dots.setAttribute("aria-hidden", "true");
      for (var i = 0; i < 3; i++) {
        dots.appendChild(document.createElement("span"));
      }
      var sr = document.createElement("span");
      sr.className = "sr-only";
      sr.textContent = "Waiting for the model…";
      typing.appendChild(dots);
      typing.appendChild(sr);
      transcript.appendChild(typing);

      transcript.scrollTop = transcript.scrollHeight;

      // Swap send -> stop for the duration of the wait.
      if (send) {
        send.hidden = true;
      }
      if (stop) {
        stop.hidden = false;
      }
    });
  }

  // The stop button posts to /chats/{id}/stop via fetch (carrying the form's
  // CSRF token like a real form would) and then restores the buttons. The
  // transcript itself refreshes through its normal data-live poll.
  if (stop && form) {
    stop.addEventListener("click", function (event) {
      event.preventDefault();
      var id = chatId();
      if (!id) {
        restoreButtons();
        return;
      }
      stop.disabled = true;
      var tokenInput =
        form.querySelector('input[name="csrf_token"]') ||
        form.querySelector('input[name*="csrf" i]');
      var params = new URLSearchParams();
      if (tokenInput && tokenInput.value) {
        params.append("csrf_token", tokenInput.value);
      }
      fetch("/chats/" + encodeURIComponent(id) + "/stop", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
        body: params,
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("HTTP " + response.status);
          }
        })
        .catch(function () {
          /* the turn may already be over; restore the buttons anyway */
        })
        .then(function () {
          restoreButtons();
          removeTyping();
        });
    });
  }

  /* ---- suggestion chips ------------------------------------------------------------
   * A chip copies its text into the composer (replacing any draft) and moves
   * focus there with the caret at the end, ready to edit or send.
   */

  var chips = document.getElementById("suggestion-chips");
  if (chips && text) {
    chips.addEventListener("click", function (event) {
      var button = event.target.closest("button");
      if (!button || !chips.contains(button)) {
        return;
      }
      var suggestion = button.textContent.trim();
      if (!suggestion) {
        return;
      }
      text.value = suggestion;
      text.focus();
      try {
        text.setSelectionRange(text.value.length, text.value.length);
      } catch (error) {
        /* caret placement is a nicety; the text is already in place */
      }
    });
  }

  /* ---- rail + fullscreen toggles ------------------------------------------------------
   * #rail-toggle collapses the conversation rail (persisted); #sidebar-toggle
   * toggles the fullscreen chat class on <body> (persisted). Both apply their
   * saved state on load so navigation restores the user's last choice.
   */

  var railToggle = document.getElementById("rail-toggle");
  var rail = document.getElementById("chat-rail");
  if (railToggle && rail) {
    var railCollapsed = store.get("jev-chat-rail-collapsed") === "1";
    rail.setAttribute("data-collapsed", railCollapsed ? "true" : "false");
    railToggle.setAttribute("aria-expanded", railCollapsed ? "false" : "true");
    railToggle.addEventListener("click", function () {
      railCollapsed = !railCollapsed;
      store.set("jev-chat-rail-collapsed", railCollapsed ? "1" : "0");
      rail.setAttribute("data-collapsed", railCollapsed ? "true" : "false");
      railToggle.setAttribute("aria-expanded", railCollapsed ? "false" : "true");
    });
  }

  var sidebarToggle = document.getElementById("sidebar-toggle");
  if (sidebarToggle) {
    var fullscreen = store.get("jev-chat-fullscreen") === "1";
    if (fullscreen) {
      document.body.classList.add("chat-fullscreen");
    }
    sidebarToggle.setAttribute(
      "aria-pressed",
      document.body.classList.contains("chat-fullscreen") ? "true" : "false"
    );
    sidebarToggle.addEventListener("click", function () {
      var on = document.body.classList.toggle("chat-fullscreen");
      store.set("jev-chat-fullscreen", on ? "1" : "0");
      sidebarToggle.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  /* ---- deliberately no JS --------------------------------------------------------------
   * - Retry buttons are plain form POSTs; nothing to enhance.
   * - <details class="step-block"> expands/collapses natively with full
   *   keyboard support; an "expand all / collapse all" toggle is skipped —
   *   without knowing the transcript's shape it would add chrome for a
   *   marginal gain, and native <details> is the accessible baseline.
   */
})();

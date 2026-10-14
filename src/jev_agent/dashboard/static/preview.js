/* Agent browser preview: takeover controls for the Preview pane.
 *
 * The pane itself is mounted with
 *     window.JevShell.mountPane("preview", "/preview/live", 3000)
 * (static/app.js); this file only wires the Take over / Hand back buttons, the
 * typed-text field and click-to-click on the screenshot. It is loaded by
 * base.html alongside the other same-origin scripts (the CSP allows no inline
 * script). Everything degrades to the empty pane when no session is live.
 *
 * mountPane pauses its polling while focus is inside the pane, so typing is
 * never wiped by a refresh; after a takeover/hand-back the next poll re-renders
 * the fragment with the new controls. Typed text is sent with kind "type" and
 * cleared locally, never echoed back.
 */
(function () {
  "use strict";

  function pane() {
    return document.querySelector('[data-pane="preview"]');
  }

  function csrf(root) {
    var host = root.querySelector("[data-preview]");
    return host ? host.getAttribute("data-csrf") || "" : "";
  }

  function post(root, url, fields) {
    var form = new FormData();
    form.append("csrf_token", csrf(root));
    Object.keys(fields).forEach(function (key) {
      form.append(key, fields[key]);
    });
    return window
      .fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
        body: form,
      })
      .then(function (response) {
        if (!response.ok) {
          return response.text().then(function (text) {
            throw new Error(text || "HTTP " + response.status);
          });
        }
        return response.json();
      });
  }

  function showError(root, message) {
    var el = root.querySelector("[data-preview-error]");
    if (!el) {
      return;
    }
    el.textContent = "Preview: " + message;
    el.hidden = false;
    window.setTimeout(function () {
      el.hidden = true;
    }, 6000);
  }

  function sendType(root, input) {
    var text = input.value;
    if (!text) {
      return;
    }
    post(root, "/preview/input", { kind: "type", text: text }).then(
      function () {
        input.value = "";
        input.focus();
      },
      function (err) {
        showError(root, err.message);
      }
    );
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) {
      return;
    }
    var root = pane();
    if (!root || !root.contains(target)) {
      return;
    }
    var actionEl = target.closest("[data-preview-action]");
    if (actionEl) {
      var action = actionEl.getAttribute("data-preview-action");
      if (action === "takeover" || action === "handback") {
        post(root, "/preview/" + action, {}).then(
          function () {
            /* the next pane poll re-renders with the new driver state */
          },
          function (err) {
            showError(root, err.message);
          }
        );
      } else if (action === "send") {
        var input = root.querySelector("[data-preview-type]");
        if (input) {
          sendType(root, input);
        }
      }
      return;
    }
    var frame = target.closest("[data-preview-clickable]");
    if (frame) {
      var rect = frame.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) {
        return;
      }
      var x = (event.clientX - rect.left) / rect.width;
      var y = (event.clientY - rect.top) / rect.height;
      post(root, "/preview/input", {
        kind: "click",
        x: x.toFixed(4),
        y: y.toFixed(4),
      }).then(
        function () {
          /* clicks need no local echo */
        },
        function (err) {
          showError(root, err.message);
        }
      );
    }
  });

  document.addEventListener("keydown", function (event) {
    var target = event.target;
    if (!target || !target.closest) {
      return;
    }
    var input = target.closest("[data-preview-type]");
    if (!input) {
      return;
    }
    var root = pane();
    if (!root || !root.contains(input)) {
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      sendType(root, input);
    } else if (event.key === "Escape" || event.key === "Tab") {
      // Forward navigation keys while keeping focus, so typing can continue.
      event.preventDefault();
      post(root, "/preview/input", { kind: "key", key: event.key }).then(
        function () {},
        function (err) {
          showError(root, err.message);
        }
      );
    }
  });

  /* Mount the Preview pane: the /preview/live fragment renders the empty state
   * itself when no browser session is registered. */
  if (window.JevShell && typeof window.JevShell.mountPane === "function") {
    window.JevShell.mountPane("preview", "/preview/live", 3000);
  }
})();

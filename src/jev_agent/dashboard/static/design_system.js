/* Design Studio system page interactions (issue #285).
 *
 * Sidebar search filter, per-section copy buttons, the toast demo, the
 * ticker pause control, tooltip Escape dismissal, and the VideoModal /
 * component Modal dialogs. No inline scripts: everything is wired here.
 */
(function () {
  "use strict";

  /* Sidebar search: filter the nav items by label; the Files row stays. */
  var search = document.getElementById("ds-nav-search");
  if (search) {
    search.addEventListener("input", function () {
      var needle = search.value.trim().toLowerCase();
      document.querySelectorAll(".ds-nav-list li[data-nav-label]").forEach(function (li) {
        var label = (li.getAttribute("data-nav-label") || "").toLowerCase();
        li.classList.toggle("ds-nav-hidden", needle !== "" && label.indexOf(needle) === -1);
      });
    });
  }

  /* Section copy buttons: copy the section's JSON from its hidden <pre>.
     The original icon is kept (unlike copy.js, which swaps button text). */
  document.querySelectorAll(".ds-copy-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = document.getElementById(btn.getAttribute("data-copy-target") || "");
      var text = target ? target.textContent : "";
      function done(ok) {
        btn.classList.toggle("copied", ok);
        btn.setAttribute("aria-label", ok ? "Copied" : "Copy failed");
        btn.title = ok ? "Copied" : "Copy failed";
        setTimeout(function () {
          btn.classList.remove("copied");
          btn.setAttribute("aria-label", btn.getAttribute("data-copy-label") || "Copy section");
          btn.title = btn.getAttribute("data-copy-label") || "Copy section";
        }, 1600);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () { done(true); },
          function () { done(false); }
        );
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { done(document.execCommand("copy")); } catch (e) { done(false); }
        document.body.removeChild(ta);
      }
    });
  });

  /* Ticker pause: the button toggles the marquee's paused state. */
  var ticker = document.getElementById("ds-ticker");
  var tickerBtn = document.getElementById("ds-ticker-pause");
  if (ticker && tickerBtn) {
    tickerBtn.addEventListener("click", function () {
      var paused = ticker.classList.toggle("paused");
      tickerBtn.textContent = paused ? "Resume ticker" : "Pause ticker";
      tickerBtn.setAttribute("aria-pressed", paused ? "true" : "false");
    });
  }

  /* Toast demo: "Show another toast" cycles Information / Success / Error
     into the live region; each toast's × dismisses it. */
  var toastLive = document.getElementById("ds-toast-live");
  var toastBtn = document.getElementById("ds-toast-more");
  var toastKinds = [
    { cls: "ds-toast-info", strong: "Information.", text: "Your session ends in 5 minutes." },
    { cls: "ds-toast-success", strong: "Success.", text: "Your message has been sent." },
    { cls: "ds-toast-error", strong: "Error.", text: "The file is larger than 5 MB. Upload a smaller file." },
  ];
  var toastIndex = 0;
  function dismissToast(btn) {
    var toast = btn.closest(".ds-toast");
    if (toast) { toast.remove(); }
  }
  document.querySelectorAll(".ds-toast [data-dismiss]").forEach(function (btn) {
    btn.addEventListener("click", function () { dismissToast(btn); });
  });
  if (toastLive && toastBtn) {
    toastBtn.addEventListener("click", function () {
      var kind = toastKinds[toastIndex % toastKinds.length];
      toastIndex += 1;
      var toast = document.createElement("div");
      toast.className = "ds-toast " + kind.cls;
      toast.setAttribute("role", "status");
      var strong = document.createElement("strong");
      strong.textContent = kind.strong;
      var span = document.createElement("span");
      span.textContent = " " + kind.text;
      var close = document.createElement("button");
      close.type = "button";
      close.setAttribute("data-dismiss", "");
      close.setAttribute("aria-label", "Dismiss toast");
      close.textContent = "×";
      close.addEventListener("click", function () { dismissToast(close); });
      toast.appendChild(strong);
      toast.appendChild(span);
      toast.appendChild(close);
      toastLive.appendChild(toast);
    });
  }

  /* Tooltips: Escape dismisses any open bubble (the bubbles show on hover /
     focus via CSS; this only handles the keyboard dismissal the reference
     describes). */
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      var focused = document.activeElement;
      if (focused && focused.blur) { focused.blur(); }
      closeModal("ds-video-modal");
      closeModal("ds-component-modal");
    }
  });

  /* Modals: open/close by id; backdrop click and the close button dismiss. */
  function openModal(id) {
    var modal = document.getElementById(id);
    if (modal) { modal.hidden = false; }
  }
  function closeModal(id) {
    var modal = document.getElementById(id);
    if (modal) { modal.hidden = true; }
  }
  document.querySelectorAll("[data-modal-open]").forEach(function (btn) {
    btn.addEventListener("click", function () { openModal(btn.getAttribute("data-modal-open")); });
  });
  document.querySelectorAll("[data-modal-close]").forEach(function (btn) {
    btn.addEventListener("click", function () { closeModal(btn.getAttribute("data-modal-close")); });
  });
  document.querySelectorAll(".ds-modal-backdrop").forEach(function (backdrop) {
    backdrop.addEventListener("click", function (event) {
      if (event.target === backdrop) { backdrop.hidden = true; }
    });
  });
})();

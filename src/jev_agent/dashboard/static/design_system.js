/* Design Studio system page interactions (issue #285).
 *
 * Sidebar search filter, per-section copy buttons, copy-per-value on
 * individual tokens (issue #289), the toast demo, the ticker pause
 * control, tooltip Escape dismissal, and the VideoModal / component
 * Modal dialogs. No inline scripts: everything is wired here.
 */
(function () {
  "use strict";

  /* Scroll-spy (issue #287): highlight the sidebar link for the section
     currently in view. IntersectionObserver with a band near the top of the
     viewport; a click sets the active link immediately, and the hash on
     load is honoured too. The search filter keeps working: a hidden link
     simply stays hidden. */
  var navLinks = Array.prototype.slice.call(
    document.querySelectorAll(".ds-nav-list a[href^=\"#\"]")
  );
  function setActiveNav(id) {
    navLinks.forEach(function (link) {
      link.classList.toggle("active", link.getAttribute("href") === "#" + id);
    });
  }
  var sections = Array.prototype.slice.call(
    document.querySelectorAll(".ds-section[id]")
  );
  if ("IntersectionObserver" in window && sections.length > 0) {
    var spy = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) { setActiveNav(entry.target.id); }
        });
      },
      { rootMargin: "-15% 0px -75% 0px" }
    );
    sections.forEach(function (section) { spy.observe(section); });
  }
  navLinks.forEach(function (link) {
    link.addEventListener("click", function () {
      setActiveNav(link.getAttribute("href").slice(1));
    });
  });
  if (window.location.hash) {
    setActiveNav(window.location.hash.slice(1));
  }

  /* Light/dark preview toggle (issue #287): re-skin the content area in
     the system's own light or dark theme. The exact palette travels in
     data-preview-*-* attributes on #ds-content (validated hex only); the
     ds-preview-light/dark classes carry generic fallbacks for no-JS. */
  var content = document.getElementById("ds-content");
  var previewBtns = Array.prototype.slice.call(
    document.querySelectorAll("[data-preview-theme]")
  );
  var hexRe = /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/;
  function applyPreview(theme) {
    if (!content) { return; }
    content.classList.toggle("ds-preview-light", theme === "light");
    content.classList.toggle("ds-preview-dark", theme === "dark");
    var prefix = theme === "light" ? "previewLight" : "previewDark";
    var vars = {
      "--ds-bg": content.dataset[prefix + "Bg"],
      "--ds-surface": content.dataset[prefix + "Surface"],
      "--ds-text": content.dataset[prefix + "Text"],
      "--ds-muted": content.dataset[prefix + "Muted"],
      "--ds-border": content.dataset[prefix + "Border"],
    };
    Object.keys(vars).forEach(function (name) {
      var value = vars[name] || "";
      if (hexRe.test(value)) { content.style.setProperty(name, value); }
    });
    previewBtns.forEach(function (btn) {
      btn.setAttribute(
        "aria-pressed",
        btn.getAttribute("data-preview-theme") === theme ? "true" : "false"
      );
    });
  }
  previewBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      applyPreview(btn.getAttribute("data-preview-theme"));
    });
  });
  if (content && previewBtns.length > 0) {
    var pressed = previewBtns.filter(function (btn) {
      return btn.getAttribute("aria-pressed") === "true";
    })[0];
    applyPreview(
      pressed ? pressed.getAttribute("data-preview-theme") : "dark"
    );
  }

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

  /* Shared clipboard write: navigator.clipboard with a textarea fallback
     for non-secure contexts — and for when the Clipboard API is present
     but rejects (issue #292: headless Chrome has the API and denies the
     write, so the fallback must run on rejection too, not only when the
     API is absent). */
  function copyText(text, done) {
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { done(document.execCommand("copy")); } catch (e) { done(false); }
      document.body.removeChild(ta);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { done(true); },
        function () { fallback(); }
      );
    } else {
      fallback();
    }
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
      copyText(text, done);
    });
  });

  /* Copy per value (issue #289): click a color swatch or hex, a token row,
     or a type specimen's spec to copy just that value. A small "Copied"
     bubble floats above the clicked value; Enter/Space do the same for
     keyboard users (the rows carry role="button" + tabindex). */
  var copyTip = null;
  var copyTipTimer = null;
  function showCopyTip(el, ok) {
    if (copyTip) { copyTip.remove(); copyTip = null; }
    if (copyTipTimer) { clearTimeout(copyTipTimer); copyTipTimer = null; }
    var tip = document.createElement("div");
    tip.className = "ds-copy-tip";
    tip.textContent = ok ? "Copied" : "Copy failed";
    document.body.appendChild(tip);
    var rect = el.getBoundingClientRect();
    var left = rect.left + rect.width / 2;
    left = Math.max(48, Math.min(window.innerWidth - 48, left));
    tip.style.left = left + "px";
    tip.style.top = Math.max(24, rect.top + 6) + "px";
    copyTip = tip;
    copyTipTimer = setTimeout(function () {
      tip.remove();
      if (copyTip === tip) { copyTip = null; }
    }, 1200);
  }
  function copyValue(el) {
    var value = el.getAttribute("data-copy-value") || "";
    if (!value) { return; }
    copyText(value, function (ok) {
      el.classList.toggle("copied", ok);
      showCopyTip(el, ok);
      setTimeout(function () { el.classList.remove("copied"); }, 1200);
    });
  }
  document.querySelectorAll("[data-copy-value]").forEach(function (el) {
    el.addEventListener("click", function () { copyValue(el); });
    el.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        copyValue(el);
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

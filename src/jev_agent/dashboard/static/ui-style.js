/* Baton web UI: UI style picker (workstream UI-02, issue #116).
 *
 * The sidebar's `select#ui-style` lives in a plain POST form (so the picker
 * keeps working with JavaScript disabled: change the select and submit). With
 * JavaScript, a change applies the style instantly via `data-ui-style` and
 * persists it with a background POST; a failed POST falls back to submitting
 * the form, which surfaces the server's flash message.
 */
(function () {
  "use strict";

  var root = document.documentElement;

  function current() {
    return root.getAttribute("data-ui-style") || "bento";
  }

  function apply(slug) {
    root.setAttribute("data-ui-style", slug);
  }

  function sync() {
    var selects = document.querySelectorAll("select#ui-style");
    var value = current();
    for (var i = 0; i < selects.length; i += 1) {
      selects[i].value = value;
    }
  }

  function persist(form) {
    /* The form already carries the CSRF token; fetch just avoids the reload. */
    fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
      redirect: "follow"
    }).then(function (response) {
      if (!response.ok) {
        form.submit();
      }
    }).catch(function () {
      form.submit();
    });
  }

  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!select || select.id !== "ui-style") {
      return;
    }
    var slug = select.value;
    apply(slug); /* instant: the server-rendered attribute already matches */
    var form = select.form || document.getElementById("ui-style-form");
    if (form) {
      persist(form);
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    sync();
  });

  window.JevUiStyle = {
    current: current,
    apply: apply,
    sync: sync
  };
})();

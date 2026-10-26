/* Tools page APIs: the Connect-an-API form shows the local-command fields
 * or the remote-URL fields, never both. The hidden group's inputs are
 * disabled so they are not submitted (an empty value parses as UNSET
 * anyway). Vanilla JS, no dependencies. Progressive enhancement: without JS
 * both groups show and the server validates exactly one of command/url.
 */
(function () {
  "use strict";

  var form = document.getElementById("api-connect-form");
  if (!form) {
    return;
  }
  var groups = {
    local: document.getElementById("api-connect-local"),
    remote: document.getElementById("api-connect-remote"),
  };
  if (!groups.local || !groups.remote) {
    return;
  }

  function show(kind) {
    Object.keys(groups).forEach(function (key) {
      var group = groups[key];
      var active = key === kind;
      group.hidden = !active;
      Array.prototype.forEach.call(
        group.querySelectorAll("input, select, textarea"),
        function (el) {
          el.disabled = !active;
        },
      );
    });
  }

  Array.prototype.forEach.call(
    form.querySelectorAll('input[name="api_kind"]'),
    function (radio) {
      radio.addEventListener("change", function () {
        show(radio.value);
      });
    },
  );

  var checked = form.querySelector('input[name="api_kind"]:checked');
  show(checked ? checked.value : "remote");
})();

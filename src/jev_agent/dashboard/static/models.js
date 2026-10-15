/* Models browser: filter the models table as you type.
 *
 * Vanilla JS, no dependencies, same-origin only (the CSP allows only this file
 * and the other vendored scripts). Progressive enhancement: without JS the full
 * table still renders and the filter input simply does nothing.
 */
(function () {
  "use strict";

  var input = document.getElementById("models-filter");
  var table = document.getElementById("models-table");
  if (!input || !table) {
    return;
  }
  var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr"));
  var empty = document.getElementById("models-empty");

  input.addEventListener("input", function () {
    var query = input.value.toLowerCase();
    var shown = 0;
    rows.forEach(function (row) {
      var hit = row.textContent.toLowerCase().indexOf(query) !== -1;
      row.style.display = hit ? "" : "none";
      if (hit) {
        shown += 1;
      }
    });
    if (empty) {
      empty.hidden = shown !== 0;
    }
  });
})();

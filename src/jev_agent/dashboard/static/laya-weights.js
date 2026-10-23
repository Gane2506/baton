/* Learning page: live sum validation for the Laya weights editor.
   The dashboard CSP forbids inline scripts, so this lives here and is loaded
   from learning.html's extra_head block. Progressive enhancement: without JS
   the form still posts and the server validates the sum (the config validator
   rejects anything that does not add up to 1). */
(function () {
  "use strict";
  var form = document.getElementById("laya-weights-form");
  if (!form) return;
  var inputs = Array.prototype.slice.call(form.querySelectorAll("[data-laya-weight]"));
  var sumLine = document.getElementById("laya-weights-sum");
  var save = document.getElementById("laya-weights-save");
  if (!inputs.length || !sumLine || !save) return;

  function currentSum() {
    return inputs.reduce(function (total, input) {
      var value = parseFloat(input.value);
      return total + (isNaN(value) ? 0 : value);
    }, 0);
  }

  function sync() {
    var sum = currentSum();
    var ok = Math.abs(sum - 1) < 1e-9;
    sumLine.textContent = ok
      ? "Sum: " + sum.toFixed(2)
      : "Sum: " + sum.toFixed(2) + " — the weights must add up to 1.00";
    sumLine.classList.toggle("bad", !ok);
    save.disabled = !ok;
  }

  inputs.forEach(function (input) {
    input.addEventListener("input", sync);
  });
  sync();
})();

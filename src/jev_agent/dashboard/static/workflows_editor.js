/* Workflow profile editor (issue #368, phase 5, design section 13.2).
 *
 * Canvas and list are two layouts of the SAME form: the mode buttons only
 * swap a CSS class on #wf-stages, so both modes submit identical fields
 * and produce identical profile JSON (canvas drag-out = list toggle-off,
 * canvas parameter edit = list field edit). No validation logic lives
 * here: live feedback posts a draft to /workflows/validate, and the
 * server returns only the live subset (schema parse, unknown names,
 * duplicate stages, empty list). The full catalogue runs at save.
 *
 * Reorder refusal: stage cards are draggable in canvas mode only, and
 * dropping one anywhere refuses with the section 7.1 reason and leaves
 * the order untouched — refusal IS the specified behaviour, not a failed
 * reorder feature. List mode has no dragging at all.
 */
(function () {
  var form = document.getElementById("wf-form");
  var stages = document.getElementById("wf-stages");
  if (!form || !stages) return;

  var refusal = document.getElementById("wf-reorder-refusal");
  var liveBox = document.getElementById("wf-live-errors");

  function setMode(mode) {
    stages.classList.toggle("wf-mode-canvas", mode === "canvas");
    stages.classList.toggle("wf-mode-list", mode === "list");
    var cards = stages.querySelectorAll(".wf-stage");
    cards.forEach(function (card) {
      card.setAttribute("draggable", mode === "canvas" ? "true" : "false");
    });
    document.querySelectorAll(".wf-mode-btn").forEach(function (btn) {
      btn.classList.toggle("is-active", btn.getAttribute("data-wf-mode") === mode);
    });
  }

  document.querySelectorAll(".wf-mode-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setMode(btn.getAttribute("data-wf-mode"));
    });
  });

  var CANONICAL = ["INTAKE", "PLANNING", "VALIDATING", "DESIGNING", "EXECUTING", "INTEGRATING", "SHIPPING"];
  var PRETTY = { INTAKE: "intake", PLANNING: "planning", VALIDATING: "validation", DESIGNING: "design", EXECUTING: "execution", INTEGRATING: "integration", SHIPPING: "shipping" };

  stages.addEventListener("dragstart", function (event) {
    var card = event.target.closest(".wf-stage");
    if (!card) return;
    event.dataTransfer.setData("text/plain", card.getAttribute("data-wf-stage"));
  });
  stages.addEventListener("dragover", function (event) {
    if (event.target.closest(".wf-stage")) event.preventDefault();
  });
  stages.addEventListener("drop", function (event) {
    var card = event.target.closest(".wf-stage");
    if (!card) return;
    event.preventDefault();
    // Refused, by design: stages have data dependencies (a plan must exist
    // before it is validated; code must exist before it is integrated), so
    // the canonical order is fixed and no reorder is applied.
    refusal.textContent =
      "Stages cannot be reordered: the order is fixed — " +
      CANONICAL.map(function (s) { return PRETTY[s]; }).join(", ") +
      " — because each stage needs the previous stage's output. " +
      "Omit a stage where allowed instead, or edit its parameters.";
    refusal.hidden = false;
  });

  // Keep omitted cards visibly dimmed as toggles change, in both modes.
  stages.addEventListener("change", function (event) {
    var box = event.target;
    if (box.type !== "checkbox" || !box.name.startsWith("stage_")) return;
    var card = box.closest(".wf-stage");
    if (card) card.classList.toggle("wf-omitted", !box.checked);
  });

  // Live validation: build the same draft the server builds from the form
  // (construction only — the rules stay server-side) and show the live
  // subset the server returns. Debounced; failures here never block save.
  var timer = null;
  function draft() {
    var profile = {
      id: ((form.querySelector('[name="scope"]') || {}).value || "repo") + ":" +
        ((form.querySelector('[name="slug"]') || {}).value || ""),
      display_name: (form.querySelector('[name="display_name"]') || {}).value || "",
      scope: (form.querySelector('[name="scope"]') || {}).value || "repo",
      stage_list: [],
      gate_policies: {},
      designing: (form.querySelector('[name="designing"]') || {}).value || "when_needed",
      origin: "user",
    };
    CANONICAL.forEach(function (stage) {
      var box = form.querySelector('[name="stage_' + stage + '"]');
      if (box && box.checked) {
        profile.stage_list.push(stage);
        if (stage !== "INTAKE") {
          var gate = form.querySelector('[name="gate_' + stage + '"]');
          profile.gate_policies[stage] = gate ? gate.value : "inherit";
        }
      }
    });
    ["quality_level", "planner_policy", "cache_mode", "cda_grading", "after_approval", "learning_profile", "goal", "priority"].forEach(function (key) {
      var el = form.querySelector('[name="' + key + '"]');
      if (el && el.value) profile[key] = el.value;
    });
    ["max_plan_rounds", "max_fix_rounds", "score_max_retries", "max_task_attempts", "score_threshold"].forEach(function (key) {
      var el = form.querySelector('[name="' + key + '"]');
      if (el && el.value) {
        var n = parseInt(el.value, 10);
        profile[key] = isNaN(n) ? el.value : n;
      }
    });
    if (form.querySelector('[name="score_skip_human"]') && form.querySelector('[name="score_skip_human"]').checked) profile.score_skip_human = true;
    if (form.querySelector('[name="ship_enabled"]') && form.querySelector('[name="ship_enabled"]').checked) profile.ship_enabled = true;
    return profile;
  }
  form.addEventListener("input", function () {
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () {
      var csrf = form.querySelector('[name="csrf_token"]');
      var body = new FormData();
      if (csrf) body.append("csrf_token", csrf.value);
      body.append("profile_json", JSON.stringify(draft()));
      fetch("/workflows/validate", { method: "POST", body: body, credentials: "same-origin" })
        .then(function (resp) { return resp.json(); })
        .then(function (data) {
          if (data.errors && data.errors.length) {
            liveBox.textContent = "Check while editing: " + data.errors.join(" ");
            liveBox.hidden = false;
          } else {
            liveBox.hidden = true;
            liveBox.textContent = "";
          }
        })
        .catch(function () { /* live feedback is convenience; save still validates */ });
    }, 400);
  });

  setMode("canvas");
})();

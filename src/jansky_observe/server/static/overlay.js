// Reference-model overlay toggles (plans/calibrated-overlay.md).
//
// The panel fragment arrives from /captures/{id}/overlay_panel with its checkboxes
// already set to the honest defaults (calibrated on when a Tsys exists). All this does
// is rebuild the <img src> when a box changes — the figure itself is server-rendered,
// so there is no client-side plotting to keep in sync.
(function () {
  "use strict";

  function refresh(panel) {
    const img = panel.querySelector(".overlay-img");
    if (!img) return;
    const id = panel.dataset.captureId;
    const on = (mode) => {
      const box = panel.querySelector(`.overlay-toggle[data-mode="${mode}"]`);
      return Boolean(box && box.checked && !box.disabled);
    };
    const params = new URLSearchParams();
    if (on("calibrated")) params.set("calibrated", "1");
    params.set("residual", on("residual") ? "1" : "0");
    img.src = `/api/captures/${id}/overlay.png?${params.toString()}`;
  }

  // Delegated so panels loaded later by htmx are wired without re-binding.
  document.addEventListener("change", function (event) {
    const box = event.target;
    if (!box.classList || !box.classList.contains("overlay-toggle")) return;
    const panel = box.closest(".overlay-panel");
    if (panel) refresh(panel);
  });
})();

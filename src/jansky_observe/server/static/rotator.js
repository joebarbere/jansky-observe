/* Rotator readback poller (roadmap M9 piece 2).
 *
 * Polls GET /api/rotator and paints the live az/el into #rotator-readback. Best
 * effort and read-only — it never commands motion (slew/stop/park are explicit
 * form POSTs). Silent when the element is absent (rotator kind "none").
 */
(function () {
  "use strict";
  var el = document.getElementById("rotator-readback");
  if (!el) return;
  var endpoint = el.getAttribute("data-endpoint") || "/api/rotator";

  function fmt(deg) {
    return (typeof deg === "number" ? deg.toFixed(1) : "—") + "°";
  }

  function paint(s) {
    if (!s.configured) {
      el.textContent = "no rotator configured";
      return;
    }
    if (s.reachable && s.position) {
      el.textContent =
        "az " + fmt(s.position.az_deg) + " / el " + fmt(s.position.el_deg) +
        "  (" + s.kind + ")";
      el.classList.remove("bad");
    } else {
      el.textContent = "unreachable" + (s.error ? " — " + s.error : "") + " (" + s.kind + ")";
      el.classList.add("bad");
    }
  }

  function poll() {
    fetch(endpoint, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) { if (s) paint(s); })
      .catch(function () { el.textContent = "readback error"; });
  }

  poll();
  setInterval(poll, 4000);
})();

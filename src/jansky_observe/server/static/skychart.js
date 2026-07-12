// Alt/az sky chart (roadmap M7). Polar plot: zenith at centre, horizon at the
// rim; N up, E left (as when you look up). Fetches GET /api/sky_chart and
// redraws every minute; fully offline server-side (astropy). Dependency-free.
(function () {
  "use strict";
  const canvas = document.getElementById("skychart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const meta = document.getElementById("sky-meta");
  let latest = null;

  const THEME = {};
  function refreshTheme() {
    const cs = getComputedStyle(document.documentElement);
    const get = (n, f) => cs.getPropertyValue(n).trim() || f;
    THEME.bg = get("--panel", "#12161f");
    THEME.grid = get("--border", "#232a38");
    THEME.label = get("--muted", "#7a8699");
    THEME.text = get("--text", "#c9d4e3");
    THEME.source = get("--accent", "#4fc3f7");
    THEME.sun = get("--warn", "#e0a83a");
    THEME.moon = get("--muted", "#7a8699");
    THEME.plane = get("--ok", "#37b26c");
    THEME.beam = get("--bad", "#d05555");
  }
  refreshTheme();
  window.addEventListener("themechange", () => { refreshTheme(); draw(); });

  // (az east-of-north deg, el deg) -> canvas x,y. r grows from zenith (el 90)
  // to horizon (el 0); N up, E left.
  function project(az, el, cx, cy, R) {
    const r = (R * (90 - Math.max(-5, el))) / 90;
    const a = (az * Math.PI) / 180;
    return [cx - r * Math.sin(a), cy - r * Math.cos(a)];
  }

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const size = Math.max(240, Math.min(560, canvas.clientWidth || 560));
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    draw();
  }

  function dot(x, y, radius, color) {
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, 2 * Math.PI);
    ctx.fillStyle = color;
    ctx.fill();
  }

  function draw() {
    const w = canvas.width;
    const h = canvas.height;
    const dpr = window.devicePixelRatio || 1;
    ctx.fillStyle = THEME.bg;
    ctx.fillRect(0, 0, w, h);
    const cx = w / 2;
    const cy = h / 2;
    const R = Math.min(w, h) / 2 - 20 * dpr;

    // Horizon + elevation rings (el 0, 30, 60) and az spokes.
    ctx.strokeStyle = THEME.grid;
    ctx.lineWidth = 1 * dpr;
    for (const el of [0, 30, 60]) {
      ctx.beginPath();
      ctx.arc(cx, cy, (R * (90 - el)) / 90, 0, 2 * Math.PI);
      ctx.stroke();
    }
    ctx.font = 11 * dpr + "px monospace";
    ctx.fillStyle = THEME.label;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const cards = [["N", 0], ["E", 90], ["S", 180], ["W", 270]];
    for (const [name, az] of cards) {
      const [x0, y0] = project(az, 0, cx, cy, R);
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(x0, y0);
      ctx.stroke();
      const [lx, ly] = project(az, -6, cx, cy, R);
      ctx.fillText(name, lx, ly);
    }

    if (!latest) return;

    // Galactic plane — dots for the sampled points that are above the horizon.
    for (const p of latest.galactic_plane || []) {
      if (p.el_deg < 0) continue;
      const [x, y] = project(p.az_deg, p.el_deg, cx, cy, R);
      dot(x, y, 1.5 * dpr, THEME.plane);
    }

    // Beam cone at the running session's pointing.
    if (latest.beam) {
      const [bx, by] = project(latest.beam.az_deg, latest.beam.el_deg, cx, cy, R);
      const br = (R * (latest.beam.hpbw_deg / 2)) / 90;
      ctx.strokeStyle = THEME.beam;
      ctx.lineWidth = 1.5 * dpr;
      ctx.beginPath();
      ctx.arc(bx, by, Math.max(3 * dpr, br), 0, 2 * Math.PI);
      ctx.stroke();
    }

    // Sun + Moon.
    function body(pos, color, label) {
      if (!pos || pos.el_deg < -2) return;
      const [x, y] = project(pos.az_deg, pos.el_deg, cx, cy, R);
      dot(x, y, 5 * dpr, color);
      ctx.fillStyle = THEME.text;
      ctx.textAlign = "left";
      ctx.fillText(label, x + 7 * dpr, y);
    }
    body(latest.sun, THEME.sun, "Sun");
    body(latest.moon, THEME.moon, "Moon");

    // Catalog sources above the horizon.
    ctx.fillStyle = THEME.text;
    for (const s of latest.sources || []) {
      if (s.el_deg < 0) continue;
      const [x, y] = project(s.az_deg, s.el_deg, cx, cy, R);
      dot(x, y, 3 * dpr, THEME.source);
      ctx.textAlign = "left";
      ctx.fillStyle = THEME.text;
      ctx.fillText(s.name, x + 6 * dpr, y);
    }
  }

  async function refresh() {
    try {
      const r = await fetch("/api/sky_chart", { cache: "no-store" });
      if (!r.ok) return;
      latest = await r.json();
      if (meta && latest.location) {
        const up = (latest.sources || []).filter((s) => s.el_deg >= 0).length;
        meta.textContent =
          latest.location.name + " · " + up + " source(s) up · updated " +
          new Date(latest.generated_at).toLocaleTimeString();
      }
      draw();
    } catch (e) {
      /* keep the last frame */
    }
  }

  window.addEventListener("resize", resize);
  resize();
  refresh();
  setInterval(refresh, 60000);
})();

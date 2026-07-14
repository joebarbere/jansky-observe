/* jansky-observe total-power strip (cockpit): integrated band power vs time.
 *
 * The classic single-dish "total power" trace — the sum of the PSD across the
 * band, plotted as a scrolling strip. It is what you watch during a Sun-pointing
 * peak-up or a cold-sky-vs-ground check (the total power rises/falls as a source
 * crosses the beam) and the basis of a drift scan. Fed by the same live frames
 * the waterfall uses (waterfall.js calls window.TotalPower.pushFrame) — pure
 * client-side, no backend and no extra Pi load, like the audio sonifier.
 *
 * Value shown is dB *relative to an arbitrary reference* (same reference as the
 * spectrum), computed with a numerically stable log-sum-exp mean over the bins.
 */
"use strict";

window.TotalPower = (function () {
  const canvas = document.getElementById("totalpower");
  const valEl = document.getElementById("stat-tp");
  if (!canvas) return { pushFrame: function () {} }; // no strip on this page → no-op
  const ctx = canvas.getContext("2d");

  const MAX_POINTS = 900; // ~3.75 min of history at 4 fps
  const MARGIN = { left: 52, right: 10, top: 14, bottom: 14 };
  const RANGE_EMA = 0.05; // smoothing for the autoscaled dB range

  const hist = new Float64Array(MAX_POINTS);
  let count = 0; // valid points held (<= MAX_POINTS)
  let head = 0; // next write index (ring)
  let loEma = NaN;
  let hiEma = NaN;
  let streamKey = "";

  const THEME = { bg: "", grid: "", label: "", trace: "" };
  function refreshTheme() {
    const cs = getComputedStyle(document.documentElement);
    const get = (name, fallback) => cs.getPropertyValue(name).trim() || fallback;
    THEME.bg = get("--spec-bg", "#12161f");
    THEME.grid = get("--border", "#232a38");
    THEME.label = get("--muted", "#7a8699");
    THEME.trace = get("--trace-avg", "#ffe082");
  }
  refreshTheme();
  window.addEventListener("themechange", refreshTheme);

  function totalPowerDb(power) {
    // 10*log10(mean_i 10^(power_i/10)) via log-sum-exp so large |dB| can't over/underflow.
    let maxDb = -Infinity;
    for (let i = 0; i < power.length; i++) if (power[i] > maxDb) maxDb = power[i];
    if (!isFinite(maxDb)) return NaN;
    let s = 0;
    for (let i = 0; i < power.length; i++) s += Math.pow(10, (power[i] - maxDb) / 10);
    return maxDb + 10 * Math.log10(s / power.length);
  }

  function niceStep(range, maxTicks) {
    const raw = range / Math.max(1, maxTicks);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 5, 10]) if (raw <= m * mag) return m * mag;
    return 10 * mag;
  }

  function draw() {
    const w = canvas.width;
    const h = canvas.height;
    const dpr = window.devicePixelRatio || 1;
    ctx.fillStyle = THEME.bg;
    ctx.fillRect(0, 0, w, h);

    const m = {
      left: MARGIN.left * dpr,
      right: MARGIN.right * dpr,
      top: MARGIN.top * dpr,
      bottom: MARGIN.bottom * dpr,
    };
    const pw = w - m.left - m.right;
    const ph = h - m.top - m.bottom;

    ctx.fillStyle = THEME.label;
    ctx.font = 10 * dpr + "px monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText("total power (dB rel)", m.left, 2 * dpr);

    if (pw <= 0 || ph <= 0 || count < 2 || !isFinite(loEma) || !isFinite(hiEma)) return;

    const pad = 0.1 * (hiEma - loEma) + 0.5;
    const yLo = loEma - pad;
    const yHi = hiEma + pad;
    const yOf = (db) => m.top + (1 - (db - yLo) / (yHi - yLo)) * ph;

    // y grid + dB labels
    ctx.strokeStyle = THEME.grid;
    ctx.lineWidth = 1;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const step = niceStep(yHi - yLo, 3);
    const digits = step < 1 ? 1 : 0; // avoid duplicate integer labels on a narrow range
    for (let db = Math.ceil(yLo / step) * step; db <= yHi; db += step) {
      const y = yOf(db);
      ctx.beginPath();
      ctx.moveTo(m.left, y);
      ctx.lineTo(w - m.right, y);
      ctx.stroke();
      ctx.fillStyle = THEME.label;
      ctx.fillText(db.toFixed(digits), m.left - 4 * dpr, y);
    }

    // trace: oldest (left) → newest (right)
    ctx.strokeStyle = THEME.trace;
    ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath();
    const start = (head - count + MAX_POINTS) % MAX_POINTS;
    for (let i = 0; i < count; i++) {
      const v = hist[(start + i) % MAX_POINTS];
      const x = m.left + (i / (count - 1)) * pw;
      const y = yOf(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // current-value dot at the leading (right) edge
    const latest = hist[(head - 1 + MAX_POINTS) % MAX_POINTS];
    ctx.fillStyle = THEME.trace;
    ctx.beginPath();
    ctx.arc(w - m.right, yOf(latest), 2.5 * dpr, 0, 2 * Math.PI);
    ctx.fill();

    // Prominent exact value (top-right): the trace shows the trend, this is the number.
    ctx.font = "bold " + 13 * dpr + "px monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "top";
    ctx.fillText(latest.toFixed(1) + " dB", w - m.right, 2 * dpr);
  }

  function pushFrame(header, power) {
    const key = header.center_freq_hz + "|" + header.sample_rate_hz + "|" + header.n_fft;
    if (key !== streamKey) {
      // Stream parameters changed → the old trace is no longer comparable.
      streamKey = key;
      count = 0;
      head = 0;
      loEma = NaN;
      hiEma = NaN;
    }
    const tp = totalPowerDb(power);
    if (!isFinite(tp)) return;
    hist[head] = tp;
    head = (head + 1) % MAX_POINTS;
    if (count < MAX_POINTS) count++;
    if (valEl) valEl.textContent = tp.toFixed(1);

    // Autoscale to the visible window, EMA-smoothed so the axis doesn't jump.
    let lo = Infinity;
    let hi = -Infinity;
    const start = (head - count + MAX_POINTS) % MAX_POINTS;
    for (let i = 0; i < count; i++) {
      const v = hist[(start + i) % MAX_POINTS];
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if (!isFinite(loEma)) {
      loEma = lo;
      hiEma = hi;
    } else {
      loEma += (lo - loEma) * RANGE_EMA;
      hiEma += (hi - hiEma) * RANGE_EMA;
    }
    draw();
  }

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
    const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    draw();
  }
  window.addEventListener("resize", resize);
  window.addEventListener("themechange", draw);
  resize();

  return { pushFrame: pushFrame };
})();

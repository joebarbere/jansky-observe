/* jansky-observe live view: spectrum line plot + scrolling waterfall.
 *
 * Dependency-free vanilla JS. Parses the pack_ws WebSocket binary layout:
 *   uint32 LE header length | UTF-8 JSON header | float32 LE power (dB)
 * Header: { v, seq, timestamp, center_freq_hz, sample_rate_hz, n_fft }.
 * Payload index 0 = lowest frequency. Newest waterfall row is at the TOP.
 */
"use strict";

(function () {
  // ---- DOM ------------------------------------------------------------
  const wsPath = document.body.dataset.wsPath || "/ws/live";
  const connEl = document.getElementById("conn");
  const seqEl = document.getElementById("stat-seq");
  const fpsEl = document.getElementById("stat-fps");
  const centerEl = document.getElementById("stat-center");
  const spanEl = document.getElementById("stat-span");
  const overlayEl = document.getElementById("overlay");
  const specCanvas = document.getElementById("spectrum");
  const wfCanvas = document.getElementById("waterfall");
  const specCtx = specCanvas.getContext("2d");
  const wfCtx = wfCanvas.getContext("2d");

  // ---- constants -------------------------------------------------------
  const HISTORY_ROWS = 320; // waterfall depth (frames)
  const MARGIN = { left: 52, right: 10, top: 8, bottom: 22 };
  const RANGE_EMA = 0.05; // smoothing for the running color/dB range
  const STALE_MS = 3000; // no frame for this long => "waiting" state
  const MAX_BACKOFF_MS = 8000;

  // Viridis-like colormap anchors (t in 0..1 -> RGB).
  const STOPS = [
    [0.0, 68, 1, 84],
    [0.125, 72, 40, 120],
    [0.25, 62, 74, 137],
    [0.375, 49, 104, 142],
    [0.5, 38, 130, 142],
    [0.625, 31, 158, 137],
    [0.75, 53, 183, 121],
    [0.875, 109, 205, 89],
    [1.0, 253, 231, 37],
  ];

  function buildLut() {
    const lut = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      let k = 0;
      while (k < STOPS.length - 2 && t > STOPS[k + 1][0]) k++;
      const [t0, r0, g0, b0] = STOPS[k];
      const [t1, r1, g1, b1] = STOPS[k + 1];
      const f = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
      lut[i * 3] = r0 + (r1 - r0) * f;
      lut[i * 3 + 1] = g0 + (g1 - g0) * f;
      lut[i * 3 + 2] = b0 + (b1 - b0) * f;
    }
    return lut;
  }
  const LUT = buildLut();

  // ---- state -----------------------------------------------------------
  let ws = null;
  let backoffMs = 500;
  let latest = null; // { header, power }
  let rangeLo = NaN; // running percentile color/dB range
  let rangeHi = NaN;
  let lastFrameAt = 0;
  let frameCount = 0;
  let fps = 0;
  // Offscreen waterfall history: width = n_fft, height = HISTORY_ROWS.
  const off = { canvas: null, ctx: null, nfft: 0, rows: 0 };

  // ---- binary parsing ----------------------------------------------------
  function parseFrame(buf) {
    const view = new DataView(buf);
    const hlen = view.getUint32(0, true);
    const headerBytes = new Uint8Array(buf, 4, hlen);
    const header = JSON.parse(new TextDecoder().decode(headerBytes));
    // slice() re-aligns the payload so Float32Array's offset rule holds.
    const power = new Float32Array(buf.slice(4 + hlen));
    return { header: header, power: power };
  }

  // ---- running dB range (autoscaled color + y-axis) ----------------------
  function percentileRange(power) {
    const stride = Math.max(1, Math.floor(power.length / 2048));
    const n = Math.ceil(power.length / stride);
    const sample = new Float32Array(n);
    for (let i = 0, j = 0; j < n; i += stride, j++) sample[j] = power[i];
    sample.sort(); // typed arrays sort numerically
    const lo = sample[Math.floor(0.05 * (n - 1))];
    const hi = sample[Math.floor(0.995 * (n - 1))];
    return [lo, hi];
  }

  function updateRange(power) {
    const [lo, hi] = percentileRange(power);
    if (!isFinite(rangeLo)) {
      rangeLo = lo;
      rangeHi = hi;
    } else {
      rangeLo += (lo - rangeLo) * RANGE_EMA;
      rangeHi += (hi - rangeHi) * RANGE_EMA;
    }
    if (rangeHi - rangeLo < 1e-3) rangeHi = rangeLo + 1e-3;
  }

  // ---- waterfall ---------------------------------------------------------
  function ensureHistory(nfft) {
    if (off.canvas && off.nfft === nfft) return;
    off.canvas = document.createElement("canvas");
    off.canvas.width = nfft;
    off.canvas.height = HISTORY_ROWS;
    off.ctx = off.canvas.getContext("2d");
    off.nfft = nfft;
    off.rows = 0;
  }

  function pushRow(power) {
    ensureHistory(power.length);
    // Scroll history down one row (newest row lives at y = 0).
    off.ctx.drawImage(
      off.canvas,
      0, 0, off.nfft, HISTORY_ROWS - 1,
      0, 1, off.nfft, HISTORY_ROWS - 1
    );
    const row = off.ctx.createImageData(off.nfft, 1);
    const px = row.data;
    const scale = 255 / (rangeHi - rangeLo);
    for (let i = 0; i < off.nfft; i++) {
      let t = (power[i] - rangeLo) * scale;
      t = t < 0 ? 0 : t > 255 ? 255 : t | 0;
      px[i * 4] = LUT[t * 3];
      px[i * 4 + 1] = LUT[t * 3 + 1];
      px[i * 4 + 2] = LUT[t * 3 + 2];
      px[i * 4 + 3] = 255;
    }
    off.ctx.putImageData(row, 0, 0);
    if (off.rows < HISTORY_ROWS) off.rows++;
  }

  function drawWaterfall() {
    const w = wfCanvas.width;
    const h = wfCanvas.height;
    wfCtx.fillStyle = "#0b0e14";
    wfCtx.fillRect(0, 0, w, h);
    if (!off.canvas || off.rows === 0) return;
    wfCtx.imageSmoothingEnabled = false;
    wfCtx.drawImage(off.canvas, 0, 0, off.nfft, HISTORY_ROWS, 0, 0, w, h);
  }

  // ---- spectrum ------------------------------------------------------------
  function niceStep(range, maxTicks) {
    const raw = range / Math.max(1, maxTicks);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 5, 10]) {
      if (raw <= m * mag) return m * mag;
    }
    return 10 * mag;
  }

  function drawSpectrum() {
    const w = specCanvas.width;
    const h = specCanvas.height;
    const dpr = window.devicePixelRatio || 1;
    const m = {
      left: MARGIN.left * dpr,
      right: MARGIN.right * dpr,
      top: MARGIN.top * dpr,
      bottom: MARGIN.bottom * dpr,
    };
    specCtx.fillStyle = "#12161f";
    specCtx.fillRect(0, 0, w, h);
    if (!latest) return;

    const pw = w - m.left - m.right;
    const ph = h - m.top - m.bottom;
    if (pw <= 0 || ph <= 0) return;

    const hd = latest.header;
    const power = latest.power;
    const fLo = (hd.center_freq_hz - hd.sample_rate_hz / 2) / 1e6;
    const fHi = (hd.center_freq_hz + hd.sample_rate_hz / 2) / 1e6;
    const pad = 0.15 * (rangeHi - rangeLo) + 1;
    const yLo = rangeLo - pad;
    const yHi = rangeHi + pad;

    const xOf = (mhz) => m.left + ((mhz - fLo) / (fHi - fLo)) * pw;
    const yOf = (db) => m.top + (1 - (db - yLo) / (yHi - yLo)) * ph;

    // Grid + labels.
    specCtx.strokeStyle = "#232a38";
    specCtx.fillStyle = "#7a8699";
    specCtx.lineWidth = 1;
    specCtx.font = 10 * dpr + "px monospace";

    const dbStep = niceStep(yHi - yLo, 5);
    specCtx.textAlign = "right";
    specCtx.textBaseline = "middle";
    for (let db = Math.ceil(yLo / dbStep) * dbStep; db <= yHi; db += dbStep) {
      const y = yOf(db);
      specCtx.beginPath();
      specCtx.moveTo(m.left, y);
      specCtx.lineTo(w - m.right, y);
      specCtx.stroke();
      specCtx.fillText(db.toFixed(0) + " dB", m.left - 4 * dpr, y);
    }

    const fStep = niceStep(fHi - fLo, 8);
    specCtx.textAlign = "center";
    specCtx.textBaseline = "top";
    for (let f = Math.ceil(fLo / fStep) * fStep; f <= fHi; f += fStep) {
      const x = xOf(f);
      specCtx.beginPath();
      specCtx.moveTo(x, m.top);
      specCtx.lineTo(x, h - m.bottom);
      specCtx.stroke();
      const digits = fStep < 0.1 ? 2 : fStep < 1 ? 1 : 0;
      specCtx.fillText(f.toFixed(digits), x, h - m.bottom + 4 * dpr);
    }

    // Spectrum trace.
    specCtx.strokeStyle = "#4fc3f7";
    specCtx.lineWidth = 1.25 * dpr;
    specCtx.beginPath();
    const n = power.length;
    for (let i = 0; i < n; i++) {
      const x = m.left + (i / (n - 1)) * pw;
      const y = yOf(power[i]);
      if (i === 0) specCtx.moveTo(x, y);
      else specCtx.lineTo(x, y);
    }
    specCtx.stroke();
  }

  // ---- status bar / overlay -------------------------------------------------
  function setStatus(state, text) {
    connEl.className = "badge " + state;
    connEl.textContent = text;
  }

  function showOverlay(show) {
    overlayEl.classList.toggle("hidden", !show);
  }

  function updateStats(header) {
    seqEl.textContent = String(header.seq);
    centerEl.textContent = (header.center_freq_hz / 1e6).toFixed(3);
    spanEl.textContent = (header.sample_rate_hz / 1e6).toFixed(3);
  }

  setInterval(function () {
    fps = frameCount;
    frameCount = 0;
    fpsEl.textContent = latest ? fps.toFixed(0) : "–";
    if (ws && ws.readyState === WebSocket.OPEN) {
      const stale = !latest || Date.now() - lastFrameAt > STALE_MS;
      setStatus(stale ? "waiting" : "live", stale ? "no frames" : "live");
      showOverlay(stale);
    }
  }, 1000);

  // ---- frame handling ----------------------------------------------------
  function onFrame(buf) {
    let frame;
    try {
      frame = parseFrame(buf);
    } catch (err) {
      console.warn("bad frame:", err);
      return;
    }
    latest = frame;
    lastFrameAt = Date.now();
    frameCount++;
    updateRange(frame.power);
    pushRow(frame.power);
    updateStats(frame.header);
    setStatus("live", "live");
    showOverlay(false);
    drawSpectrum();
    drawWaterfall();
  }

  // ---- resize --------------------------------------------------------------
  function resizeCanvases() {
    const dpr = window.devicePixelRatio || 1;
    for (const c of [specCanvas, wfCanvas]) {
      const w = Math.max(1, Math.round(c.clientWidth * dpr));
      const h = Math.max(1, Math.round(c.clientHeight * dpr));
      if (c.width !== w || c.height !== h) {
        c.width = w;
        c.height = h;
      }
    }
    drawSpectrum();
    drawWaterfall();
  }
  window.addEventListener("resize", resizeCanvases);
  resizeCanvases();

  // ---- websocket with auto-reconnect ----------------------------------------
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(proto + "://" + location.host + wsPath);
    ws.binaryType = "arraybuffer";
    setStatus("connecting", "connecting…");

    ws.onopen = function () {
      backoffMs = 500;
      overlayEl.textContent = "waiting for capture daemon…";
      setStatus("waiting", "no frames");
      showOverlay(!latest);
    };
    ws.onmessage = function (ev) {
      if (ev.data instanceof ArrayBuffer) onFrame(ev.data);
    };
    ws.onclose = function () {
      setStatus("disconnected", "disconnected");
      showOverlay(true);
      overlayEl.textContent = "reconnecting…";
      setTimeout(connect, backoffMs);
      backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
    };
    ws.onerror = function () {
      ws.close();
    };
  }
  connect();
})();

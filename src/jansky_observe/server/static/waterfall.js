/* jansky-observe live view: spectrum line plot + scrolling waterfall +
 * accumulating average + capture controls.
 *
 * Dependency-free vanilla JS. Parses the pack_ws WebSocket binary layout:
 *   uint32 LE header length | UTF-8 JSON header | float32 LE power (dB)
 * Header: { v, seq, timestamp, center_freq_hz, sample_rate_hz, n_fft }.
 * Payload index 0 = lowest frequency. Newest waterfall row is at the TOP.
 *
 * The capture panel polls /api/capture/status (~2 s, only while the page is
 * visible) and drives /api/capture/start|stop; 409/503 surface as a banner.
 * The HI badge chip polls /api/live/hi_badge (~3 s, visible page only): the
 * server-side accumulating average's running verdict + SNR (plan §5.2).
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
  const avgCountEl = document.getElementById("avg-count");
  const resetAvgBtn = document.getElementById("btn-reset-avg");

  // ---- theme colors (roadmap M6) --------------------------------------
  // The canvas backdrops, grid, labels, and traces come from CSS custom
  // properties so the plots track the page theme (light/dark). Cached, and
  // refreshed when ui.js dispatches "themechange".
  const THEME = { wfBg: "", specBg: "", grid: "", label: "", live: "", avg: "" };
  function refreshTheme() {
    const cs = getComputedStyle(document.documentElement);
    const get = (name, fallback) => (cs.getPropertyValue(name).trim() || fallback);
    THEME.wfBg = get("--wf-bg", "#0b0e14");
    THEME.specBg = get("--spec-bg", "#12161f");
    THEME.grid = get("--border", "#232a38");
    THEME.label = get("--muted", "#7a8699");
    THEME.live = get("--trace-live", "rgba(79, 195, 247, 0.35)");
    THEME.avg = get("--trace-avg", "#ffe082");
  }
  refreshTheme();
  window.addEventListener("themechange", refreshTheme);
  const startNpzBtn = document.getElementById("btn-start-npz");
  const startSigmfBtn = document.getElementById("btn-start-sigmf");
  const stopBtn = document.getElementById("btn-stop");
  const rfiBtn = document.getElementById("btn-rfi-sweep");
  const rfiResultEl = document.getElementById("rfi-result");
  const projNpzEl = document.getElementById("proj-npz");
  const projSigmfEl = document.getElementById("proj-sigmf");
  const capStateEl = document.getElementById("capture-state");
  const overrunEl = document.getElementById("overrun-badge");
  const capLiveEl = document.getElementById("capture-live");
  const capFileEl = document.getElementById("cap-file");
  const capElapsedEl = document.getElementById("cap-elapsed");
  const capMbEl = document.getElementById("cap-mb");
  const capRateEl = document.getElementById("cap-rate");
  const capDiskEl = document.getElementById("cap-disk");
  const capErrorEl = document.getElementById("capture-error");
  const hiBadgeEl = document.getElementById("hi-badge");
  const hiResetEl = document.getElementById("hi-badge-reset");

  // ---- constants -------------------------------------------------------
  const MARGIN = { left: 52, right: 10, top: 8, bottom: 22 };
  const RANGE_EMA = 0.05; // smoothing for the running color/dB range
  const STALE_MS = 3000; // no frame for this long => "waiting" state
  const MAX_BACKOFF_MS = 8000;
  const STATUS_POLL_MS = 2000; // capture-status poll period (visible page only)
  const HI_BADGE_POLL_MS = 3000; // HI badge poll period (visible page only)
  const HOT_GB_PER_HOUR = 10; // amber-highlight threshold for projected rates
  const ERROR_HIDE_MS = 8000; // action-error banner auto-hide

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
  // Offscreen waterfall history rendered at the display's exact VERTICAL resolution:
  // one data row = one device pixel, so a row keeps its exact brightness as it scrolls
  // down — a true frozen-in-time record. The old buffer was a fixed 320 rows scaled to
  // the canvas height with nearest-neighbour sampling; that ratio is non-integer, so a
  // row was drawn 1px tall on some frames and 2px on others as it scrolled — the
  // brighten/dim "flicker" you see following a single point. Width stays n_fft (the
  // horizontal n_fft->width scale is fixed frame-to-frame, so it never flickers). There
  // is no sub-frame glide: the waterfall advances one pixel when a new frame arrives.
  const off = { canvas: null, ctx: null, nfft: 0, height: 0, rows: 0 };
  // Accumulating average: Float64 sum over frames, keyed by stream params.
  const avg = { sum: null, count: 0, key: "" };

  // ---- accumulating average ---------------------------------------------
  function resetAverage(n, key) {
    avg.sum = n > 0 ? new Float64Array(n) : null;
    avg.count = 0;
    avg.key = key || "";
    avgCountEl.textContent = "0";
  }

  function accumulate(header, power) {
    // Any change in stream parameters invalidates the running mean.
    const key = header.center_freq_hz + "|" + header.sample_rate_hz + "|" + header.n_fft;
    if (key !== avg.key || !avg.sum || avg.sum.length !== power.length) {
      resetAverage(power.length, key);
    }
    for (let i = 0; i < power.length; i++) avg.sum[i] += power[i];
    avg.count++;
    avgCountEl.textContent = String(avg.count);
  }

  resetAvgBtn.addEventListener("click", function () {
    resetAverage(avg.sum ? avg.sum.length : 0, avg.key);
    drawSpectrum();
  });

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
  function ensureHistory(nfft, height) {
    height = Math.max(1, height);
    if (off.canvas && off.nfft === nfft && off.height === height) return;
    // n_fft or canvas height changed → (re)build. History resets; both are rare
    // (a resize or a stream-parameter change), and a reset is cleaner than a
    // one-off rescale that would briefly reintroduce the very artifact we removed.
    off.canvas = document.createElement("canvas");
    off.canvas.width = nfft;
    off.canvas.height = height;
    off.ctx = off.canvas.getContext("2d");
    off.nfft = nfft;
    off.height = height;
    off.rows = 0;
  }

  function pushRow(power) {
    ensureHistory(power.length, wfCanvas.height);
    const H = off.height;
    // Scroll history down one pixel (newest row lives at y = 0).
    off.ctx.drawImage(off.canvas, 0, 0, off.nfft, H - 1, 0, 1, off.nfft, H - 1);
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
    if (off.rows < H) off.rows++;
  }

  function drawWaterfall() {
    const w = wfCanvas.width;
    const h = wfCanvas.height;
    wfCtx.fillStyle = THEME.wfBg;
    wfCtx.fillRect(0, 0, w, h);
    if (!off.canvas || off.rows === 0) return;
    wfCtx.imageSmoothingEnabled = false;
    // Vertical is 1:1 (off.height === h in steady state), so a row never resamples
    // as it scrolls; only the fixed horizontal n_fft->width scale applies.
    wfCtx.drawImage(off.canvas, 0, 0, off.nfft, off.height, 0, 0, w, h);
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
    specCtx.fillStyle = THEME.specBg;
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
    specCtx.strokeStyle = THEME.grid;
    specCtx.fillStyle = THEME.label;
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

    function trace(values, scale, color, width) {
      specCtx.strokeStyle = color;
      specCtx.lineWidth = width * dpr;
      specCtx.beginPath();
      const n = values.length;
      for (let i = 0; i < n; i++) {
        const x = m.left + (i / (n - 1)) * pw;
        const y = yOf(values[i] * scale);
        if (i === 0) specCtx.moveTo(x, y);
        else specCtx.lineTo(x, y);
      }
      specCtx.stroke();
    }

    // Instantaneous trace (dimmed) behind the brighter accumulating average.
    trace(power, 1, THEME.live, 1.25);
    if (avg.sum && avg.count > 0 && avg.sum.length === power.length) {
      trace(avg.sum, 1 / avg.count, THEME.avg, 1.5);
    }
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
    accumulate(frame.header, frame.power);
    pushRow(frame.power);
    updateStats(frame.header);
    setStatus("live", "live");
    showOverlay(false);
    drawSpectrum();
    drawWaterfall();
    if (window.SpectrumAudio) window.SpectrumAudio.pushFrame(frame.header, frame.power);
    if (window.TotalPower) window.TotalPower.pushFrame(frame.header, frame.power);
  }

  // ---- audio sonification controls (roadmap M6) ----------------------------
  // The synthesis lives in audio.js; here we just wire the live-view controls.
  // Starting audio needs a user gesture, so the toggle click is what boots the
  // AudioContext.
  (function wireAudio() {
    const group = document.getElementById("audio-group");
    const toggle = document.getElementById("audio-toggle");
    const modeSel = document.getElementById("audio-mode");
    if (!group || !toggle || !modeSel || !window.SpectrumAudio || !window.SpectrumAudio.available) {
      return; // no WebAudio (or controls absent) → leave the group hidden
    }
    group.hidden = false;
    function setLabel(on) {
      toggle.textContent = on ? "🔊 Audio" : "🔇 Audio";
      toggle.setAttribute("aria-pressed", on ? "true" : "false");
      modeSel.disabled = !on;
    }
    toggle.addEventListener("click", function () {
      if (window.SpectrumAudio.isRunning()) {
        window.SpectrumAudio.stop();
        setLabel(false);
      } else {
        window.SpectrumAudio.setMode(modeSel.value);
        setLabel(window.SpectrumAudio.start());
      }
    });
    modeSel.addEventListener("change", function () {
      window.SpectrumAudio.setMode(modeSel.value);
    });
  })();

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

  // ---- capture panel -------------------------------------------------------
  let errorTimer = null;
  let sweeping = false; // an RFI sweep round trip is in flight

  function showCaptureError(msg) {
    capErrorEl.textContent = msg;
    capErrorEl.classList.remove("hidden");
    if (errorTimer) clearTimeout(errorTimer);
    errorTimer = setTimeout(function () {
      capErrorEl.classList.add("hidden");
    }, ERROR_HIDE_MS);
  }

  function setCaptureState(cls, text) {
    capStateEl.className = "badge " + cls;
    capStateEl.textContent = text;
  }

  function fmtProjected(gbPerHour) {
    if (gbPerHour == null) return "–";
    if (gbPerHour < 0.1) return "~" + (gbPerHour * 1000).toFixed(1) + " MB/h";
    return "~" + gbPerHour.toFixed(1) + " GB/h";
  }

  function fmtElapsed(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const mm = String(m).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    return h > 0 ? h + ":" + mm + ":" + ss : m + ":" + ss;
  }

  function renderProjection(el, gbPerHour) {
    el.textContent = fmtProjected(gbPerHour);
    // The "before you fill the SD card" warning: amber when the projected
    // rate is punishing (SigMF at 3 MSPS is tens of GB/h).
    el.classList.toggle("hot", gbPerHour != null && gbPerHour > HOT_GB_PER_HOUR);
  }

  function renderCaptureStatus(st) {
    const proj = st.projected_gb_per_hour || {};
    renderProjection(projNpzEl, proj.npz);
    renderProjection(projSigmfEl, proj.sigmf);
    overrunEl.classList.toggle("hidden", !st.overrun);

    startNpzBtn.disabled = st.capturing;
    startSigmfBtn.disabled = st.capturing;
    stopBtn.disabled = !st.capturing;
    rfiBtn.disabled = sweeping || st.capturing; // daemon refuses a sweep mid-capture
    capLiveEl.classList.toggle("hidden", !st.capturing);

    if (!st.capturing) {
      setCaptureState("idle", "idle");
      return;
    }
    setCaptureState("recording", "recording " + (st.format || ""));
    capFileEl.textContent = st.path ? String(st.path).split("/").pop() : "–";
    capElapsedEl.textContent = fmtElapsed(st.elapsed_s || 0);
    capMbEl.textContent = ((st.bytes_written || 0) / 1e6).toFixed(1);
    capRateEl.textContent = ((st.rate_bytes_per_s || 0) / 1e6).toFixed(2);
    const free = st.disk_free_gb != null ? st.disk_free_gb.toFixed(1) + " GB free" : "–";
    const toFull =
      st.hours_to_full != null ? " · full in " + st.hours_to_full.toFixed(1) + " h" : "";
    capDiskEl.textContent = free + toFull;
  }

  function setDaemonUnreachable(detail) {
    setCaptureState("unreachable", "daemon unreachable");
    startNpzBtn.disabled = true;
    startSigmfBtn.disabled = true;
    stopBtn.disabled = true;
    rfiBtn.disabled = true;
    capLiveEl.classList.add("hidden");
    overrunEl.classList.add("hidden");
    if (detail) showCaptureError(detail);
  }

  async function detailOf(resp) {
    try {
      const body = await resp.json();
      return body.detail || resp.status + " " + resp.statusText;
    } catch (err) {
      return resp.status + " " + resp.statusText;
    }
  }

  async function pollCaptureStatus() {
    if (document.visibilityState !== "visible") return;
    let resp;
    try {
      resp = await fetch("/api/capture/status");
    } catch (err) {
      setDaemonUnreachable(null); // server itself unreachable; WS badge covers it
      return;
    }
    if (resp.ok) {
      renderCaptureStatus(await resp.json());
    } else if (resp.status === 503) {
      setDaemonUnreachable(null); // steady state, not a toast-worthy event
    } else {
      showCaptureError(await detailOf(resp));
    }
  }

  async function captureAction(path, body) {
    let resp;
    try {
      resp = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : null,
      });
    } catch (err) {
      showCaptureError("request failed: " + err);
      return;
    }
    if (!resp.ok) {
      showCaptureError(await detailOf(resp));
      pollCaptureStatus();
      return;
    }
    renderCaptureStatus(await resp.json());
  }

  startNpzBtn.addEventListener("click", function () {
    captureAction("/api/capture/start", { format: "npz" });
  });
  startSigmfBtn.addEventListener("click", function () {
    captureAction("/api/capture/start", { format: "sigmf" });
  });
  stopBtn.addEventListener("click", function () {
    captureAction("/api/capture/stop", null);
  });

  // ---- RFI sweep (plan §4.2/§5.2: HackRF pre-session survey) ----------------
  function renderRfiResult(r) {
    const range = r.freq_range_hz || [];
    const top = (r.loudest || [])[0];
    let text = (r.num_sweeps || "?") + " sweeps";
    if (range.length === 2) {
      text += " " + (range[0] / 1e6).toFixed(0) + "–" + (range[1] / 1e6).toFixed(0) + " MHz";
    }
    if (top) {
      text +=
        " — loudest: " +
        (top.freq_hz / 1e6).toFixed(1) +
        " MHz " +
        top.power_db.toFixed(1) +
        " dB";
    }
    if (r.capture_id != null) text += " (capture #" + r.capture_id + ")";
    rfiResultEl.textContent = text;
    rfiResultEl.classList.remove("hidden");
  }

  rfiBtn.addEventListener("click", async function () {
    sweeping = true;
    rfiBtn.disabled = true;
    rfiResultEl.textContent = "sweeping… (live stream pauses for the sweep)";
    rfiResultEl.classList.remove("hidden");
    try {
      const resp = await fetch("/api/rfi_sweep", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!resp.ok) {
        rfiResultEl.classList.add("hidden");
        showCaptureError(await detailOf(resp));
      } else {
        renderRfiResult(await resp.json());
      }
    } catch (err) {
      rfiResultEl.classList.add("hidden");
      showCaptureError("RFI sweep failed: " + err);
    } finally {
      sweeping = false;
      rfiBtn.disabled = false;
      pollCaptureStatus();
    }
  });
  // ---- HI badge (plan §5.2: "am I seeing it?") ------------------------------
  function renderHiBadge(b) {
    let cls = "hi-accum";
    let text = "HI –";
    let tip = "";
    if (b.status === "accumulating") {
      text = "accumulating (" + (b.n_frames || 0) + ")";
    } else if (b.status === "ok") {
      const snr = b.snr != null ? b.snr.toFixed(1) : "–";
      if (b.verdict === "detected") {
        cls = "hi-detected";
        text = "HI DETECTED — SNR " + snr;
      } else if (b.verdict === "uncertain") {
        cls = "hi-uncertain";
        text = "uncertain — SNR " + snr;
      } else {
        cls = "hi-none";
        text = "not detected — SNR " + snr;
      }
      tip = "window: " + b.window_source;
      if (b.peak_vlsr_kms != null) {
        tip += " · peak v_LSR " + b.peak_vlsr_kms.toFixed(1) + " km/s";
      }
    } else {
      cls = "hi-none";
      text = "HI badge unavailable";
      tip = b.detail || "";
    }
    hiBadgeEl.className = "badge " + cls;
    hiBadgeEl.textContent = text;
    hiBadgeEl.title = tip;
  }

  async function pollHiBadge() {
    if (document.visibilityState !== "visible") return;
    let resp;
    try {
      resp = await fetch("/api/live/hi_badge");
    } catch (err) {
      return; // server unreachable; the connection badge covers it
    }
    if (resp.ok) renderHiBadge(await resp.json());
  }

  hiResetEl.addEventListener("click", async function (ev) {
    ev.preventDefault();
    try {
      const resp = await fetch("/api/live/hi_badge/reset", { method: "POST" });
      if (resp.ok) renderHiBadge(await resp.json());
    } catch (err) {
      /* next poll recovers */
    }
    pollHiBadge();
  });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      pollCaptureStatus();
      pollHiBadge();
    }
  });
  setInterval(pollCaptureStatus, STATUS_POLL_MS);
  setInterval(pollHiBadge, HI_BADGE_POLL_MS);
  pollCaptureStatus();
  pollHiBadge();
})();

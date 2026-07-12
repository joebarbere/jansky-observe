// Spectrum sonification (roadmap M6): turn the live PSD the WebSocket already
// delivers into sound, entirely client-side — no new backend, no extra Pi load.
// This is an aesthetic surface, not a science one: the waterfall stays the
// quantitative view. window.SpectrumAudio is fed one frame per PSD (~4 fps) by
// waterfall.js; the live view wires the on/off + mode controls.
//
// Four modes:
//   receiver — pink noise through a filter bank whose gains track the band shape
//   doppler  — one oscillator; the peak's position → pitch, its SNR → loudness
//   geiger   — click rate follows the band SNR (the classic counter)
//   drone    — a slow harmonic pad weighted by low/mid/high band power
//
// All synthesis constants are client-side aesthetics; nothing here feeds a
// verdict. Guarded on AudioContext so it is inert where WebAudio is absent.
(function () {
  "use strict";
  var AC = window.AudioContext || window.webkitAudioContext;

  var ctx = null;
  var master = null;
  var running = false;
  var mode = "receiver";
  var active = null; // the current mode's node graph
  var RAMP = 0.08; // seconds — smooth param changes (avoid zipper noise)

  // ---- shared helpers ----------------------------------------------------
  function pinkNoiseBuffer() {
    // ~2 s of looping pink-ish noise (Voss-McCartney-lite).
    var len = ctx.sampleRate * 2;
    var buf = ctx.createBuffer(1, len, ctx.sampleRate);
    var d = buf.getChannelData(0);
    var b0 = 0, b1 = 0, b2 = 0;
    for (var i = 0; i < len; i++) {
      var w = Math.random() * 2 - 1;
      b0 = 0.99765 * b0 + w * 0.099046;
      b1 = 0.963 * b1 + w * 0.2965164;
      b2 = 0.57 * b2 + w * 1.0526913;
      d[i] = (b0 + b1 + b2 + w * 0.1848) * 0.15;
    }
    return buf;
  }

  // Band-average a dB PSD into n groups, normalized to 0..1 across the frame.
  function bands(power, n) {
    var out = new Float32Array(n);
    var lo = Infinity, hi = -Infinity;
    var per = power.length / n;
    for (var k = 0; k < n; k++) {
      var s = 0, c = 0;
      var start = Math.floor(k * per), end = Math.floor((k + 1) * per);
      for (var i = start; i < end; i++) { s += power[i]; c++; }
      var v = c ? s / c : -120;
      out[k] = v;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    var span = hi - lo || 1;
    for (var j = 0; j < n; j++) out[j] = (out[j] - lo) / span;
    return out;
  }

  // Rough band SNR in dB: peak minus a robust noise floor (median-ish).
  function snrDb(power) {
    var stride = Math.max(1, Math.floor(power.length / 512));
    var sample = [];
    var peak = -Infinity;
    for (var i = 0; i < power.length; i += stride) {
      sample.push(power[i]);
      if (power[i] > peak) peak = power[i];
    }
    sample.sort(function (a, b) { return a - b; });
    var floor = sample[Math.floor(sample.length / 2)];
    return peak - floor;
  }

  function peakFraction(power) {
    var peak = -Infinity, idx = 0;
    for (var i = 0; i < power.length; i++) {
      if (power[i] > peak) { peak = power[i]; idx = i; }
    }
    return power.length > 1 ? idx / (power.length - 1) : 0.5;
  }

  function clamp01(x) { return x < 0 ? 0 : x > 1 ? 1 : x; }

  // ---- receiver: pink noise through a band-gain filter bank --------------
  function makeReceiver() {
    var N = 16;
    var src = ctx.createBufferSource();
    src.buffer = pinkNoiseBuffer();
    src.loop = true;
    var gains = [];
    // Log-spaced band centers from 120 Hz to ~5 kHz.
    for (var k = 0; k < N; k++) {
      var f = 120 * Math.pow(5000 / 120, k / (N - 1));
      var bp = ctx.createBiquadFilter();
      bp.type = "bandpass";
      bp.frequency.value = f;
      bp.Q.value = 6;
      var g = ctx.createGain();
      g.gain.value = 0;
      src.connect(bp); bp.connect(g); g.connect(master);
      gains.push(g);
    }
    src.start();
    return {
      update: function (header, power) {
        var b = bands(power, N);
        var t = ctx.currentTime;
        for (var k = 0; k < N; k++) {
          gains[k].gain.setTargetAtTime(0.9 * b[k] * b[k], t, RAMP);
        }
      },
      stop: function () { try { src.stop(); } catch (e) { /* already stopped */ } },
    };
  }

  // ---- doppler: peak position → pitch, SNR → loudness --------------------
  function makeDoppler() {
    var osc = ctx.createOscillator();
    osc.type = "sine";
    var g = ctx.createGain();
    g.gain.value = 0;
    osc.connect(g); g.connect(master);
    osc.start();
    return {
      update: function (header, power) {
        var frac = peakFraction(power); // 0 (low edge) .. 1 (high edge)
        var hz = 180 * Math.pow(1000 / 180, frac); // 180 Hz .. 1 kHz
        var loud = clamp01((snrDb(power) - 2) / 12);
        var t = ctx.currentTime;
        osc.frequency.setTargetAtTime(hz, t, RAMP);
        g.gain.setTargetAtTime(0.6 * loud, t, RAMP);
      },
      stop: function () { try { osc.stop(); } catch (e) { /* already stopped */ } },
    };
  }

  // ---- geiger: click rate follows SNR ------------------------------------
  function makeGeiger() {
    var rate = 0; // clicks/sec, driven by SNR
    var timer = null;
    function click() {
      var t = ctx.currentTime;
      var o = ctx.createOscillator();
      var g = ctx.createGain();
      o.type = "square";
      o.frequency.value = 900;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.5, t + 0.001);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.05);
      o.connect(g); g.connect(master);
      o.start(t); o.stop(t + 0.06);
    }
    function schedule() {
      if (rate > 0) click();
      // Next click: exponential inter-arrival for a Geiger feel; floor rate.
      var r = Math.max(0.2, rate);
      timer = setTimeout(schedule, (-Math.log(Math.random()) / r) * 1000);
    }
    schedule();
    return {
      update: function (header, power) {
        rate = clamp01((snrDb(power) - 2) / 12) * 25; // up to ~25 clicks/s
      },
      stop: function () { if (timer) clearTimeout(timer); },
    };
  }

  // ---- drone: harmonic pad weighted by low/mid/high band power -----------
  function makeDrone() {
    var base = 110; // A2
    var partials = [1, 2, 3, 4, 5];
    var oscs = [], gs = [];
    for (var k = 0; k < partials.length; k++) {
      var o = ctx.createOscillator();
      o.type = "triangle";
      o.frequency.value = base * partials[k];
      var g = ctx.createGain();
      g.gain.value = 0;
      o.connect(g); g.connect(master);
      o.start();
      oscs.push(o); gs.push(g);
    }
    return {
      update: function (header, power) {
        var b = bands(power, partials.length);
        var t = ctx.currentTime;
        for (var k = 0; k < gs.length; k++) {
          gs[k].gain.setTargetAtTime(0.18 * b[k], t, 0.4); // slow pad
        }
      },
      stop: function () {
        for (var k = 0; k < oscs.length; k++) {
          try { oscs[k].stop(); } catch (e) { /* already stopped */ }
        }
      },
    };
  }

  var BUILDERS = {
    receiver: makeReceiver,
    doppler: makeDoppler,
    geiger: makeGeiger,
    drone: makeDrone,
  };

  function startMode() {
    if (active) active.stop();
    active = (BUILDERS[mode] || makeReceiver)();
  }
  function stopMode() {
    if (active) { active.stop(); active = null; }
  }

  function ensureCtx() {
    if (!AC) return false;
    if (!ctx) {
      ctx = new AC();
      master = ctx.createGain();
      master.gain.value = 0.25;
      master.connect(ctx.destination);
    }
    if (ctx.state === "suspended" && ctx.resume) ctx.resume();
    return true;
  }

  window.SpectrumAudio = {
    available: !!AC,
    isRunning: function () { return running; },
    modes: Object.keys(BUILDERS),
    setMode: function (m) {
      if (!BUILDERS[m] || m === mode) return;
      mode = m;
      if (running) startMode();
    },
    start: function () {
      if (!ensureCtx()) return false;
      running = true;
      startMode();
      return true;
    },
    stop: function () {
      running = false;
      stopMode();
    },
    pushFrame: function (header, power) {
      if (running && active && active.update) active.update(header, power);
    },
  };
})();

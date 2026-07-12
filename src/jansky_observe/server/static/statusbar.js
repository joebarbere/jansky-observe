// Station-cockpit status bar (roadmap M6). Polls GET /api/status_bar every 10 s
// and ticks the clocks every second in between — the UTC/local clocks come from
// the browser; LST is seeded from the server (astropy) and advanced at the
// sidereal rate between polls. No framework: guarded on the bar's presence so it
// is a no-op on pages without it.
(function () {
  "use strict";
  const bar = document.getElementById("cockpit-bar");
  if (!bar) return;
  const cb = (key) => bar.querySelector('[data-cb="' + key + '"]');

  const SIDEREAL_RATE = 1.00273790935; // sidereal seconds per solar second
  let lstBaseHours = null; // LST at serverEpochSec (from the last poll)
  let serverEpochSec = null;

  const pad = (n) => String(n).padStart(2, "0");

  function hoursToHM(h) {
    h = ((h % 24) + 24) % 24;
    const hh = Math.floor(h);
    const mm = Math.floor((h - hh) * 60);
    return pad(hh) + ":" + pad(mm);
  }

  function fmtOffset(v) {
    if (v === null || v === undefined) return "0.0";
    return (v >= 0 ? "+" : "") + v.toFixed(1);
  }

  function setText(key, text) {
    const el = cb(key);
    if (el) el.textContent = text;
  }

  function tick() {
    const now = new Date();
    setText("utc", now.toISOString().slice(11, 19));
    setText("local", now.toLocaleTimeString());
    if (lstBaseHours !== null && serverEpochSec !== null) {
      const elapsed = Date.now() / 1000 - serverEpochSec;
      setText("lst", hoursToHM(lstBaseHours + (elapsed * SIDEREAL_RATE) / 3600));
    }
  }

  function renderStation(s) {
    const el = cb("station");
    if (!el) return;
    s = s || {};
    if (!s.name) {
      el.textContent = "no station";
      el.className = "cb-item cb-station cb-muted";
      return;
    }
    const cal = s.calibrated
      ? "Δaz " + fmtOffset(s.offset_az_deg) + " Δel " + fmtOffset(s.offset_el_deg)
      : "uncalibrated";
    el.textContent = s.name + " · " + cal;
    el.className = "cb-item cb-station" + (s.calibrated ? "" : " cb-warn");
  }

  function renderSource(src) {
    src = src || {};
    const dot = cb("source-dot");
    if (dot) {
      let state = "cb-ok";
      if (!src.reachable) state = "cb-bad";
      else if (src.stale) state = "cb-warn";
      dot.className = "cb-dot " + state;
    }
    const txt = cb("source");
    if (txt) {
      if (!src.reachable) {
        txt.textContent = "daemon down";
      } else {
        const fps = src.fps !== null && src.fps !== undefined ? " " + src.fps.toFixed(1) + "fps" : "";
        txt.textContent = (src.source || "?") + fps;
      }
    }
  }

  function renderWeather(w) {
    const el = cb("weather");
    if (!el) return;
    if (w && w.temp_c !== null && w.temp_c !== undefined) {
      el.textContent = Math.round(w.temp_c) + "°C" + (w.summary ? " " + w.summary : "");
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  }

  function renderDisk(disk) {
    const el = cb("disk");
    if (!el) return;
    disk = disk || {};
    if (disk.status === "unavailable" || disk.free_gb === undefined) {
      el.textContent = "disk ?";
      el.className = "cb-item cb-disk cb-muted";
      return;
    }
    const hrs =
      disk.sigmf_hours_remaining !== null && disk.sigmf_hours_remaining !== undefined
        ? " · ~" + disk.sigmf_hours_remaining + "h SigMF"
        : "";
    el.textContent = Math.round(disk.free_gb) + "GB" + hrs;
    const tone = disk.status === "error" ? " cb-bad" : disk.status === "warn" ? " cb-warn" : "";
    el.className = "cb-item cb-disk" + tone;
  }

  function render(d) {
    if (d.lst_hours !== null && d.lst_hours !== undefined && d.server_time_utc) {
      lstBaseHours = d.lst_hours;
      serverEpochSec = Date.parse(d.server_time_utc) / 1000;
    }
    renderStation(d.station);
    renderSource(d.source);
    renderWeather(d.weather);
    renderDisk(d.disk);
  }

  async function refresh() {
    try {
      const r = await fetch("/api/status_bar", { cache: "no-store" });
      if (r.ok) render(await r.json());
    } catch (e) {
      /* transient — keep the last-good values and try again next tick */
    }
  }

  tick();
  refresh();
  setInterval(tick, 1000);
  setInterval(refresh, 10000);
})();

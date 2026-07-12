// Cockpit UI preferences (roadmap M6): theme (auto/light/dark) and timestamp
// display (browser-local vs UTC), both persisted in localStorage. No framework;
// safe to load on any page — the controls are optional and localization is a
// no-op when there are no <time class="ts"> elements.
(function () {
  "use strict";
  var root = document.documentElement;

  // --- theme: dark (default) ↔ light --------------------------------------
  // Dark is the app's default regardless of the OS preference (the CSS default);
  // light is opt-in and persisted. The button shows the theme you'd switch TO.
  function currentTheme() {
    return localStorage.getItem("theme") === "light" ? "light" : "dark";
  }

  function applyTheme(theme) {
    if (theme === "light") root.setAttribute("data-theme", "light");
    else root.removeAttribute("data-theme"); // dark = the CSS default
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = theme === "light" ? "🌙 Dark" : "☀ Light";
    // waterfall.js re-reads its canvas colors from CSS on this event.
    window.dispatchEvent(new CustomEvent("themechange"));
  }

  function cycleTheme() {
    var next = currentTheme() === "light" ? "dark" : "light";
    localStorage.setItem("theme", next);
    applyTheme(next);
  }

  // --- timestamp display: local (browser locale/tz) vs UTC ----------------
  function timeMode() {
    return localStorage.getItem("timeMode") === "utc" ? "utc" : "local";
  }

  function localizeAll() {
    var mode = timeMode();
    var nodes = document.querySelectorAll("time.ts");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var iso = el.getAttribute("datetime");
      if (!iso) continue;
      if (el.dataset.utc === undefined) el.dataset.utc = el.textContent; // keep the UTC text
      if (mode === "utc") {
        el.textContent = el.dataset.utc;
        el.removeAttribute("title");
        continue;
      }
      var d = new Date(iso);
      if (isNaN(d.getTime())) {
        el.textContent = el.dataset.utc;
        continue;
      }
      el.textContent = d.toLocaleString([], {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
      el.title = el.dataset.utc; // hover shows the canonical UTC
    }
    var btn = document.getElementById("time-toggle");
    if (btn) btn.textContent = mode === "utc" ? "UTC" : "Local";
  }

  function toggleTime() {
    localStorage.setItem("timeMode", timeMode() === "utc" ? "local" : "utc");
    localizeAll();
  }

  function init() {
    applyTheme(currentTheme());
    localizeAll();
    var themeBtn = document.getElementById("theme-toggle");
    if (themeBtn) themeBtn.addEventListener("click", cycleTheme);
    var timeBtn = document.getElementById("time-toggle");
    if (timeBtn) timeBtn.addEventListener("click", toggleTime);
    // Fragments swapped in by htmx bring fresh <time> elements to localize.
    if (document.body) document.body.addEventListener("htmx:afterSwap", localizeAll);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

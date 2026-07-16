"""Live-view smoke: the synthetic pipe actually paints the browser canvases,
and the nav is not a dead end (roadmap browser-only fix)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

# Reads a canvas back with getImageData and reports whole-canvas mean luminance,
# a count of pixels that differ from the (long-lived) bottom-left background, and
# the mean luminance of the top vs. bottom 10% bands. The waterfall paints newest
# rows at y=0 and scrolls down, so a painted top band standing above a still-blank
# bottom band is the "real frames arrived and drew" signal.
_CANVAS_STATS_JS = """
(id) => {
  const c = document.getElementById(id);
  if (!c) return null;
  const w = c.width, h = c.height;
  if (!w || !h) return null;
  const d = c.getContext('2d').getImageData(0, 0, w, h).data;
  const base = (h - 1) * w * 4;              // bottom-left pixel = background
  const bx = d[base], by = d[base + 1], bz = d[base + 2];
  const band = Math.max(1, Math.floor(h * 0.1));
  let sum = 0, painted = 0, topSum = 0, topN = 0, botSum = 0, botN = 0;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const r = d[i], g = d[i + 1], b = d[i + 2];
      const lum = (r + g + b) / 3;
      sum += lum;
      if (Math.abs(r - bx) + Math.abs(g - by) + Math.abs(b - bz) > 24) painted++;
      if (y < band) { topSum += lum; topN++; }
      if (y >= h - band) { botSum += lum; botN++; }
    }
  }
  return { width: w, height: h, mean: sum / (w * h), painted,
           topMean: topSum / topN, bottomMean: botSum / botN };
}
"""


@pytest.mark.e2e
def test_live_view_renders(page: Page, live_server: str) -> None:
    """The three canvases paint real synthetic frames and #stat-tp goes numeric."""
    page.goto(f"{live_server}/")

    # A numeric total-power readout (not the "–" placeholder) means the daemon ->
    # ZMQ -> server -> WebSocket -> canvas pipe delivered at least one frame.
    page.wait_for_function(
        "() => { const el = document.getElementById('stat-tp');"
        " return el && /^-?\\d/.test(el.textContent.trim()); }",
        timeout=15_000,
    )
    tp_text = page.locator("#stat-tp").inner_text().strip()
    assert re.match(r"^-?\d", tp_text), f"#stat-tp is not numeric: {tp_text!r}"
    float(tp_text)  # parses as a real number

    # Let a handful of waterfall rows accumulate (synthetic runs ~4 fps).
    page.wait_for_timeout(3_000)

    waterfall = page.evaluate(_CANVAS_STATS_JS, "waterfall")
    assert waterfall is not None, "waterfall canvas has zero size"
    # Newest rows (top band) carry colormapped spectra; the bottom band is still the
    # untouched background — a clear top-vs-bottom brightness gap proves it painted.
    assert waterfall["topMean"] > waterfall["bottomMean"] + 5, waterfall
    assert waterfall["painted"] > waterfall["width"], waterfall

    # The spectrum plot fills its whole area with grid + trace once a frame lands.
    spectrum = page.evaluate(_CANVAS_STATS_JS, "spectrum")
    assert spectrum is not None, "spectrum canvas has zero size"
    assert spectrum["painted"] > spectrum["width"], spectrum

    # The total-power strip draws an axis + trace + fill.
    totalpower = page.evaluate(_CANVAS_STATS_JS, "totalpower")
    assert totalpower is not None, "totalpower canvas has zero size"
    assert totalpower["painted"] > 100, totalpower


@pytest.mark.e2e
def test_nav_reaches_wizard(page: Page, live_server: str) -> None:
    """The live view links to the wizard (live view is not a dead end)."""
    page.goto(f"{live_server}/")

    topnav = page.locator("#topnav")
    expect(topnav).to_be_visible()

    page.locator('#topnav a[href="/wizard"]').click()

    expect(page).to_have_url(re.compile(r"/wizard$"))
    expect(page.get_by_role("heading", name=re.compile("New session"))).to_be_visible()
    expect(page.locator("#type-select")).to_be_visible()

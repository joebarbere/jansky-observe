"""Record a short .npz capture from the live view, then classify it on its
observation and assert a verdict badge appears (the M1->M3 pipe, in a browser)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._helpers import start_running_observation


@pytest.mark.e2e
def test_record_and_classify(page: Page, live_server: str) -> None:
    """Record spectra (.npz) -> Stop -> Classify yields a verdict badge."""
    # A running observation so the recorded capture links to it (and therefore
    # shows up, classifiable, on the observation detail page).
    obs_url = start_running_observation(page, live_server)

    page.goto(f"{live_server}/")

    # The capture buttons enable once the status poll reaches the daemon.
    start_btn = page.locator("#btn-start-npz")
    expect(start_btn).to_be_enabled(timeout=15_000)

    with page.expect_response(
        lambda r: "/api/capture/start" in r.url and r.request.method == "POST"
    ) as start_info:
        start_btn.click()
    assert start_info.value.ok, f"capture start failed: {start_info.value.status}"

    # Recording is live: wait for Stop to arm, then let a few seconds of synthetic
    # frames land in the .npz before stopping.
    stop_btn = page.locator("#btn-stop")
    expect(stop_btn).to_be_enabled()
    page.wait_for_timeout(4_000)

    with page.expect_response(
        lambda r: "/api/capture/stop" in r.url and r.request.method == "POST"
    ) as stop_info:
        stop_btn.click()
    stop_reply = stop_info.value
    assert stop_reply.ok, f"capture stop failed: {stop_reply.status}"
    capture_id = stop_reply.json().get("capture_id")
    assert capture_id is not None, f"stop did not register a capture: {stop_reply.json()}"

    # On the observation detail page the capture is classifiable (npz_spectra).
    page.goto(obs_url)
    confirm_cell = page.locator(f"#cap-confirm-{capture_id}")
    expect(confirm_cell).to_be_visible()
    confirm_cell.get_by_role("button", name="Classify", exact=True).click()

    # The classify fragment swaps in a verdict badge with an SNR score.
    expect(confirm_cell.locator(".cap-result .badge")).to_be_visible()
    expect(confirm_cell).to_contain_text("SNR")

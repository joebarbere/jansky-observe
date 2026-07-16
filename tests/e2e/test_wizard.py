"""The flagship flow: drive the session wizard end to end to a running observation."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_wizard_starts_observation(page: Page, live_server: str) -> None:
    """Wizard step 1 -> 4 with seeded data lands on a *running* observation."""
    page.goto(f"{live_server}/wizard")

    # Step 1: pick the HI Cygnus observation type (match on text to dodge the em
    # dash in the option label); the seeded "Home" location is selected by default.
    type_option = page.locator("#type-select option", has_text="Cygnus region").first
    type_value = type_option.get_attribute("value")
    assert type_value is not None
    page.locator("#type-select").select_option(value=type_value)
    page.get_by_role("button", name="Next: pick source").click()

    # Step 2: pick a seeded source (Cas A). Selecting the radio also fires an htmx
    # pointing/weather load, but the test only needs the selection.
    expect(page.get_by_role("heading", name=re.compile("step 2"))).to_be_visible()
    page.get_by_role("radio", name="Cas A").check()
    page.get_by_role("button", name="Next: point the dish").click()

    # Step 3: the dialed az/el are pre-filled from the computed pointing — save them.
    expect(page).to_have_url(re.compile(r"/wizard/\d+/step3$"))
    expect(page.locator("input[name='pointing_az_deg']")).to_have_count(1)
    page.get_by_role("button", name="Next: checklist").click()

    # Step 4: tick every required checklist item; each tick htmx-swaps the fragment,
    # so re-query the first still-unchecked required box until none remain.
    expect(page).to_have_url(re.compile(r"/wizard/\d+/step4$"))
    required_unchecked = "#checklist li:has(span.req) input[name='checked']:not(:checked)"
    for _ in range(20):
        boxes = page.locator(required_unchecked)
        if boxes.count() == 0:
            break
        with page.expect_response(lambda r: "/checklist/" in r.url):
            boxes.first.check()
    assert page.locator(required_unchecked).count() == 0, "required checklist items remain unticked"

    start = page.locator("#btn-start-obs")
    expect(start).to_be_enabled()
    start.click()

    # The Start transition redirects to the observation detail page, now running.
    expect(page).to_have_url(re.compile(r"/observations/\d+$"))
    expect(page.locator("h1 .badge.running")).to_have_text("running")

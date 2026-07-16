"""Shared browser flows for the e2e suite."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


def start_running_observation(page: Page, base_url: str) -> str:
    """Drive the wizard to a running observation; return its detail URL.

    A condensed version of ``test_wizard`` used as setup by other tests that
    need a running observation (e.g. so a recorded capture links to it).
    """
    page.goto(f"{base_url}/wizard")

    type_option = page.locator("#type-select option", has_text="Cygnus region").first
    type_value = type_option.get_attribute("value")
    assert type_value is not None
    page.locator("#type-select").select_option(value=type_value)
    page.get_by_role("button", name="Next: pick source").click()

    page.get_by_role("radio", name="Cas A").check()
    page.get_by_role("button", name="Next: point the dish").click()

    expect(page).to_have_url(re.compile(r"/wizard/\d+/step3$"))
    page.get_by_role("button", name="Next: checklist").click()

    expect(page).to_have_url(re.compile(r"/wizard/\d+/step4$"))
    required_unchecked = "#checklist li:has(span.req) input[name='checked']:not(:checked)"
    for _ in range(20):
        boxes = page.locator(required_unchecked)
        if boxes.count() == 0:
            break
        with page.expect_response(lambda r: "/checklist/" in r.url):
            boxes.first.check()

    start = page.locator("#btn-start-obs")
    expect(start).to_be_enabled()
    start.click()

    expect(page).to_have_url(re.compile(r"/observations/\d+$"))
    expect(page.locator("h1 .badge.running")).to_have_text("running")
    return page.url

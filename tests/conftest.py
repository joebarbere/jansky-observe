"""Shared test fixtures.

The only thing here is hermeticity for process-global state. Add sparingly: the house
style is self-contained tests with explicit fixtures, not implicit shared setup.
"""

from __future__ import annotations

import pytest

from jansky_observe.astro import hi_reference


@pytest.fixture(autouse=True)
def _clear_profile_memo() -> None:
    """Reset the per-pointing reference-profile memo around every test.

    The memo is process-global (plans/calibrated-overlay.md). Its key includes the
    cache dir, so today's tests cannot collide via their ``tmp_path`` fixtures --- but a
    future test using the default cache dir would inherit whatever ran before it, and
    order-dependent flakes are miserable to diagnose. Clearing costs nothing.
    """
    hi_reference.clear_profile_memo()

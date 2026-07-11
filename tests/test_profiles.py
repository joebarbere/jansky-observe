"""GUARD TEST — the bias-tee safety rule (capture/profiles.py).

The Airspy Mini internal bias tee (4.5 V, ~50 mA) cannot power the 120 mA
H-line feed (which is powered by the dedicated inline USB-C bias-tee
injector) and must NEVER be enabled in the H-line profile. Any diff touching
device profiles must keep these tests passing.
"""

from __future__ import annotations

import dataclasses

import pytest

from jansky_observe.capture.profiles import (
    HLINE_AIRSPY,
    BiasTeeForbiddenError,
    DeviceProfile,
    validate_profile,
)


def test_hline_profile_bias_tee_is_off() -> None:
    assert HLINE_AIRSPY.bias_tee is False
    assert HLINE_AIRSPY.device == "airspy"


def test_device_profile_is_frozen() -> None:
    profile = dataclasses.replace(HLINE_AIRSPY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.bias_tee = True  # type: ignore[misc]


def test_validate_rejects_bias_tee_on_hline_airspy() -> None:
    bad = dataclasses.replace(HLINE_AIRSPY, name="hline-airspy-bad", bias_tee=True)
    with pytest.raises(BiasTeeForbiddenError):
        validate_profile(bad)


def test_validate_accepts_the_shipped_hline_profile() -> None:
    assert validate_profile(HLINE_AIRSPY) is HLINE_AIRSPY


def test_validate_allows_bias_tee_away_from_hline() -> None:
    # The rule is specific to the H-line feed chain: an Airspy profile far
    # from 1420 MHz powering some other antenna may use the internal bias tee.
    other = DeviceProfile(
        name="fm-band-airspy",
        device="airspy",
        center_freq_hz=100e6,
        sample_rate_hz=6e6,
        bias_tee=True,
    )
    assert validate_profile(other) is other

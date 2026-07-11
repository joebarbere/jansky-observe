"""SDR device profiles and the bias-tee safety rule. SAFETY-CRITICAL.

**The bias-tee rule.** The Airspy Mini's internal bias tee supplies 4.5 V at
about 50 mA — *not enough* for the H-line feed's LNA, which draws about
120 mA. The feed is powered by the dedicated inline USB-C bias-tee injector
instead. Enabling the Airspy's internal bias tee in the H-line profile would
at best brown out the feed and at worst damage the Airspy, so it must
**NEVER** be enabled in the H-line profile: no ``airspy_rx -b 1``, no Soapy
``biastee`` setting. :func:`validate_profile` enforces this structurally,
:data:`HLINE_AIRSPY` is validated at import time, and
``tests/test_profiles.py`` is the guard test — any diff touching this module
must preserve all three.
"""

from __future__ import annotations

from dataclasses import dataclass

HI_LINE_FREQ_HZ = 1_420_405_751.7667
"""Rest frequency of the neutral-hydrogen 21 cm line (Hz)."""

_HLINE_BAND_HALF_WIDTH_HZ = 20e6
"""Half-width of the band around the HI line treated as 'the H-line profile'."""

__all__ = [
    "HI_LINE_FREQ_HZ",
    "HLINE_AIRSPY",
    "BiasTeeForbiddenError",
    "DeviceProfile",
    "validate_profile",
]


class BiasTeeForbiddenError(ValueError):
    """Raised when a profile would enable the Airspy internal bias tee at H-line."""


@dataclass(frozen=True)
class DeviceProfile:
    """Immutable capture settings for one SDR device in one role.

    Attributes
    ----------
    name
        Human-readable profile name (unique within the station).
    device
        Device kind, e.g. ``"airspy"`` or ``"hackrf"``.
    center_freq_hz
        RF tuning frequency (Hz).
    sample_rate_hz
        Complex sample rate (Hz).
    bias_tee
        Whether the device's *internal* bias tee is enabled. Must be ``False``
        for any Airspy H-line profile — see the module docstring.
    """

    name: str
    device: str
    center_freq_hz: float
    sample_rate_hz: float
    bias_tee: bool


def _is_airspy_hline(profile: DeviceProfile) -> bool:
    """Whether ``profile`` is an Airspy profile tuned into the H-line band."""
    return (
        profile.device == "airspy"
        and abs(profile.center_freq_hz - HI_LINE_FREQ_HZ) <= _HLINE_BAND_HALF_WIDTH_HZ
    )


def validate_profile(profile: DeviceProfile) -> DeviceProfile:
    """Validate a profile against the station safety rules; return it unchanged.

    The one rule at M0 is the bias-tee rule (module docstring): the Airspy
    Mini internal bias tee (4.5 V, ~50 mA) cannot power the 120 mA H-line
    feed — the feed is powered by the dedicated inline USB-C bias-tee
    injector — so the internal bias tee must never be enabled in the H-line
    profile.

    Parameters
    ----------
    profile
        The profile to validate.

    Returns
    -------
    DeviceProfile
        The same profile, so definitions can be wrapped in-line.

    Raises
    ------
    BiasTeeForbiddenError
        If an Airspy profile tuned into the H-line band enables the internal
        bias tee.
    """
    if _is_airspy_hline(profile) and profile.bias_tee:
        raise BiasTeeForbiddenError(
            f"profile {profile.name!r} enables the Airspy internal bias tee at "
            "the H-line: the internal bias tee (4.5 V, ~50 mA) cannot power the "
            "120 mA H-line feed (powered by the inline USB-C injector) and must "
            "NEVER be enabled in the H-line profile"
        )
    return profile


HLINE_AIRSPY = validate_profile(
    DeviceProfile(
        name="hline-airspy",
        device="airspy",
        center_freq_hz=1420.4e6,
        sample_rate_hz=3e6,
        bias_tee=False,  # SAFETY: never True — see the module docstring.
    )
)
"""The station H-line profile: Airspy Mini at 3 MSPS, internal bias tee OFF."""

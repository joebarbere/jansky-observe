"""Deterministic synthetic IQ: noise floor + a fake galactic-HI line.

The M0 walking skeleton has no hardware, so the capture daemon streams
*synthetic* complex baseband instead: a complex white Gaussian noise floor,
a Gaussian-profile HI emission bump near the 21 cm rest frequency, a gentle
baseline ripple (a stand-in for real passband shape), and a slow wobble of
the line power so a live waterfall visibly breathes.

Implementation: shaped noise. Each chunk is white complex Gaussian noise
drawn in the frequency domain, multiplied by the square root of the target
power envelope (flat floor + ripple + Gaussian line), and inverse-FFTed back
to the time domain. The line is therefore noise-like — exactly what a real
HI detection looks like — and the Welch PSD of the output shows the bump
directly.

Everything is seeded through :func:`jansky.signals.rng`, so the same seed
(and the same read sequence) reproduces identical samples — the tests and
the synthetic capture daemon rely on this.
"""

from __future__ import annotations

import numpy as np

# Re-export the house seeded-generator helper so callers seed one way everywhere.
from jansky.signals import rng as rng

HI_REST_FREQ_HZ = 1_420_405_751.7667
"""Rest frequency of the neutral-hydrogen 21 cm line (Hz)."""

DEFAULT_CENTER_FREQ_HZ = 1420.4e6
"""Default tuning frequency: just below the HI rest frequency (Hz)."""

DEFAULT_SAMPLE_RATE_HZ = 3e6
"""Default sample rate: the Airspy Mini's 3 MSPS low rate (Hz)."""

_FWHM_TO_SIGMA = 1.0 / (2.0 * np.sqrt(2.0 * np.log(2.0)))

__all__ = [
    "DEFAULT_CENTER_FREQ_HZ",
    "DEFAULT_SAMPLE_RATE_HZ",
    "HI_REST_FREQ_HZ",
    "hi_iq_chunk",
    "rng",
]


def hi_iq_chunk(
    n_samples: int,
    generator: np.random.Generator,
    *,
    t0_s: float = 0.0,
    center_freq_hz: float = DEFAULT_CENTER_FREQ_HZ,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    noise_floor: float = 1.0,
    line_amplitude: float = 3.0,
    line_width_hz: float = 100e3,
    line_offset_hz: float = 0.0,
    ripple_amplitude: float = 0.1,
    ripple_period_hz: float = 600e3,
    wobble_depth: float = 0.15,
    wobble_period_s: float = 0.25,
) -> np.ndarray:
    """One chunk of synthetic complex64 IQ containing noise plus a fake HI line.

    Parameters
    ----------
    n_samples
        Number of IQ samples to generate.
    generator
        Seeded NumPy generator (use :func:`jansky.signals.rng`). Feeding the
        same generator state the same call sequence reproduces identical IQ.
    t0_s
        Signal time of the first sample (s). Drives the slow wobble, so a
        stateful caller advancing ``t0_s`` chunk by chunk gets a live-looking
        waterfall while staying fully deterministic.
    center_freq_hz
        Tuning (RF center) frequency in Hz; sets where the HI line lands in
        baseband.
    sample_rate_hz
        Complex sample rate in Hz.
    noise_floor
        RMS amplitude of the white-noise floor (power ``noise_floor**2``).
    line_amplitude
        HI line strength relative to the noise floor: the line's *peak* PSD is
        ``line_amplitude**2`` times the floor PSD (the default 3.0 is a ~10 dB
        bump).
    line_width_hz
        Full width at half maximum of the Gaussian spectral profile (Hz).
    line_offset_hz
        Extra frequency offset of the line from the HI rest frequency (Hz) —
        a stand-in for Doppler shift.
    ripple_amplitude
        Fractional depth of the sinusoidal baseline ripple, in ``[0, 1)``.
    ripple_period_hz
        Period of the baseline ripple across the band (Hz).
    wobble_depth
        Fractional slow modulation of the line power, in ``[0, 1)``.
    wobble_period_s
        Period of the wobble in *signal* seconds (samples consumed divided by
        the sample rate, not wall-clock time).

    Returns
    -------
    numpy.ndarray
        ``complex64`` array of shape ``(n_samples,)``.

    Raises
    ------
    ValueError
        If ``n_samples`` is not positive or a shape parameter is out of range.
    """
    if n_samples <= 0:
        raise ValueError("n_samples must be positive")
    if noise_floor <= 0 or line_width_hz <= 0 or ripple_period_hz <= 0 or wobble_period_s <= 0:
        raise ValueError(
            "noise_floor, line_width_hz, ripple_period_hz, wobble_period_s must be positive"
        )
    if not 0.0 <= ripple_amplitude < 1.0 or not 0.0 <= wobble_depth < 1.0:
        raise ValueError("ripple_amplitude and wobble_depth must be in [0, 1)")

    freqs = np.fft.fftfreq(n_samples, d=1.0 / sample_rate_hz)
    line_freq_hz = HI_REST_FREQ_HZ - center_freq_hz + line_offset_hz
    sigma_hz = line_width_hz * _FWHM_TO_SIGMA
    # Slow wobble of the line power, evaluated at the chunk midpoint.
    t_mid = t0_s + 0.5 * n_samples / sample_rate_hz
    wobble = 1.0 + wobble_depth * np.sin(2.0 * np.pi * t_mid / wobble_period_s)
    # Target power envelope: flat floor x gentle ripple + Gaussian HI bump.
    psd = noise_floor**2 * (1.0 + ripple_amplitude * np.cos(2.0 * np.pi * freqs / ripple_period_hz))
    psd = psd + (line_amplitude * noise_floor) ** 2 * wobble * np.exp(
        -0.5 * ((freqs - line_freq_hz) / sigma_hz) ** 2
    )
    # White complex Gaussian noise shaped in the frequency domain. The
    # sqrt(n) factor makes the time-domain RMS equal sqrt(psd) per bin.
    white = (
        generator.standard_normal(n_samples) + 1j * generator.standard_normal(n_samples)
    ) / np.sqrt(2.0)
    spectrum = white * np.sqrt(psd * n_samples)
    return np.fft.ifft(spectrum).astype(np.complex64)

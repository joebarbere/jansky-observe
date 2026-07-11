"""SDR sample sources behind one small protocol.

At M0 the only implementation is :class:`SyntheticHISource`, which streams
deterministic synthetic noise + fake-HI IQ from
:mod:`jansky_observe.synthetic` — no hardware anywhere. The real Airspy Mini
source (``airspy_rx`` subprocess first, SoapyAirspy later) arrives at M1 and
will implement the same :class:`SDRSource` protocol, so the daemon and DSP
code never change.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from jansky_observe.synthetic import (
    DEFAULT_CENTER_FREQ_HZ,
    DEFAULT_SAMPLE_RATE_HZ,
    hi_iq_chunk,
    rng,
)

__all__ = ["SDRSource", "SyntheticHISource"]


@runtime_checkable
class SDRSource(Protocol):
    """What the capture daemon needs from any SDR (or fake SDR)."""

    center_freq_hz: float
    """RF tuning frequency (Hz)."""

    sample_rate_hz: float
    """Complex sample rate (Hz)."""

    def read(self, n_samples: int) -> np.ndarray:
        """Return the next ``n_samples`` complex64 IQ samples."""
        ...

    def close(self) -> None:
        """Release the device; further reads are an error."""
        ...


class SyntheticHISource:
    """Deterministic synthetic source: noise floor + fake galactic HI line.

    Wraps :func:`jansky_observe.synthetic.hi_iq_chunk`, advancing signal time
    across reads so the line wobbles slowly and the waterfall looks alive.
    Two sources built with the same seed and read with the same call sequence
    produce identical samples.

    Parameters
    ----------
    center_freq_hz, sample_rate_hz
        Tuning and rate; defaults match the Airspy H-line profile.
    seed
        Seed for :func:`jansky.signals.rng`. ``None`` gives a fresh,
        non-reproducible stream.
    noise_floor, line_amplitude, line_width_hz, line_offset_hz
        Signal shape — see :func:`jansky_observe.synthetic.hi_iq_chunk`.
    ripple_amplitude, ripple_period_hz, wobble_depth, wobble_period_s
        Baseline ripple and slow line wobble — same reference.
    """

    def __init__(
        self,
        *,
        center_freq_hz: float = DEFAULT_CENTER_FREQ_HZ,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        seed: int | None = 0,
        noise_floor: float = 1.0,
        line_amplitude: float = 3.0,
        line_width_hz: float = 100e3,
        line_offset_hz: float = 0.0,
        ripple_amplitude: float = 0.1,
        ripple_period_hz: float = 600e3,
        wobble_depth: float = 0.15,
        wobble_period_s: float = 0.25,
    ) -> None:
        self.center_freq_hz = float(center_freq_hz)
        self.sample_rate_hz = float(sample_rate_hz)
        self._noise_floor = noise_floor
        self._line_amplitude = line_amplitude
        self._line_width_hz = line_width_hz
        self._line_offset_hz = line_offset_hz
        self._ripple_amplitude = ripple_amplitude
        self._ripple_period_hz = ripple_period_hz
        self._wobble_depth = wobble_depth
        self._wobble_period_s = wobble_period_s
        self._generator = rng(seed)
        self._t0_s = 0.0
        self._closed = False

    def read(self, n_samples: int) -> np.ndarray:
        """Return the next ``n_samples`` synthetic complex64 IQ samples."""
        if self._closed:
            raise RuntimeError("read() on a closed SyntheticHISource")
        chunk = hi_iq_chunk(
            n_samples,
            self._generator,
            t0_s=self._t0_s,
            center_freq_hz=self.center_freq_hz,
            sample_rate_hz=self.sample_rate_hz,
            noise_floor=self._noise_floor,
            line_amplitude=self._line_amplitude,
            line_width_hz=self._line_width_hz,
            line_offset_hz=self._line_offset_hz,
            ripple_amplitude=self._ripple_amplitude,
            ripple_period_hz=self._ripple_period_hz,
            wobble_depth=self._wobble_depth,
            wobble_period_s=self._wobble_period_s,
        )
        self._t0_s += n_samples / self.sample_rate_hz
        return chunk

    def close(self) -> None:
        """Mark the source closed; subsequent reads raise ``RuntimeError``."""
        self._closed = True

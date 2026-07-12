"""SDR sample sources behind one small protocol.

Two implementations: :class:`SyntheticHISource` (deterministic synthetic
noise + fake-HI IQ from :mod:`jansky_observe.synthetic` — no hardware) and
:class:`~jansky_observe.capture.airspy_cli.AirspyRxSource` (the real Airspy
Mini via an ``airspy_rx`` subprocess, M1). Both satisfy :class:`SDRSource`,
so the daemon and DSP code never change.

The protocol has two consumption modes (documented extension at M1):

- **live view** — :meth:`SDRSource.read` returns the *newest* contiguous
  samples; anything streamed between calls may be discarded (latest wins).
- **capture tap** — between :meth:`SDRSource.start_tap` and
  :meth:`SDRSource.stop_tap`, *every* sample the source produces is also
  queued gaplessly for :meth:`SDRSource.read_tap`. If the consumer falls
  behind and the bounded tap queue would overflow, the source drops samples
  and latches the sticky :attr:`SDRSource.overrun` flag — captures report
  overruns honestly, never silently gap.
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

    @property
    def overrun(self) -> bool:
        """Sticky flag: a capture tap dropped samples (consumer too slow)."""
        ...

    def read(self, n_samples: int) -> np.ndarray:
        """Return the newest ``n_samples`` complex64 IQ samples (live view)."""
        ...

    def start_tap(self) -> None:
        """Begin queuing every produced sample, gaplessly, for :meth:`read_tap`."""
        ...

    def read_tap(self) -> np.ndarray | None:
        """Drain queued tap samples as interleaved int16 I/Q; ``None`` when empty."""
        ...

    def stop_tap(self) -> None:
        """Stop queuing; clears the pending tap queue (overrun stays sticky)."""
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
        self._tap_active = False
        self._tap_chunks: list[np.ndarray] = []
        self._overrun = False

    @property
    def overrun(self) -> bool:
        """Sticky overrun flag (a synthetic source never truly overruns)."""
        return self._overrun

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
        if self._tap_active:
            # Gapless by construction: synthetic time only advances on read(),
            # so the tap is exactly the stream. Convert to the tap's interleaved
            # int16 wire form (what the Airspy delivers and SigMF stores).
            scaled = np.clip(chunk * 32767.0, -32768, 32767)
            interleaved = np.empty(2 * chunk.size, dtype=np.int16)
            interleaved[0::2] = scaled.real.astype(np.int16)
            interleaved[1::2] = scaled.imag.astype(np.int16)
            self._tap_chunks.append(interleaved)
        return chunk

    def start_tap(self) -> None:
        """Begin collecting every read() chunk for :meth:`read_tap`."""
        self._tap_chunks = []
        self._tap_active = True

    def read_tap(self) -> np.ndarray | None:
        """Drain collected tap samples (interleaved int16 I/Q); ``None`` if empty."""
        if not self._tap_chunks:
            return None
        chunks, self._tap_chunks = self._tap_chunks, []
        return np.concatenate(chunks)

    def stop_tap(self) -> None:
        """Stop collecting; pending tap samples are discarded."""
        self._tap_active = False
        self._tap_chunks = []

    def close(self) -> None:
        """Mark the source closed; subsequent reads raise ``RuntimeError``."""
        self._closed = True

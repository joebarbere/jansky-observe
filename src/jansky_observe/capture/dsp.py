"""Small, pure DSP helpers for the capture daemon.

One job at M0: reduce a chunk of complex IQ to the fftshifted Welch power
spectrum (dB) that :mod:`jansky_observe.frames` carries over the wire.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

_PSD_FLOOR = 1e-20
"""Smallest PSD value passed to log10, i.e. a -200 dB floor (avoids -inf)."""

__all__ = ["welch_psd_db"]


def welch_psd_db(
    iq: np.ndarray,
    sample_rate_hz: float,
    n_fft: int,
    *,
    window: str = "hann",
) -> np.ndarray:
    """Welch power spectral density of complex IQ, fftshifted, in dB.

    Segments are non-overlapping and mean-averaged, so a chunk of
    ``n_fft * averages`` samples yields exactly ``averages`` Welch segments.

    Parameters
    ----------
    iq
        Complex baseband samples; must contain at least ``n_fft`` samples.
    sample_rate_hz
        Complex sample rate (Hz).
    n_fft
        FFT length per Welch segment; also the number of output bins.
    window
        Window passed to :func:`scipy.signal.welch`.

    Returns
    -------
    numpy.ndarray
        ``float32`` array of ``n_fft`` power values in dB (relative to an
        arbitrary reference), fftshifted so index 0 is the lowest frequency
        (``-sample_rate_hz / 2`` relative to center). Values are floored to
        avoid ``-inf``.

    Raises
    ------
    ValueError
        If ``iq`` is shorter than ``n_fft``.
    """
    if iq.size < n_fft:
        raise ValueError(f"need at least n_fft={n_fft} samples, got {iq.size}")
    _, psd = signal.welch(
        iq,
        fs=sample_rate_hz,
        window=window,
        nperseg=n_fft,
        noverlap=0,
        detrend=False,
        return_onesided=False,
        scaling="density",
    )
    psd = np.fft.fftshift(psd)
    return (10.0 * np.log10(np.maximum(psd, _PSD_FLOOR))).astype(np.float32)

"""ON−OFF position-switch difference spectrum (roadmap M10, plan M10 Piece 2).

Position switching is the textbook amateur-HI confirmation: record an ON
pointing at the source and an OFF pointing at nearby blank sky, then difference
them. The OFF carries the receiver's bandpass shape (and the local RFI
environment) without the line, so the difference isolates the HI signal.

Two methods, both pure numpy over the two averaged spectra:

- ``"ratio"`` (default, the standard position-switch): ``on_lin / off_lin``
  divides out the receiver's frequency response — a ~flat spectrum with the HI
  line as the only bump — returned in dB.
- ``"subtract"``: ``on_lin - off_lin`` (floored before the log) — the weaker
  option for when the OFF is a poor bandpass match.

The result feeds straight into
:func:`jansky_observe.confirm.classifier.classify_spectrum` unchanged.
"""

from __future__ import annotations

import numpy as np

from jansky_observe.confirm.baseline import db_to_linear, linear_to_db

__all__ = ["difference_spectrum"]

_SUBTRACT_FLOOR = 1e-20
"""Smallest linear power kept when subtracting (a -200 dB floor, avoids ``-inf``)."""

_AXIS_ATOL_HZ = 1.0
"""Per-channel frequency tolerance (Hz) for calling two axes identical. Averaged
spectra built from the same ``(center, rate, n_fft)`` are bit-identical, so 1 Hz
comfortably admits equal axes while rejecting any real retune/resolution
mismatch (channel spacing is ~10 kHz)."""


def difference_spectrum(
    on_freq: np.ndarray,
    on_db: np.ndarray,
    off_freq: np.ndarray,
    off_db: np.ndarray,
    *,
    method: str = "ratio",
) -> tuple[np.ndarray, np.ndarray]:
    """Difference an ON and OFF averaged spectrum for ON−OFF confirmation.

    Parameters
    ----------
    on_freq, off_freq : numpy.ndarray
        Topocentric frequency axes in Hz for the ON and OFF captures. Must be
        the same shape and element-wise close (same ``center``/``rate``/
        ``n_fft``) — a mismatch raises :class:`ValueError`.
    on_db, off_db : numpy.ndarray
        The ON and OFF averaged power spectra in dB (same shape as their axes).
    method : str
        ``"ratio"`` (default): ``linear_to_db(on_lin / off_lin)`` — divides out
        the receiver bandpass. ``"subtract"``:
        ``linear_to_db(maximum(on_lin - off_lin, floor))``.

    Returns
    -------
    tuple of numpy.ndarray
        ``(freq_hz, diff_db)`` — the shared frequency axis (the ON axis) and the
        difference spectrum in dB.

    Raises
    ------
    ValueError
        If the axes disagree in shape or value, if the spectra shapes do not
        match their axes, or if ``method`` is not ``"ratio"`` or ``"subtract"``.
    """
    on_freq = np.asarray(on_freq, dtype=np.float64)
    off_freq = np.asarray(off_freq, dtype=np.float64)
    on_db = np.asarray(on_db, dtype=np.float64)
    off_db = np.asarray(off_db, dtype=np.float64)

    if not (on_freq.shape == off_freq.shape == on_db.shape == off_db.shape):
        raise ValueError(
            "ON/OFF axis mismatch: shapes "
            f"on_freq={on_freq.shape}, off_freq={off_freq.shape}, "
            f"on_db={on_db.shape}, off_db={off_db.shape} must all be equal"
        )
    if not np.allclose(on_freq, off_freq, rtol=0.0, atol=_AXIS_ATOL_HZ):
        raise ValueError(
            "ON/OFF frequency axes differ — captures must share the same "
            "center frequency, sample rate, and FFT size to be differenced"
        )

    on_lin = db_to_linear(on_db)
    off_lin = db_to_linear(off_db)
    if method == "ratio":
        diff_db = linear_to_db(on_lin / off_lin)
    elif method == "subtract":
        diff_db = linear_to_db(np.maximum(on_lin - off_lin, _SUBTRACT_FLOOR))
    else:
        raise ValueError(f"unknown difference method {method!r} (expected 'ratio' or 'subtract')")
    return on_freq, diff_db

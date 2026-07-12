"""One-way exporter: a capture's averaged spectrum as a Virgo-style CSV (plan §4.7).

Researched format (source: ``virgo/virgo.py`` ``plot()`` in
https://github.com/0xCoto/Virgo, read 2026-07-12): Virgo saves its
``spectra_csv`` with ``np.savetxt(..., delimiter=',', fmt='%1.6f')`` and
**no header row**. Without a calibration file it writes two columns —
frequency and the averaged spectrum::

    np.savetxt(spectra_csv, np.concatenate((frequency.reshape(channels, 1),
       avg_spectrum.reshape(channels, 1)), axis=1), delimiter=',', fmt='%1.6f')

(with a calibration file, four: frequency, avg_spectrum, avg_spectrum_cal,
calibrated spectrum). Virgo's ``frequency`` axis is
``np.linspace(f0 - bw/2, f0 + bw/2, channels, endpoint=False) * 1e-6`` —
bin-centre MHz starting at the band's low edge, exactly the fftshifted
``.npz`` frame convention — and ``avg_spectrum`` is the time-averaged
spectrum in dB (``10·log10``) when Virgo's default ``dB=True`` is set.

This exporter matches the uncalibrated two-column layout: frequency (MHz),
averaged power (dB). We record no calibration, so the four-column variant
does not apply.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from jansky_observe.confirm.classifier import averaged_spectrum

__all__ = ["export_virgo_csv"]


def export_virgo_csv(npz_path: str | Path, out_path: str | Path) -> Path:
    """Export a ``.npz`` capture's averaged spectrum as a Virgo-style CSV.

    Parameters
    ----------
    npz_path : str or Path
        A ``.npz`` written by
        :class:`jansky_observe.capture.writer.NpzCaptureWriter`.
    out_path : str or Path
        Destination ``.csv`` path (parent directories are created;
        overwritten if present).

    Returns
    -------
    Path
        The written file's path: comma-delimited, ``%1.6f``, no header,
        columns ``frequency_mhz, averaged_power_db``.
    """
    freq_hz, power_db = averaged_spectrum(npz_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savetxt(out, np.column_stack((freq_hz / 1e6, power_db)), delimiter=",", fmt="%1.6f")
    return out

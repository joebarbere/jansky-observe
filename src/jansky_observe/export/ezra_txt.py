"""One-way exporter: a capture's averaged spectrum as an ezRA ezCol ``.txt`` (plan §4.7).

Researched format (source: https://github.com/tedcline/ezRA —
``ezRA/ezCol.py`` header/data writer and ``ezRA/ezCon.py`` parser, read
2026-07-12; the LRO GNU-Radio precedent at https://www.astronomy.me.uk/14920-2
is password-protected, so the format below comes from the ezRA source
itself). ezCon ingests a ``.txt`` with a 3-line non-comment header, optional
pointing lines, ``#`` comment lines, and one-line data samples::

    from ezCol04b5ea.py
    lat 40.299512 long -105.084491 amsl 1524 name N0RQV8 ezb
    freqMin 1419.205 freqMax 1421.605 freqBinQty 256
    az 227.9 el 42.7
    # times are in UTC
    2022-02-15T05:30:55 10.523690382 10.570080895 ... (freqBinQty values)

ezCon parser rules that matter (``ezCon.py``, the ``inspecting`` loop):

- line 1 must start with the 10 characters ``from ezCol`` or the whole file
  is skipped;
- line 2 splits on whitespace: fields [1]/[3]/[5] are lat / long / amsl and
  the station name is fields[7:] joined (field [6] is the literal ``name``);
- line 3 fields [1]/[3]/[5] are freqMin (MHz) / freqMax (MHz) / freqBinQty;
- ``az <deg> el <deg>`` pointing lines may appear among the data lines;
- blank lines and lines starting ``#`` are ignored anywhere;
- data lines are a UTC timestamp ``YYYY-MM-DDTHH:MM:SS`` (astropy FITS
  format) followed by freqBinQty whitespace-separated LINEAR power values
  (ezCol writes ``{v:.9g}``; its header comment reads "frequency spectrums
  of RMS power = sqrt(mean of sum of squares)"), optionally followed by a
  flags token (``R`` marks reference-feed samples).

This exporter writes one data line — the capture's frame-averaged spectrum
converted from dB back to linear power — with freqMin/freqMax set to the
first/last bin-centre frequencies in MHz. Unknowable/placeholder fields:
line 1 is written as ``from ezCol-compatible jansky-observe …`` (satisfies
ezCon's ``from ezCol`` magic while staying honest about the producer), and
ezCol's optional ``# gain`` comment line is omitted (comments are ignored
by ezCon). The data-line timestamp is the capture's first frame timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from jansky_observe import __version__
from jansky_observe.confirm.baseline import db_to_linear
from jansky_observe.confirm.classifier import averaged_spectrum

__all__ = ["export_ezra_txt"]


def _first_timestamp_utc(npz_path: Path) -> datetime:
    """The capture's first frame timestamp (unix seconds) as UTC."""
    with np.load(npz_path, allow_pickle=False) as data:
        timestamps = np.asarray(data["timestamps"], dtype=np.float64)
    return datetime.fromtimestamp(float(timestamps[0]), tz=UTC)


def export_ezra_txt(
    npz_path: str | Path,
    out_path: str | Path,
    *,
    lat: float,
    lon: float,
    elev: float,
    azimuth_deg: float,
    elevation_deg: float,
    name: str = "jansky-observe",
) -> Path:
    """Export a ``.npz`` capture's averaged spectrum as an ezRA ezCol ``.txt``.

    Parameters
    ----------
    npz_path : str or Path
        A ``.npz`` written by
        :class:`jansky_observe.capture.writer.NpzCaptureWriter`.
    out_path : str or Path
        Destination ``.txt`` path (parent directories are created;
        overwritten if present).
    lat, lon : float
        Station geodetic latitude/longitude in degrees (ezRA header
        ``lat``/``long``).
    elev : float
        Station elevation above mean sea level in metres (ezRA ``amsl``).
    azimuth_deg, elevation_deg : float
        Dish pointing for the ``az``/``el`` line — ezRA records pointing in
        the data file, not in a config, so it is required here.
    name : str
        Station name for the header ``name`` field.

    Returns
    -------
    Path
        The written file's path: the researched 3-line header, one ``az/el``
        pointing line, comment lines, and one data line carrying the
        averaged spectrum as linear power (``{v:.9g}``).
    """
    src = Path(npz_path)
    freq_hz, power_db = averaged_spectrum(src)
    linear = db_to_linear(power_db)
    when = _first_timestamp_utc(src)

    # ezCol writes these fields with ':g'; its example files carry 7–8
    # significant figures, so we use '.9g' — ezCon float()s them either way
    # and bin-frequency precision matters at km/s velocity scales.
    lines = [
        f"from ezCol-compatible jansky-observe {__version__} (export.ezra_txt)",
        f"lat {lat:.9g} long {lon:.9g} amsl {elev:g} name {name}",
        f"freqMin {freq_hz[0] / 1e6:.9g} freqMax {freq_hz[-1] / 1e6:.9g} "
        f"freqBinQty {freq_hz.size:d}",
        f"az {azimuth_deg:g} el {elevation_deg:g}",
        "# times are in UTC",
        "# one-way export of the capture's frame-averaged spectrum (linear power)",
        # ezCol's antenna data lines end "<values> \n" (a blank flags token).
        when.strftime("%Y-%m-%dT%H:%M:%S ") + " ".join(f"{v:.9g}" for v in linear) + " ",
    ]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    return out

"""Tests for the one-way Virgo-CSV and ezRA-txt exporters (plan §4.7).

Fixtures follow the /synthetic-fixture pattern: real ``.npz`` captures
written by the real writer from synthetic fake-HI IQ. Format assertions
mirror what the receiving programs actually do: the Virgo CSV parses back
into the averaged spectrum, the ezRA txt satisfies ezCon's documented
parser rules (see each exporter's module docstring for the researched
format and sources).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from jansky.signals import rng

from jansky_observe import synthetic
from jansky_observe.capture.dsp import welch_psd_db
from jansky_observe.capture.writer import NpzCaptureWriter
from jansky_observe.confirm.baseline import db_to_linear
from jansky_observe.confirm.classifier import averaged_spectrum
from jansky_observe.export.ezra_txt import export_ezra_txt
from jansky_observe.export.virgo_csv import export_virgo_csv
from jansky_observe.frames import SpectralFrame

CENTER_HZ, RATE_HZ = 1420.4e6, 3e6
N_FFT = 256
T0 = 1_750_000_000.0
LAT, LON, ELEV_M = 40.02, -75.16, 100.0
AZ, EL = 123.4, 45.6


def _write_capture(path: Path, n_frames: int = 8, n_fft: int = N_FFT) -> Path:
    """Write an .npz capture of synthetic HI frames via the real writer."""
    gen = rng(7)
    writer = NpzCaptureWriter(path, settings={"gain": 15, "source": "synthetic"})
    samples_per_frame = n_fft * 32
    for i in range(n_frames):
        iq = synthetic.hi_iq_chunk(
            samples_per_frame,
            gen,
            t0_s=i * samples_per_frame / RATE_HZ,
            center_freq_hz=CENTER_HZ,
            sample_rate_hz=RATE_HZ,
        )
        writer.add_frame(
            SpectralFrame(
                seq=i,
                timestamp=T0 + i * 0.5,
                center_freq_hz=CENTER_HZ,
                sample_rate_hz=RATE_HZ,
                power_db=welch_psd_db(iq, RATE_HZ, n_fft),
            )
        )
    return writer.close()


class TestVirgoCsv:
    def test_round_trips_the_averaged_spectrum(self, tmp_path: Path) -> None:
        npz = _write_capture(tmp_path / "capture.npz")
        out = export_virgo_csv(npz, tmp_path / "exports" / "capture-virgo.csv")
        assert out.is_file()

        freq_hz, power_db = averaged_spectrum(npz)
        parsed = np.loadtxt(out, delimiter=",")
        assert parsed.shape == (N_FFT, 2)
        # fmt='%1.6f' → 1e-6 MHz = 1 Hz frequency resolution.
        np.testing.assert_allclose(parsed[:, 0], freq_hz / 1e6, atol=1e-6)
        np.testing.assert_allclose(parsed[:, 1], power_db, atol=1e-6)

    def test_layout_matches_virgo(self, tmp_path: Path) -> None:
        npz = _write_capture(tmp_path / "capture.npz")
        out = export_virgo_csv(npz, tmp_path / "capture-virgo.csv")
        lines = out.read_text().splitlines()
        assert len(lines) == N_FFT  # no header row (Virgo writes none)
        first = lines[0].split(",")
        assert len(first) == 2  # uncalibrated layout: frequency, avg_spectrum
        float(first[0]), float(first[1])  # both numeric, '%1.6f'
        assert all("." in field for field in first)
        # Column 0 is MHz starting at the band's low edge (Virgo's linspace).
        assert float(first[0]) == pytest.approx((CENTER_HZ - RATE_HZ / 2) / 1e6)

    def test_overwrites_on_reexport(self, tmp_path: Path) -> None:
        npz = _write_capture(tmp_path / "capture.npz")
        out = tmp_path / "capture-virgo.csv"
        export_virgo_csv(npz, out)
        assert export_virgo_csv(npz, out) == out
        assert np.loadtxt(out, delimiter=",").shape == (N_FFT, 2)


class TestEzraTxt:
    def _export(self, tmp_path: Path) -> tuple[Path, Path]:
        npz = _write_capture(tmp_path / "capture.npz")
        out = export_ezra_txt(
            npz,
            tmp_path / "exports" / "capture-ezra.txt",
            lat=LAT,
            lon=LON,
            elev=ELEV_M,
            azimuth_deg=AZ,
            elevation_deg=EL,
            name="Discovery Dish",
        )
        return npz, out

    def test_header_structure(self, tmp_path: Path) -> None:
        npz, out = self._export(tmp_path)
        lines = out.read_text().splitlines()

        # Line 1: ezCon skips any file whose first 10 chars are not 'from ezCol'.
        assert lines[0][:10] == "from ezCol"

        # Line 2: split fields [1]/[3]/[5] are lat/long/amsl; name = fields[7:].
        fields = lines[1].split()
        assert fields[0] == "lat" and float(fields[1]) == pytest.approx(LAT)
        assert fields[2] == "long" and float(fields[3]) == pytest.approx(LON)
        assert fields[4] == "amsl" and float(fields[5]) == pytest.approx(ELEV_M)
        assert fields[6] == "name" and " ".join(fields[7:]) == "Discovery Dish"

        # Line 3: freqMin/freqMax in MHz, freqBinQty channels.
        fields = lines[2].split()
        freq_hz, _ = averaged_spectrum(npz)
        assert fields[0] == "freqMin" and float(fields[1]) == pytest.approx(freq_hz[0] / 1e6)
        assert fields[2] == "freqMax" and float(fields[3]) == pytest.approx(freq_hz[-1] / 1e6)
        assert fields[4] == "freqBinQty" and int(fields[5]) == N_FFT

        # Pointing line, then comments.
        fields = lines[3].split()
        assert fields[0] == "az" and float(fields[1]) == pytest.approx(AZ)
        assert fields[2] == "el" and float(fields[3]) == pytest.approx(EL)
        assert lines[4].startswith("#")

    def test_data_line_sample_count_and_values(self, tmp_path: Path) -> None:
        npz, out = self._export(tmp_path)
        data_lines = [
            line
            for line in out.read_text().splitlines()[3:]
            if line.strip() and not line.startswith("#") and not line.startswith("az ")
        ]
        assert len(data_lines) == 1  # one averaged spectrum → one sample
        fields = data_lines[0].split()

        # Timestamp is the capture's first frame time, FITS/ezCol format.
        stamp = datetime.strptime(fields[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        assert stamp == datetime.fromtimestamp(T0, tz=UTC)

        # freqBinQty linear power values (ezCon reads fields[1:freqBinQty+1]).
        values = np.array([float(v) for v in fields[1 : N_FFT + 1]])
        assert values.size == N_FFT
        _, power_db = averaged_spectrum(npz)
        np.testing.assert_allclose(values, db_to_linear(power_db), rtol=1e-6)
        assert (values > 0).all()  # linear power, not dB

    def test_ezcon_would_accept_the_file(self, tmp_path: Path) -> None:
        """Mimic ezCon's header walk: skip comments/blanks, parse 3 header lines."""
        _, out = self._export(tmp_path)
        lines = iter(out.read_text().splitlines())

        def next_noncomment() -> str:
            for line in lines:
                if line.split() and not line.split()[0].startswith("#"):
                    return line
            raise AssertionError("ran out of lines")

        line1 = next_noncomment()
        assert len(line1) >= 10 and line1[:10] == "from ezCol"
        line2 = next_noncomment().split()
        float(line2[1]), float(line2[3]), float(line2[5])
        line3 = next_noncomment().split()
        assert int(line3[5]) == N_FFT

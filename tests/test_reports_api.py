"""Tests for the reports router: PDF build/serve round trip + capture exports.

Same fixture pattern as test_captures_api: a seeded tmp database, real
``.npz`` captures from synthetic fake-HI IQ, a TestClient over the real
app. WeasyPrint font noise is tolerated (logging silenced, nothing
asserted on stderr).
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from jansky.signals import rng
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe import synthetic
from jansky_observe.capture.dsp import welch_psd_db
from jansky_observe.capture.writer import NpzCaptureWriter, SigmfCaptureWriter
from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.frames import SpectralFrame
from jansky_observe.models import Capture, Observation, ObservationType, RadioSource
from jansky_observe.server.app import create_app
from jansky_observe.server.routers import reports

DEAD_ENDPOINT = "tcp://127.0.0.1:1"
CENTER_HZ, RATE_HZ = 1420.4e6, 3e6
WHEN = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _quiet_weasyprint() -> Iterator[None]:
    """Silence WeasyPrint/fontTools font warnings; never assert on stderr."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        levels = {}
        for name in ("weasyprint", "fontTools", "fontTools.subset"):
            logger = logging.getLogger(name)
            levels[name] = logger.level
            logger.setLevel(logging.ERROR)
        try:
            yield
        finally:
            for name, level in levels.items():
                logging.getLogger(name).setLevel(level)


@pytest.fixture()
def engine(tmp_path) -> Engine:
    """A fully migrated + seeded database in a tmp dir."""
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    """A TestClient over an app whose data_dir is the tmp dir.

    The reports router is included here if app wiring hasn't picked it up
    yet (the include lives in server/app.py, owned by the M4 integration).
    """
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    app = create_app(settings, engine=engine)
    paths = {getattr(route, "path", None) for route in app.routes}
    if "/observations/{obs_id}/report.pdf" not in paths:
        app.include_router(reports.router)
    return TestClient(app)


def _write_capture(path: Path, n_frames: int = 8, n_fft: int = 256) -> Path:
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
                timestamp=1_750_000_000.0 + i * 0.5,
                center_freq_hz=CENTER_HZ,
                sample_rate_hz=RATE_HZ,
                power_db=welch_psd_db(iq, RATE_HZ, n_fft),
            )
        )
    return writer.close()


def _observation(engine: Engine, *, pointing: bool = True) -> int:
    """A done observation on the seeded Cygnus HI source."""
    with Session(engine) as session:
        obs_type = session.exec(select(ObservationType)).first()
        source = session.exec(
            select(RadioSource).where(RadioSource.name == "Cygnus region HI")
        ).one()
        assert obs_type is not None and obs_type.id is not None and source.id is not None
        observation = Observation(
            name="reports test",
            observation_type_id=obs_type.id,
            station_id=1,
            location_id=1,
            source_id=source.id,
            status="done",
            actual_start=WHEN,
            actual_end=WHEN.replace(hour=2),
            pointing_az_deg=123.0 if pointing else None,
            pointing_el_deg=45.0 if pointing else None,
        )
        session.add(observation)
        session.commit()
        session.refresh(observation)
        assert observation.id is not None
        return observation.id


def _add_capture(
    engine: Engine, path: Path, fmt: str = "npz_spectra", observation_id: int | None = None
) -> int:
    with Session(engine) as session:
        capture = Capture(
            observation_id=observation_id,
            device="synthetic",
            path=str(path),
            format=fmt,
            size_bytes=path.stat().st_size if path.exists() else 0,
            start=WHEN,
            sdr_settings={"gain": 15},
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        assert capture.id is not None
        return capture.id


# ---- report endpoints ---------------------------------------------------------------


def test_report_pdf_404_until_built(client: TestClient, engine: Engine) -> None:
    obs_id = _observation(engine)
    resp = client.get(f"/observations/{obs_id}/report.pdf")
    assert resp.status_code == 404
    assert "no report built" in resp.json()["detail"]


def test_api_build_then_download_round_trip(client: TestClient, engine: Engine, tmp_path) -> None:
    obs_id = _observation(engine)
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    _add_capture(engine, npz, observation_id=obs_id)

    resp = client.post(f"/api/observations/{obs_id}/report")
    assert resp.status_code == 200
    path = Path(resp.json()["path"])
    assert path == tmp_path / "observations" / str(obs_id) / "report.pdf"
    assert path.is_file()

    resp = client.get(f"/observations/{obs_id}/report.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    assert len(resp.content) > 20_000


def test_html_build_htmx_fragment(client: TestClient, engine: Engine) -> None:
    obs_id = _observation(engine)
    resp = client.post(f"/observations/{obs_id}/report", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert f"/observations/{obs_id}/report.pdf" in resp.text


def test_html_build_plain_post_redirects_to_pdf(client: TestClient, engine: Engine) -> None:
    obs_id = _observation(engine)
    resp = client.post(f"/observations/{obs_id}/report", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/observations/{obs_id}/report.pdf"
    assert client.get(resp.headers["location"]).status_code == 200


def test_report_unknown_observation_404(client: TestClient) -> None:
    assert client.post("/api/observations/9999/report").status_code == 404
    assert client.get("/observations/9999/report.pdf").status_code == 404


# ---- capture export endpoint --------------------------------------------------------


def test_export_virgo_csv_download(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    capture_id = _add_capture(engine, npz)

    resp = client.get(f"/api/captures/{capture_id}/export", params={"format": "virgo_csv"})
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert f"capture-{capture_id}-virgo.csv" in disposition
    assert (tmp_path / "exports" / f"capture-{capture_id}-virgo.csv").is_file()

    rows = [line.split(",") for line in resp.text.splitlines()]
    assert len(rows) == 256
    assert all(len(row) == 2 for row in rows)
    freqs = np.array([float(row[0]) for row in rows])
    assert freqs[0] == pytest.approx((CENTER_HZ - RATE_HZ / 2) / 1e6)


def test_export_ezra_txt_with_pointing(client: TestClient, engine: Engine, tmp_path) -> None:
    obs_id = _observation(engine, pointing=True)
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    capture_id = _add_capture(engine, npz, observation_id=obs_id)

    resp = client.get(f"/api/captures/{capture_id}/export", params={"format": "ezra_txt"})
    assert resp.status_code == 200
    assert f"capture-{capture_id}-ezra.txt" in resp.headers["content-disposition"]
    lines = resp.text.splitlines()
    assert lines[0].startswith("from ezCol")
    assert lines[1].split()[0] == "lat"
    az_line = lines[3].split()
    assert az_line[0] == "az" and float(az_line[1]) == pytest.approx(123.0)
    assert az_line[2] == "el" and float(az_line[3]) == pytest.approx(45.0)


def test_export_ezra_409_without_observation(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    capture_id = _add_capture(engine, npz)
    resp = client.get(f"/api/captures/{capture_id}/export", params={"format": "ezra_txt"})
    assert resp.status_code == 409
    assert "linked observation" in resp.json()["detail"]


def test_export_ezra_409_without_pointing(client: TestClient, engine: Engine, tmp_path) -> None:
    obs_id = _observation(engine, pointing=False)
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    capture_id = _add_capture(engine, npz, observation_id=obs_id)
    resp = client.get(f"/api/captures/{capture_id}/export", params={"format": "ezra_txt"})
    assert resp.status_code == 409
    assert "az/el" in resp.json()["detail"]


def test_export_sigmf_409(client: TestClient, engine: Engine, tmp_path) -> None:
    writer = SigmfCaptureWriter(
        tmp_path / "captures" / "c", sample_rate_hz=RATE_HZ, center_freq_hz=CENTER_HZ
    )
    writer.write(np.zeros(64, dtype=np.int16))
    writer.close()
    capture_id = _add_capture(engine, tmp_path / "captures" / "c.sigmf-data", fmt="sigmf")
    resp = client.get(f"/api/captures/{capture_id}/export", params={"format": "virgo_csv"})
    assert resp.status_code == 409
    assert "npz_spectra only" in resp.json()["detail"]


def test_export_missing_file_404(client: TestClient, engine: Engine, tmp_path) -> None:
    capture_id = _add_capture(engine, tmp_path / "captures" / "gone.npz")
    resp = client.get(f"/api/captures/{capture_id}/export", params={"format": "virgo_csv"})
    assert resp.status_code == 404


def test_export_bad_format_422(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    capture_id = _add_capture(engine, npz)
    resp = client.get(f"/api/captures/{capture_id}/export", params={"format": "fits"})
    assert resp.status_code == 422

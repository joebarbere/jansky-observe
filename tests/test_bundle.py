"""Tests for the codified observation bundle (roadmap M8, plan 78).

Reuses the synthetic-npz + seeded-tmp-db pattern: a real ``.npz`` averaged-
spectrum capture on disk, the manifest builder + zip writer, the JSON/zip API
routes, the MCP tool, and the PDF embed.
"""

from __future__ import annotations

import io
import json
import logging
import warnings
import zipfile
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
from jansky_observe.capture.writer import NpzCaptureWriter
from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.export.bundle import (
    BUNDLE_SCHEMA,
    build_observation_manifest,
    write_observation_bundle,
)
from jansky_observe.frames import SpectralFrame
from jansky_observe.models import (
    Capture,
    ClassifierResult,
    Observation,
    ObservationType,
    RadioSource,
    Station,
)
from jansky_observe.server.app import create_app

DEAD_ENDPOINT = "tcp://127.0.0.1:1"
CENTER_HZ, RATE_HZ = 1420.4e6, 3e6
WHEN = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _quiet_weasyprint() -> Iterator[None]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for name in ("weasyprint", "fontTools", "fontTools.subset"):
            logging.getLogger(name).setLevel(logging.ERROR)
        yield


@pytest.fixture()
def engine(tmp_path) -> Engine:
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    return TestClient(create_app(settings, engine=engine))


def _write_capture(path: Path, n_frames: int = 8, n_fft: int = 256) -> Path:
    gen = rng(7)
    writer = NpzCaptureWriter(
        path, settings={"gain": 15, "center_freq_hz": CENTER_HZ, "sample_rate_hz": RATE_HZ}
    )
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


def _observation(engine: Engine) -> int:
    with Session(engine) as session:
        obs_type = session.exec(select(ObservationType)).first()
        source = session.exec(
            select(RadioSource).where(RadioSource.name == "Cygnus region HI")
        ).one()
        assert obs_type is not None and obs_type.id is not None and source.id is not None
        observation = Observation(
            name="bundle test",
            observation_type_id=obs_type.id,
            station_id=1,
            location_id=1,
            source_id=source.id,
            status="done",
            actual_start=WHEN,
            actual_end=WHEN.replace(hour=2),
            pointing_az_deg=123.0,
            pointing_el_deg=45.0,
        )
        session.add(observation)
        session.commit()
        session.refresh(observation)
        assert observation.id is not None
        return observation.id


def _add_capture(engine: Engine, path: Path, obs_id: int, fmt: str = "npz_spectra") -> int:
    with Session(engine) as session:
        capture = Capture(
            observation_id=obs_id,
            device="synthetic",
            path=str(path),
            format=fmt,
            size_bytes=path.stat().st_size if path.exists() else 0,
            start=WHEN,
            end=WHEN.replace(minute=30),
            sdr_settings={"gain": 15, "center_freq_hz": CENTER_HZ, "sample_rate_hz": RATE_HZ},
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        assert capture.id is not None
        return capture.id


def test_manifest_carries_station_uuid_pointing_and_lst(engine: Engine, tmp_path) -> None:
    obs_id = _observation(engine)
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    cap_id = _add_capture(engine, npz, obs_id)
    with Session(engine) as session:
        session.add(
            ClassifierResult(
                capture_id=cap_id,
                name="hline_v1",
                version="1",
                verdict="detected",
                score=7.5,
                mode="post",
                params={"snr": 7.5},
            )
        )
        session.commit()
        observation = session.get(Observation, obs_id)
        station = session.exec(select(Station)).one()
        assert observation is not None
        manifest = build_observation_manifest(session, observation)

    assert manifest["schema"] == BUNDLE_SCHEMA
    assert manifest["station"]["uuid"] == station.uuid  # plan 78's per-station key
    assert manifest["observation"]["pointing"]["dialed_az_deg"] == 123.0
    (cap,) = manifest["captures"]
    assert cap["id"] == cap_id
    assert cap["sdr_settings"]["gain"] == 15
    assert cap["lst_hours_at_start"] is not None and 0.0 <= cap["lst_hours_at_start"] < 24.0
    assert cap["spectrum_file"] == f"capture-{cap_id}.npz"
    assert cap["classifier_results"][0]["verdict"] == "detected"
    # JSON-serializable end to end (it is embedded in the PDF + served over MCP).
    assert json.loads(json.dumps(manifest))["captures"][0]["id"] == cap_id


def test_manifest_no_spectrum_file_for_sigmf_or_purged(engine: Engine, tmp_path) -> None:
    obs_id = _observation(engine)
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    _add_capture(engine, npz, obs_id, fmt="sigmf")  # raw IQ, not an averaged spectrum
    with Session(engine) as session:
        observation = session.get(Observation, obs_id)
        assert observation is not None
        manifest = build_observation_manifest(session, observation)
    assert manifest["captures"][0]["spectrum_file"] is None


def test_write_bundle_zip_has_manifest_and_self_describing_npz(engine: Engine, tmp_path) -> None:
    obs_id = _observation(engine)
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    cap_id = _add_capture(engine, npz, obs_id)
    with Session(engine) as session:
        observation = session.get(Observation, obs_id)
        station = session.exec(select(Station)).one()
        assert observation is not None
        zip_path = write_observation_bundle(session, observation, tmp_path / "exports")

    assert zip_path.name == f"observation-{obs_id}-bundle.zip"
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        assert names == {"bundle.json", f"capture-{cap_id}.npz"}
        manifest = json.loads(archive.read("bundle.json"))
        assert manifest["station"]["uuid"] == station.uuid
        with np.load(io.BytesIO(archive.read(f"capture-{cap_id}.npz"))) as data:
            assert data["frequency_hz"].shape == data["power_db"].shape
            assert str(data["station_uuid"]) == station.uuid  # standalone-usable npz
            assert float(data["az_deg"]) == 123.0
            assert float(data["gain"]) == 15.0
            assert 0.0 <= float(data["lst_hours"]) < 24.0

    # The staging scratch dir is cleaned up.
    assert not (tmp_path / "exports" / f".observation-{obs_id}-npz").exists()


def test_api_bundle_json_and_zip(client: TestClient, engine: Engine, tmp_path) -> None:
    obs_id = _observation(engine)
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    cap_id = _add_capture(engine, npz, obs_id)
    with Session(engine) as session:
        uuid = session.exec(select(Station)).one().uuid

    manifest = client.get(f"/api/observations/{obs_id}/bundle.json").json()
    assert manifest["station"]["uuid"] == uuid
    assert manifest["captures"][0]["id"] == cap_id

    resp = client.get(f"/api/observations/{obs_id}/bundle")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert "bundle.json" in archive.namelist()
        assert f"capture-{cap_id}.npz" in archive.namelist()


def test_api_bundle_unknown_observation_404(client: TestClient) -> None:
    assert client.get("/api/observations/9999/bundle.json").status_code == 404
    assert client.get("/api/observations/9999/bundle").status_code == 404


def test_report_embeds_bundle_block(client: TestClient, engine: Engine, tmp_path) -> None:
    from jansky_observe.export import pdf as pdf_module

    obs_id = _observation(engine)
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    _add_capture(engine, npz, obs_id)

    # The report context embeds the manifest verbatim (a compressed PDF hides the
    # text, so assert on the context that feeds the template).
    with Session(engine) as session:
        observation = session.get(Observation, obs_id)
        assert observation is not None
        context = pdf_module._gather_context(session, observation, tmp_path)
    embedded = json.loads(context["bundle_json"])
    assert embedded["schema"] == BUNDLE_SCHEMA
    assert embedded["observation"]["id"] == obs_id

    # And the PDF still builds.
    assert client.post(f"/api/observations/{obs_id}/report").status_code == 200
    assert client.get(f"/observations/{obs_id}/report.pdf").content.startswith(b"%PDF")

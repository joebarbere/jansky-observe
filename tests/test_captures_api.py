"""Tests for the captures API: stop-registration, import, spectra, classify, plots.

Fixtures follow the /synthetic-fixture pattern: real ``.npz`` captures written
by the real writer from synthetic fake-HI IQ, a fake ZMQ REP daemon for the
stop handshake — no hardware, no network.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import zmq
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
from jansky_observe.models import (
    Capture,
    ClassifierResult,
    Observation,
    ObservationType,
    RadioSource,
    utcnow,
)
from jansky_observe.server.app import create_app

DEAD_ENDPOINT = "tcp://127.0.0.1:1"
CENTER_HZ, RATE_HZ = 1420.4e6, 3e6
CAPTURE_SETTINGS = {"gain": 15, "source": "synthetic"}
WHEN = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)


def _write_capture(path: Path, n_frames: int = 8, n_fft: int = 256) -> Path:
    """Write an .npz capture of synthetic HI frames via the real writer."""
    gen = rng(7)
    writer = NpzCaptureWriter(path, settings=CAPTURE_SETTINGS)
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


def _write_sigmf_pair(base: Path) -> Path:
    """Write a tiny SigMF data+meta pair via the real writer; returns the data path."""
    writer = SigmfCaptureWriter(
        base, sample_rate_hz=RATE_HZ, center_freq_hz=CENTER_HZ, settings=CAPTURE_SETTINGS
    )
    writer.write(np.zeros(64, dtype=np.int16))
    writer.close()
    return base.with_suffix(".sigmf-data")


@pytest.fixture()
def engine(tmp_path) -> Engine:
    """A fully migrated + seeded database in a tmp dir."""
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    """A TestClient over an app whose data_dir is the tmp dir."""
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    return TestClient(create_app(settings, engine=engine))


def _add_capture(
    engine: Engine,
    path: Path,
    fmt: str = "npz_spectra",
    observation_id: int | None = None,
    start: datetime | None = None,
) -> int:
    with Session(engine) as session:
        capture = Capture(
            observation_id=observation_id,
            device="synthetic",
            path=str(path),
            format=fmt,
            size_bytes=path.stat().st_size if path.exists() else 0,
            start=start,
            sdr_settings=CAPTURE_SETTINGS,
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        assert capture.id is not None
        return capture.id


def _running_observation(engine: Engine, source_name: str = "Cygnus region HI") -> int:
    """Create a status='running' observation directly via the models."""
    with Session(engine) as session:
        obs_type = session.exec(select(ObservationType)).first()
        source = session.exec(select(RadioSource).where(RadioSource.name == source_name)).one()
        assert obs_type is not None and obs_type.id is not None and source.id is not None
        observation = Observation(
            name="captures test",
            observation_type_id=obs_type.id,
            station_id=1,
            location_id=1,
            source_id=source.id,
            status="running",
            actual_start=utcnow(),
        )
        session.add(observation)
        session.commit()
        session.refresh(observation)
        assert observation.id is not None
        return observation.id


# ---- fake REP daemon (pattern from test_server.py) --------------------------------


@contextlib.contextmanager
def _fake_daemon(
    handler: Callable[[dict[str, Any]], dict[str, Any]],
) -> Iterator[str]:
    """A plain ZMQ REP socket answering control requests with ``handler(request)``."""
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind("tcp://127.0.0.1:*")
    endpoint = sock.getsockopt_string(zmq.LAST_ENDPOINT)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            if sock.poll(50, zmq.POLLIN):
                request = json.loads(sock.recv())
                sock.send(json.dumps(handler(request)).encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield endpoint
    finally:
        stop.set()
        thread.join(timeout=2.0)
        sock.close(0)
        ctx.term()


def _stopped_status(path: Path, fmt: str = "npz", **overrides: Any) -> dict[str, Any]:
    """The daemon's stop reply: the final status of the capture just stopped."""
    reply: dict[str, Any] = {
        "ok": True,
        "capturing": True,
        "format": fmt,
        "path": str(path),
        "bytes_written": path.stat().st_size,
        "elapsed_s": 4.0,
        "rate_bytes_per_s": 1000.0,
        "disk_free_bytes": 50_000_000_000,
        "source": "synthetic",
        "overrun": False,
    }
    reply.update(overrides)
    return reply


# ---- capture stop registration ----------------------------------------------------


def test_capture_stop_registers_npz_capture(engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    obs_id = _running_observation(engine)
    with _fake_daemon(lambda req: _stopped_status(npz)) as endpoint:
        settings = Settings(
            zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint, data_dir=str(tmp_path)
        )
        client = TestClient(create_app(settings, engine=engine))
        resp = client.post("/api/capture/stop")
    assert resp.status_code == 200
    capture_id = resp.json()["capture_id"]
    assert isinstance(capture_id, int)
    with Session(engine) as session:
        capture = session.get(Capture, capture_id)
        assert capture is not None
        assert capture.format == "npz_spectra"
        assert capture.device == "synthetic"
        assert capture.path == str(npz)
        assert capture.size_bytes == npz.stat().st_size
        assert capture.observation_id == obs_id
        # Settings parsed from inside the .npz itself (reproducibility rule).
        assert capture.sdr_settings["gain"] == 15
        assert "software_version" in capture.sdr_settings
        assert capture.start is not None and capture.end is not None
        assert (capture.end - capture.start).total_seconds() == pytest.approx(4.0, abs=0.5)


def test_capture_stop_sigmf_parses_meta_and_needs_no_observation(engine: Engine, tmp_path) -> None:
    data_path = _write_sigmf_pair(tmp_path / "captures" / "c")
    status = _stopped_status(data_path, fmt="sigmf")
    with _fake_daemon(lambda req: status) as endpoint:
        settings = Settings(
            zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint, data_dir=str(tmp_path)
        )
        client = TestClient(create_app(settings, engine=engine))
        resp = client.post("/api/capture/stop")
    assert resp.status_code == 200
    with Session(engine) as session:
        capture = session.get(Capture, resp.json()["capture_id"])
        assert capture is not None
        assert capture.format == "sigmf"
        assert capture.observation_id is None  # nothing running
        assert capture.sdr_settings["gain"] == 15  # from <base>.sigmf-meta


def test_capture_stop_tolerates_unparseable_settings(engine: Engine, tmp_path) -> None:
    bogus = tmp_path / "captures" / "bogus.npz"
    bogus.parent.mkdir(parents=True)
    bogus.write_bytes(b"not an npz")
    with _fake_daemon(lambda req: _stopped_status(bogus)) as endpoint:
        settings = Settings(
            zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint, data_dir=str(tmp_path)
        )
        client = TestClient(create_app(settings, engine=engine))
        resp = client.post("/api/capture/stop")
    assert resp.status_code == 200
    with Session(engine) as session:
        capture = session.get(Capture, resp.json()["capture_id"])
        assert capture is not None
        assert capture.sdr_settings == {}


def test_capture_stop_idle_reply_registers_nothing(engine: Engine, tmp_path) -> None:
    idle = {
        "ok": True,
        "capturing": False,
        "format": None,
        "path": None,
        "bytes_written": 0,
        "elapsed_s": 0.0,
        "rate_bytes_per_s": 0.0,
        "disk_free_bytes": 1,
        "source": "synthetic",
    }
    with _fake_daemon(lambda req: idle) as endpoint:
        settings = Settings(
            zmq_endpoint=DEAD_ENDPOINT, ctl_endpoint=endpoint, data_dir=str(tmp_path)
        )
        client = TestClient(create_app(settings, engine=engine))
        resp = client.post("/api/capture/stop")
    assert resp.status_code == 200
    assert resp.json()["capture_id"] is None
    with Session(engine) as session:
        assert session.exec(select(Capture)).all() == []


# ---- import ------------------------------------------------------------------------


def test_import_registers_loose_files_once(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "loose.npz")
    data_path = _write_sigmf_pair(tmp_path / "captures" / "loose-iq")
    resp = client.post("/api/captures/import")
    assert resp.status_code == 200
    assert resp.json() == {"imported": 2}
    assert client.post("/api/captures/import").json() == {"imported": 0}  # idempotent

    rows = client.get("/api/captures").json()
    assert len(rows) == 2
    by_path = {row["path"]: row for row in rows}
    assert by_path[str(npz)]["format"] == "npz_spectra"
    assert by_path[str(data_path)]["format"] == "sigmf"
    assert all(row["observation_id"] is None for row in rows)
    assert all(row["device"] == "synthetic" for row in rows)  # from parsed settings


def test_captures_list_newest_first(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    first = _add_capture(engine, npz)
    second = _add_capture(engine, npz)
    rows = client.get("/api/captures").json()
    assert [row["id"] for row in rows] == [second, first]


def test_capture_meta(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    capture_id = _add_capture(engine, npz)
    body = client.get(f"/api/captures/{capture_id}").json()
    assert body["path"] == str(npz)
    assert body["sdr_settings"]["gain"] == 15
    assert client.get("/api/captures/9999").status_code == 404


# ---- spectrum ----------------------------------------------------------------------


def test_spectrum_mhz(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz", n_fft=256)
    capture_id = _add_capture(engine, npz)
    body = client.get(f"/api/captures/{capture_id}/spectrum").json()
    assert body["axis_kind"] == "mhz"
    assert len(body["axis"]) == len(body["power_db"]) == 256
    assert body["axis"][0] == pytest.approx((CENTER_HZ - RATE_HZ / 2) / 1e6)
    assert all(np.isfinite(body["power_db"]))


def test_spectrum_vlsr_with_linked_observation(
    client: TestClient, engine: Engine, tmp_path
) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz", n_fft=256)
    obs_id = _running_observation(engine)
    capture_id = _add_capture(engine, npz, observation_id=obs_id, start=WHEN)
    body = client.get(f"/api/captures/{capture_id}/spectrum", params={"axis": "vlsr"}).json()
    assert body["axis_kind"] == "vlsr"
    axis = np.asarray(body["axis"])
    assert axis.shape == (256,)
    assert np.all(np.diff(axis) < 0)  # v_LSR decreases with frequency
    assert axis.max() > 0 > axis.min()  # the band straddles the HI line


def test_spectrum_vlsr_without_observation_409(
    client: TestClient, engine: Engine, tmp_path
) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    capture_id = _add_capture(engine, npz)
    resp = client.get(f"/api/captures/{capture_id}/spectrum", params={"axis": "vlsr"})
    assert resp.status_code == 409
    assert "linked observation" in resp.json()["detail"]


def test_spectrum_sigmf_409(client: TestClient, engine: Engine, tmp_path) -> None:
    data_path = _write_sigmf_pair(tmp_path / "captures" / "c")
    capture_id = _add_capture(engine, data_path, fmt="sigmf")
    resp = client.get(f"/api/captures/{capture_id}/spectrum")
    assert resp.status_code == 409
    assert "sigmf" in resp.json()["detail"]


def test_spectrum_bad_axis_422(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    capture_id = _add_capture(engine, npz)
    assert (
        client.get(f"/api/captures/{capture_id}/spectrum", params={"axis": "ghz"}).status_code
        == 422
    )


# ---- classify ----------------------------------------------------------------------


def test_classify_creates_result_and_plot(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    capture_id = _add_capture(engine, npz)
    resp = client.post(f"/api/captures/{capture_id}/classify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "detected"
    assert body["score"] >= 5.0
    assert body["name"] == "hline_v1" and body["version"] == "1"
    assert body["mode"] == "post"
    assert body["params"]["window_source"] == "fixed"
    plot = Path(body["plot_path"])
    assert plot == tmp_path / "plots" / f"capture-{capture_id}-hline_v1.png"
    assert plot.is_file() and plot.stat().st_size > 0

    with Session(engine) as session:
        rows = session.exec(select(ClassifierResult)).all()
        assert len(rows) == 1
        assert rows[0].capture_id == capture_id

    # A second run appends a second row (every run is a provenance record).
    assert client.post(f"/api/captures/{capture_id}/classify").status_code == 200
    results = client.get(f"/api/captures/{capture_id}/results").json()
    assert len(results) == 2
    assert all(r["verdict"] == "detected" for r in results)


def test_classify_uses_lsr_window_when_linked(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    obs_id = _running_observation(engine)
    capture_id = _add_capture(engine, npz, observation_id=obs_id, start=WHEN)
    body = client.post(f"/api/captures/{capture_id}/classify").json()
    assert body["params"]["window_source"] == "lsr"
    assert body["verdict"] == "detected"


def test_classify_sigmf_409(client: TestClient, engine: Engine, tmp_path) -> None:
    data_path = _write_sigmf_pair(tmp_path / "captures" / "c")
    capture_id = _add_capture(engine, data_path, fmt="sigmf")
    assert client.post(f"/api/captures/{capture_id}/classify").status_code == 409


def test_classify_missing_file_404(client: TestClient, engine: Engine, tmp_path) -> None:
    capture_id = _add_capture(engine, tmp_path / "captures" / "gone.npz")
    assert client.post(f"/api/captures/{capture_id}/classify").status_code == 404


# ---- plot + detail-page UI ----------------------------------------------------------


def test_plot_route_serves_png_after_classify(client: TestClient, engine: Engine, tmp_path) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    capture_id = _add_capture(engine, npz)
    assert client.get(f"/api/captures/{capture_id}/plot").status_code == 404  # not rendered yet
    client.post(f"/api/captures/{capture_id}/classify")
    resp = client.get(f"/api/captures/{capture_id}/plot")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


def test_detail_page_shows_classify_button_and_verdicts(
    client: TestClient, engine: Engine, tmp_path
) -> None:
    npz = _write_capture(tmp_path / "captures" / "c.npz")
    obs_id = _running_observation(engine)
    capture_id = _add_capture(engine, npz, observation_id=obs_id, start=WHEN)

    body = client.get(f"/observations/{obs_id}").text
    assert "Classify" in body
    assert f"/captures/{capture_id}/classify" in body

    # The htmx fragment route classifies and renders the verdict inline.
    fragment = client.post(f"/captures/{capture_id}/classify")
    assert fragment.status_code == 200
    assert "detected" in fragment.text
    assert f"/api/captures/{capture_id}/plot" in fragment.text

    body = client.get(f"/observations/{obs_id}").text
    assert "detected" in body  # existing results render inline on reload

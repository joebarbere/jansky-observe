"""Reference-model overlay (roadmap M12): figure + capture routes (fetch mocked)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.astro.hi_reference import ReferenceProfile
from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ
from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.export.figures import profile_overlay_figure
from jansky_observe.models import Capture, Location, Observation, RadioSource
from jansky_observe.server.app import create_app
from jansky_observe.server.routers import captures as captures_router

DEAD_ENDPOINT = "tcp://127.0.0.1:1"
RATE_HZ = 6.0e6

_MODEL = ReferenceProfile(
    v_lsr_kms=np.array([-50.0, 0.0, 50.0]),
    t_b_k=np.array([1.0, 45.0, 1.0]),
    source="LAB",
    l_deg=90.0,
    b_deg=0.0,
)


@pytest.fixture()
def engine(tmp_path) -> Engine:
    return init_db(tmp_path / "data")


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path / "data")), engine=engine
        )
    )


def test_overlay_figure_renders(tmp_path) -> None:
    out = profile_overlay_figure(
        np.linspace(-200, 200, 128),
        np.full(128, -40.0),
        _MODEL.v_lsr_kms,
        _MODEL.t_b_k,
        tmp_path / "ov.png",
        title="t",
    )
    assert out.exists() and out.stat().st_size > 0


def _write_npz(path: Path, *, n_fft: int = 256, n_frames: int = 4) -> Path:
    power_db = np.tile(np.full(n_fft, -40.0), (n_frames, 1)).astype(np.float64)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        power_db=power_db,
        center_freq_hz=HI_LINE_FREQ_HZ,
        sample_rate_hz=RATE_HZ,
        timestamps=np.arange(n_frames, dtype=np.float64),
        settings=json.dumps({"gain": 15}),
    )
    return path


def _capture_with_observation(engine: Engine, npz: Path) -> int:
    with Session(engine) as session:
        source = session.exec(select(RadioSource)).first()
        location = session.exec(select(Location)).first()
        obs = Observation(
            name="m12 overlay",
            observation_type_id=1,
            station_id=1,
            location_id=location.id,
            source_id=source.id,
            status="done",
            actual_start=datetime(2026, 7, 1, 3, 0, tzinfo=UTC),
        )
        session.add(obs)
        session.commit()
        session.refresh(obs)
        capture = Capture(
            observation_id=obs.id,
            device="synthetic",
            path=str(npz),
            format="npz_spectra",
            size_bytes=npz.stat().st_size,
            start=datetime(2026, 7, 1, 3, 0, tzinfo=UTC),
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        assert capture.id is not None
        return capture.id


def test_overlay_available_when_model_returned(
    client: TestClient, engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(captures_router, "reference_profile", lambda *a, **k: _MODEL)
    cap_id = _capture_with_observation(engine, _write_npz(tmp_path / "c.npz"))
    body = client.get(f"/api/captures/{cap_id}/overlay").json()
    assert body["available"] is True
    assert body["model"]["source"] == "LAB" and body["model"]["peak_t_b_k"] == 45.0
    assert len(body["observed"]["v_lsr_kms"]) == len(body["observed"]["power_db"]) == 256
    resp = client.get(f"/api/captures/{cap_id}/overlay.png")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"


def test_overlay_unavailable_when_no_model(
    client: TestClient, engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(captures_router, "reference_profile", lambda *a, **k: None)
    cap_id = _capture_with_observation(engine, _write_npz(tmp_path / "c.npz"))
    body = client.get(f"/api/captures/{cap_id}/overlay").json()
    assert body["available"] is False
    assert "observed" in body  # observed spectrum still returned for context
    assert client.get(f"/api/captures/{cap_id}/overlay.png").status_code == 409


def test_overlay_unavailable_without_pointing(client: TestClient, engine: Engine, tmp_path) -> None:
    # A capture with no linked observation → no galactic direction → unavailable.
    with Session(engine) as session:
        capture = Capture(
            device="synthetic",
            path=str(_write_npz(tmp_path / "c.npz")),
            format="npz_spectra",
            start=datetime(2026, 7, 1, 3, 0, tzinfo=UTC),
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        cap_id = capture.id
    body = client.get(f"/api/captures/{cap_id}/overlay").json()
    assert body["available"] is False and "pointing" in body["reason"]


# ---- calibrated overlay (plans/calibrated-overlay.md) ----------------------------


def _cal_epoch(engine: Engine, tsys_k: float | None) -> int:
    from jansky_observe.models import CalibrationEpoch

    with Session(engine) as session:
        epoch = CalibrationEpoch(started_at=datetime(2026, 7, 1, 2, 0, tzinfo=UTC), tsys_k=tsys_k)
        session.add(epoch)
        session.commit()
        session.refresh(epoch)
        assert epoch.id is not None
        return epoch.id


def _stamp_epoch(engine: Engine, capture_id: int, epoch_id: int) -> None:
    with Session(engine) as session:
        capture = session.get(Capture, capture_id)
        assert capture is not None
        capture.cal_epoch_id = epoch_id
        session.add(capture)
        session.commit()


def test_calibrated_figure_with_residual_renders(tmp_path) -> None:
    from jansky_observe.confirm.overlay import compare_to_model

    v = np.linspace(-200, 200, 256)
    model = 45.0 * np.exp(-0.5 * (v / 25.0) ** 2)
    comparison = compare_to_model(v, model * 0.65, v, model)
    assert comparison is not None

    out = profile_overlay_figure(
        v,
        np.full(256, -40.0),
        v,
        model,
        tmp_path / "cal.png",
        observed_t_b_k=model * 0.65,
        observed_axis_label="T_A (K)",
        comparison=comparison,
    )
    assert out.exists() and out.stat().st_size > 0


def test_calibrated_figure_without_residual_renders(tmp_path) -> None:
    v = np.linspace(-200, 200, 128)
    out = profile_overlay_figure(
        v,
        np.full(128, -40.0),
        _MODEL.v_lsr_kms,
        _MODEL.t_b_k,
        tmp_path / "cal-noresid.png",
        observed_t_b_k=np.full(128, 5.0),
        observed_axis_label="T_B (K)",
    )
    assert out.exists() and out.stat().st_size > 0


def test_calibrated_block_present_with_tsys(
    client: TestClient, engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(captures_router, "reference_profile", lambda *a, **k: _MODEL)
    cap_id = _capture_with_observation(engine, _write_npz(tmp_path / "c.npz"))
    _stamp_epoch(engine, cap_id, _cal_epoch(engine, tsys_k=150.0))

    cal = client.get(f"/api/captures/{cap_id}/overlay").json()["calibrated"]
    assert cal["available"] is True
    assert cal["tsys_k"] == 150.0
    # eta_mb defaults to 1.0, so the axis must be labelled antenna temperature.
    assert cal["eta_mb"] == 1.0 and cal["is_main_beam"] is False
    assert cal["axis_label"] == "T_A (K)"
    assert len(cal["temperature_k"]) == 256

    resp = client.get(f"/api/captures/{cap_id}/overlay.png?calibrated=1&residual=1")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"


def test_calibrated_block_degrades_without_tsys(
    client: TestClient, engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(captures_router, "reference_profile", lambda *a, **k: _MODEL)
    cap_id = _capture_with_observation(engine, _write_npz(tmp_path / "c.npz"))

    body = client.get(f"/api/captures/{cap_id}/overlay").json()
    # The shape-only overlay must survive a missing calibration.
    assert body["available"] is True
    assert body["calibrated"]["available"] is False
    assert "calibration epoch" in body["calibrated"]["reason"]

    # Shape-only PNG still renders; the calibrated one 409s with the reason.
    assert client.get(f"/api/captures/{cap_id}/overlay.png").status_code == 200
    resp = client.get(f"/api/captures/{cap_id}/overlay.png?calibrated=1")
    assert resp.status_code == 409 and "calibration epoch" in resp.json()["detail"]


def test_calibrated_block_degrades_when_epoch_has_no_tsys(
    client: TestClient, engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(captures_router, "reference_profile", lambda *a, **k: _MODEL)
    cap_id = _capture_with_observation(engine, _write_npz(tmp_path / "c.npz"))
    _stamp_epoch(engine, cap_id, _cal_epoch(engine, tsys_k=None))

    cal = client.get(f"/api/captures/{cap_id}/overlay").json()["calibrated"]
    assert cal["available"] is False and "sky/ground" in cal["reason"]


def test_eta_mb_setting_relabels_the_axis(
    engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(captures_router, "reference_profile", lambda *a, **k: _MODEL)
    client = TestClient(
        create_app(
            Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path / "data"), eta_mb=0.65),
            engine=engine,
        )
    )
    cap_id = _capture_with_observation(engine, _write_npz(tmp_path / "c.npz"))
    _stamp_epoch(engine, cap_id, _cal_epoch(engine, tsys_k=150.0))

    cal = client.get(f"/api/captures/{cap_id}/overlay").json()["calibrated"]
    assert cal["eta_mb"] == 0.65 and cal["is_main_beam"] is True
    assert cal["axis_label"] == "T_B (K)"


def test_overlay_panel_fragment_carries_toggles_and_stats(
    client: TestClient, engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(captures_router, "reference_profile", lambda *a, **k: _MODEL)
    cap_id = _capture_with_observation(engine, _write_npz(tmp_path / "c.npz"))
    _stamp_epoch(engine, cap_id, _cal_epoch(engine, tsys_k=150.0))

    html = client.get(f"/captures/{cap_id}/overlay_panel").text
    assert 'data-mode="calibrated"' in html and 'data-mode="residual"' in html
    # Calibrated defaults ON when a Tsys exists — the check runs itself.
    assert "calibrated=1" in html
    assert "not a detection verdict" in html
    # The default eta_mb=1.0 must say so rather than implying a brightness temperature.
    assert "no main-beam efficiency applied" in html


def test_overlay_panel_reports_unavailability(
    client: TestClient, engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(captures_router, "reference_profile", lambda *a, **k: None)
    cap_id = _capture_with_observation(engine, _write_npz(tmp_path / "c.npz"))

    html = client.get(f"/captures/{cap_id}/overlay_panel").text
    assert "unavailable" in html


def test_overlay_panel_notes_shape_only_without_tsys(
    client: TestClient, engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(captures_router, "reference_profile", lambda *a, **k: _MODEL)
    cap_id = _capture_with_observation(engine, _write_npz(tmp_path / "c.npz"))

    html = client.get(f"/captures/{cap_id}/overlay_panel").text
    assert "Shape comparison only" in html
    assert 'data-mode="calibrated"' in html and "disabled" in html

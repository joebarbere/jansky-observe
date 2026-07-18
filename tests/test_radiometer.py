"""Radiometer-equation sensitivity (roadmap M12): reduction + the capture route."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session

from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ
from jansky_observe.config import Settings
from jansky_observe.confirm.radiometer import radiometer_estimate
from jansky_observe.db import init_db
from jansky_observe.models import CalibrationEpoch, Capture
from jansky_observe.server.app import create_app

DEAD_ENDPOINT = "tcp://127.0.0.1:1"
RATE_HZ = 6.0e6


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


# ---- pure reduction ----------------------------------------------------------------


def test_delta_t_rms_matches_the_equation() -> None:
    out = radiometer_estimate(tsys_k=150.0, channel_bw_hz=1.0e4, integration_s=100.0)
    expected = 150.0 / math.sqrt(1.0e4 * 100.0)
    assert out["delta_t_rms_k"] == pytest.approx(expected)
    assert out["delta_t_rms_mk"] == pytest.approx(expected * 1e3)
    assert out["predicted_snr"] == pytest.approx(50.0 / expected)


def test_more_integration_lowers_the_noise_floor() -> None:
    short = radiometer_estimate(tsys_k=150.0, channel_bw_hz=1.0e4, integration_s=10.0)
    long = radiometer_estimate(tsys_k=150.0, channel_bw_hz=1.0e4, integration_s=1000.0)
    assert long["delta_t_rms_k"] < short["delta_t_rms_k"]
    # 100x integration -> 10x lower rms.
    assert short["delta_t_rms_k"] / long["delta_t_rms_k"] == pytest.approx(10.0)


def test_time_to_target_scales_with_tsys_squared() -> None:
    lo = radiometer_estimate(tsys_k=100.0, channel_bw_hz=1.0e4, integration_s=10.0)
    hi = radiometer_estimate(tsys_k=200.0, channel_bw_hz=1.0e4, integration_s=10.0)
    assert hi["time_to_target_s"] / lo["time_to_target_s"] == pytest.approx(4.0)


def test_achieved_snr_only_with_measured_peak() -> None:
    assert (
        radiometer_estimate(tsys_k=150.0, channel_bw_hz=1.0e4, integration_s=100.0)["achieved_snr"]
        is None
    )
    out = radiometer_estimate(
        tsys_k=150.0, channel_bw_hz=1.0e4, integration_s=100.0, measured_peak_k=3.0
    )
    assert out["achieved_snr"] == pytest.approx(3.0 / out["delta_t_rms_k"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tsys_k": 0.0, "channel_bw_hz": 1.0, "integration_s": 1.0},
        {"tsys_k": 1.0, "channel_bw_hz": 0.0, "integration_s": 1.0},
        {"tsys_k": 1.0, "channel_bw_hz": 1.0, "integration_s": -1.0},
        {"tsys_k": 1.0, "channel_bw_hz": 1.0, "integration_s": 1.0, "assumed_line_k": 0.0},
    ],
)
def test_non_positive_inputs_raise(kwargs) -> None:
    with pytest.raises(ValueError, match="must all be positive"):
        radiometer_estimate(**kwargs)


def test_estimate_is_json_serialisable() -> None:
    json.dumps(radiometer_estimate(tsys_k=120.0, channel_bw_hz=5e3, integration_s=300.0))


# ---- capture route -----------------------------------------------------------------


def _write_npz(path: Path, *, n_fft: int = 256, n_frames: int = 60) -> Path:
    """A minimal .npz whose timestamps span n_frames-1 seconds of integration."""
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


def _capture_with_tsys(engine: Engine, npz: Path, tsys_k: float | None) -> int:
    with Session(engine) as session:
        epoch_id = None
        if tsys_k is not None:
            epoch = CalibrationEpoch(notes="m12", tsys_k=tsys_k, sky_ground_delta_db=6.0)
            session.add(epoch)
            session.commit()
            session.refresh(epoch)
            epoch_id = epoch.id
        capture = Capture(
            device="synthetic",
            path=str(npz),
            format="npz_spectra",
            size_bytes=npz.stat().st_size,
            start=datetime(2026, 7, 1, 3, 0, tzinfo=UTC),
            end=datetime(2026, 7, 1, 3, 1, tzinfo=UTC),
            cal_epoch_id=epoch_id,
        )
        session.add(capture)
        session.commit()
        session.refresh(capture)
        assert capture.id is not None
        return capture.id


def test_radiometer_route_available_with_tsys(client: TestClient, engine: Engine, tmp_path) -> None:
    cap_id = _capture_with_tsys(engine, _write_npz(tmp_path / "c.npz"), tsys_k=150.0)
    body = client.get(f"/api/captures/{cap_id}/radiometer").json()
    assert body["available"] is True
    # channel_bw = 6e6/256 ; integration = 59 s (timestamps 0..59).
    expected_bw = RATE_HZ / 256
    assert body["channel_bw_hz"] == pytest.approx(expected_bw)
    assert body["integration_s"] == pytest.approx(59.0)
    assert body["delta_t_rms_k"] == pytest.approx(150.0 / math.sqrt(expected_bw * 59.0))


def test_radiometer_route_unavailable_without_tsys(
    client: TestClient, engine: Engine, tmp_path
) -> None:
    cap_id = _capture_with_tsys(engine, _write_npz(tmp_path / "c.npz"), tsys_k=None)
    body = client.get(f"/api/captures/{cap_id}/radiometer").json()
    assert body["available"] is False
    assert "calibration epoch" in body["reason"]

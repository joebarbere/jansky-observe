"""Total-power noise diagnostic (roadmap M12): distribution reduction + routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session

from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ
from jansky_observe.config import Settings
from jansky_observe.confirm.baseline import linear_to_db
from jansky_observe.confirm.noise import power_distribution
from jansky_observe.db import init_db
from jansky_observe.export.figures import total_power_histogram_figure
from jansky_observe.models import Capture
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


def _power_db_from_linear_series(series_linear: np.ndarray, n_fft: int = 4) -> np.ndarray:
    """A (n_frames, n_fft) power_db whose per-frame band-mean linear power == series."""
    per_frame_db = linear_to_db(series_linear)  # constant across channels → band-mean == value
    return np.repeat(per_frame_db[:, None], n_fft, axis=1)


# ---- pure reduction ----------------------------------------------------------------


def test_gaussian_series_is_not_flagged() -> None:
    gen = np.random.default_rng(3)
    series = gen.normal(100.0, 5.0, size=600)
    dist = power_distribution(_power_db_from_linear_series(series))
    assert dist.n_frames == 600
    assert dist.mean == pytest.approx(100.0, abs=1.0)
    assert dist.sigma == pytest.approx(5.0, abs=0.6)
    assert abs(dist.skew) < 1.0 and abs(dist.excess_kurtosis) < 1.0
    assert dist.non_gaussian is False


def test_spiky_series_is_flagged_non_gaussian() -> None:
    series = np.concatenate([np.full(200, 1.0), np.array([80.0, 90.0, 100.0])])  # heavy tail
    dist = power_distribution(_power_db_from_linear_series(series))
    assert dist.non_gaussian is True
    assert dist.skew > 1.0


def test_flat_series_is_trivially_gaussian_and_json_safe() -> None:
    dist = power_distribution(_power_db_from_linear_series(np.full(10, 5.0)))
    assert dist.sigma == 0.0 and dist.skew == 0.0 and dist.non_gaussian is False
    json.dumps(dist.stats())  # no NaN


def test_too_few_frames_and_bad_shape_raise() -> None:
    with pytest.raises(ValueError, match="at least 3 frames"):
        power_distribution(_power_db_from_linear_series(np.array([1.0, 2.0])))
    with pytest.raises(ValueError, match="shape"):
        power_distribution(np.array([1.0, 2.0, 3.0]))  # 1-D


def test_histogram_figure_renders(tmp_path) -> None:
    gen = np.random.default_rng(1)
    dist = power_distribution(_power_db_from_linear_series(gen.normal(50.0, 3.0, size=200)))
    out = total_power_histogram_figure(dist, tmp_path / "hist.png", title="t")
    assert out.exists() and out.stat().st_size > 0


# ---- routes ------------------------------------------------------------------------


def _write_varied_npz(path: Path, *, n_fft: int = 64, n_frames: int = 120) -> Path:
    gen = np.random.default_rng(7)
    power_db = -40.0 + gen.normal(0.0, 0.5, size=(n_frames, n_fft))
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        power_db=power_db.astype(np.float64),
        center_freq_hz=HI_LINE_FREQ_HZ,
        sample_rate_hz=RATE_HZ,
        timestamps=np.arange(n_frames, dtype=np.float64),
        settings=json.dumps({"gain": 15}),
    )
    return path


def _capture(engine: Engine, npz: Path) -> int:
    with Session(engine) as session:
        capture = Capture(
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


def test_noise_route_returns_stats(client: TestClient, engine: Engine, tmp_path) -> None:
    cap_id = _capture(engine, _write_varied_npz(tmp_path / "c.npz"))
    body = client.get(f"/api/captures/{cap_id}/noise").json()
    assert body["n_frames"] == 120
    assert set(body) == {"n_frames", "mean", "sigma", "skew", "excess_kurtosis", "non_gaussian"}


def test_power_histogram_png_renders(client: TestClient, engine: Engine, tmp_path) -> None:
    cap_id = _capture(engine, _write_varied_npz(tmp_path / "c.npz"))
    resp = client.get(f"/api/captures/{cap_id}/power_histogram.png")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert len(resp.content) > 0

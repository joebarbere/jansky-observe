"""Tests for the live HI badge: accumulation, verdicts, LSR window, endpoints.

Fixtures follow the /synthetic-fixture pattern: seeded, hand-built power
spectra with a Gaussian fake-HI bump at the rest frequency — no hardware,
no sky, no network (astropy runs offline).
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
import zmq
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe import frames
from jansky_observe.astro.lsr import HI_LINE_FREQ_HZ
from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import Observation, ObservationType, RadioSource, utcnow
from jansky_observe.server.app import create_app
from jansky_observe.server.live_badge import MIN_FRAMES, LiveBadge

DEAD_ENDPOINT = "tcp://127.0.0.1:1"
N = 1024
CENTER_HZ, RATE_HZ = 1420.4e6, 3e6
T0 = 1_752_000_000.0


def _frame(
    seq: int, line_amp: float = 0.15, center_hz: float = CENTER_HZ, n: int = N
) -> frames.SpectralFrame:
    """A synthetic frame: smoothed noise floor + a Gaussian HI bump at rest frequency.

    Smoothed noise mimics a Welch-averaged spectrum (adjacent channels
    correlated) — the same fixture convention as ``tests/test_classifier.py``.
    """
    smooth = 31
    gen = np.random.default_rng(seq)
    freq = center_hz + (np.arange(n) - n / 2) * (RATE_HZ / n)
    noise = np.convolve(gen.standard_normal(n + smooth - 1), np.ones(smooth) / smooth, mode="valid")
    linear = 1.0 + 0.02 * noise
    linear += line_amp * np.exp(-0.5 * ((freq - HI_LINE_FREQ_HZ) / 50e3) ** 2)
    power_db = 10.0 * np.log10(np.maximum(linear, 1e-6))
    return frames.SpectralFrame(
        seq=seq,
        timestamp=T0 + seq,
        center_freq_hz=center_hz,
        sample_rate_hz=RATE_HZ,
        power_db=power_db.astype(np.float32),
    )


@pytest.fixture()
def engine(tmp_path) -> Engine:
    """A fully migrated + seeded database in a tmp dir."""
    return init_db(tmp_path)


def _factory(engine: Engine):
    return lambda: Session(engine)


def _start_running_observation(engine: Engine, source_name: str = "Cygnus region HI") -> int:
    """Create a status='running' observation directly via the models."""
    with Session(engine) as session:
        obs_type = session.exec(select(ObservationType)).first()
        source = session.exec(select(RadioSource).where(RadioSource.name == source_name)).one()
        assert obs_type is not None and obs_type.id is not None and source.id is not None
        observation = Observation(
            name="live badge test",
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


# ---- LiveBadge unit behavior ----------------------------------------------------


def test_accumulating_below_min_frames(engine: Engine) -> None:
    badge = LiveBadge()
    for seq in range(MIN_FRAMES - 1):
        badge.add_frame(_frame(seq))
    snapshot = badge.snapshot(_factory(engine))
    assert snapshot == {"status": "accumulating", "n_frames": MIN_FRAMES - 1}


def test_detected_with_fixed_window_when_nothing_running(engine: Engine) -> None:
    badge = LiveBadge()
    for seq in range(12):
        badge.add_frame(_frame(seq))
    snapshot = badge.snapshot(_factory(engine))
    assert snapshot["status"] == "ok"
    assert snapshot["verdict"] == "detected"
    assert snapshot["snr"] >= 5.0
    assert snapshot["peak_freq_hz"] == pytest.approx(HI_LINE_FREQ_HZ, abs=20e3)
    assert snapshot["n_frames"] == 12
    assert snapshot["elapsed_s"] == pytest.approx(11.0)
    assert snapshot["window_source"] == "fixed"
    lo_hz, hi_hz = snapshot["window_hz"]
    assert lo_hz < HI_LINE_FREQ_HZ < hi_hz
    assert snapshot["name"] == "hline_v1"
    assert snapshot["version"] == "1"
    assert "peak_vlsr_kms" not in snapshot  # fixed window: no pointing, no v_LSR


def test_no_line_not_detected(engine: Engine) -> None:
    badge = LiveBadge()
    for seq in range(12):
        badge.add_frame(_frame(seq, line_amp=0.0))
    snapshot = badge.snapshot(_factory(engine))
    assert snapshot["status"] == "ok"
    assert snapshot["verdict"] == "not_detected"
    assert snapshot["snr"] < 2.0


def test_reset_clears_the_accumulator(engine: Engine) -> None:
    badge = LiveBadge()
    for seq in range(12):
        badge.add_frame(_frame(seq))
    badge.reset()
    assert badge.n_frames == 0
    assert badge.snapshot(_factory(engine)) == {"status": "accumulating", "n_frames": 0}


def test_stream_parameter_change_auto_resets(engine: Engine) -> None:
    badge = LiveBadge()
    for seq in range(12):
        badge.add_frame(_frame(seq))
    for seq in range(3):  # retune: the running mean is invalid
        badge.add_frame(_frame(seq, center_hz=1421.0e6))
    snapshot = badge.snapshot(_factory(engine))
    assert snapshot == {"status": "accumulating", "n_frames": 3}


def test_lsr_window_used_while_an_observation_is_running(engine: Engine) -> None:
    _start_running_observation(engine)
    badge = LiveBadge()
    for seq in range(12):
        badge.add_frame(_frame(seq))
    snapshot = badge.snapshot(_factory(engine))
    assert snapshot["status"] == "ok"
    assert snapshot["window_source"] == "lsr"
    assert snapshot["verdict"] == "detected"
    # The LSR window differs from the fixed one (topocentric correction ≤ 50 km/s).
    lo_hz, hi_hz = snapshot["window_hz"]
    assert lo_hz < HI_LINE_FREQ_HZ < hi_hz
    # The synthetic line sits at the rest frequency: |v_LSR| is the correction, ≲ 50 km/s.
    assert abs(snapshot["peak_vlsr_kms"]) < 60.0


# ---- endpoints -------------------------------------------------------------------


def _app_client(engine: Engine, tmp_path) -> TestClient:
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    return TestClient(create_app(settings, engine=engine))


def test_badge_endpoint_and_reset(engine: Engine, tmp_path) -> None:
    client = _app_client(engine, tmp_path)
    app = client.app
    resp = client.get("/api/live/hi_badge")
    assert resp.status_code == 200
    assert resp.json() == {"status": "accumulating", "n_frames": 0}

    for seq in range(12):
        app.state.live_badge.add_frame(_frame(seq))
    body = client.get("/api/live/hi_badge").json()
    assert body["status"] == "ok"
    assert body["verdict"] == "detected"
    assert body["window_source"] == "fixed"

    resp = client.post("/api/live/hi_badge/reset")
    assert resp.status_code == 200
    assert resp.json() == {"status": "accumulating", "n_frames": 0}
    assert client.get("/api/live/hi_badge").json()["n_frames"] == 0


def test_index_renders_badge_chip() -> None:
    client = TestClient(create_app(Settings(zmq_endpoint=DEAD_ENDPOINT)))
    body = client.get("/").text
    assert 'id="hi-badge"' in body
    assert 'id="hi-badge-reset"' in body
    js = client.get("/static/waterfall.js").text
    assert "/api/live/hi_badge" in js


def test_zmq_relay_feeds_the_badge(tmp_path) -> None:
    """The relay hands every decoded frame to the badge (same place as the fan-out)."""
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.bind("tcp://127.0.0.1:*")
    endpoint = pub.getsockopt_string(zmq.LAST_ENDPOINT)
    parts = frames.encode_zmq(_frame(seq=1))
    stop = threading.Event()

    def pump() -> None:
        while not stop.is_set():
            pub.send_multipart(parts)
            time.sleep(0.05)

    pumper = threading.Thread(target=pump, daemon=True)
    try:
        app = create_app(Settings(zmq_endpoint=endpoint, data_dir=str(tmp_path)))
        with TestClient(app):
            pumper.start()
            deadline = time.monotonic() + 15.0
            while app.state.live_badge.n_frames == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert app.state.live_badge.n_frames > 0
    finally:
        stop.set()
        if pumper.is_alive():
            pumper.join(timeout=2.0)
        pub.close(0)
        ctx.term()

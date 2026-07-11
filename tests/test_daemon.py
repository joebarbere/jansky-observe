"""Tests for the capture daemon: frame production, ZMQ publishing, CLI."""

from __future__ import annotations

import contextlib
import io
import threading
import time

import pytest
import zmq

from jansky_observe import __version__
from jansky_observe.capture.daemon import frame_producer, main, run
from jansky_observe.capture.sources import SyntheticHISource
from jansky_observe.frames import decode_zmq


def test_frame_producer_sequences_and_shape() -> None:
    source = SyntheticHISource(seed=0)
    producer = frame_producer(source, n_fft=256, averages=2)
    frames = [next(producer) for _ in range(3)]
    source.close()
    assert [f.seq for f in frames] == [0, 1, 2]
    for frame in frames:
        assert frame.power_db.size == 256
        assert frame.center_freq_hz == source.center_freq_hz
        assert frame.sample_rate_hz == source.sample_rate_hz
        assert frame.timestamp > 0


def test_run_publishes_frames_over_zmq() -> None:
    ctx = zmq.Context()
    source = SyntheticHISource(seed=42)
    bound: list[str] = []
    published: list[int] = []

    def target() -> None:
        published.append(
            run(
                source,
                "tcp://127.0.0.1:*",
                fps=50.0,
                n_fft=256,
                averages=2,
                max_frames=3,
                warmup_s=0.5,
                on_bound=bound.append,
                context=ctx,
            )
        )

    thread = threading.Thread(target=target)
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while not bound and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bound, "run() never reported a bound endpoint"

        sub = ctx.socket(zmq.SUB)
        sub.connect(bound[0])
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        frames = []
        deadline = time.monotonic() + 10.0
        while len(frames) < 3 and time.monotonic() < deadline:
            if sub.poll(timeout=1000):
                frames.append(decode_zmq(sub.recv_multipart()))
        sub.close(linger=0)
    finally:
        thread.join(timeout=10.0)
        source.close()
        ctx.term()

    assert not thread.is_alive()
    assert published == [3]
    assert [f.seq for f in frames] == [0, 1, 2]
    for frame in frames:
        assert frame.power_db.size == 256
        assert frame.center_freq_hz == 1420.4e6
        assert frame.sample_rate_hz == 3e6
        assert frame.timestamp == pytest.approx(time.time(), abs=60.0)


def test_run_rejects_nonpositive_fps() -> None:
    source = SyntheticHISource(seed=0)
    with pytest.raises(ValueError, match="fps"):
        run(source, "tcp://127.0.0.1:*", fps=0.0)
    source.close()


def test_main_version() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in out.getvalue()


def test_main_requires_synthetic() -> None:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
    assert "--synthetic" in err.getvalue()
    assert "M1" in err.getvalue()


def test_main_synthetic_end_to_end() -> None:
    code = main(
        [
            "--synthetic",
            "--endpoint",
            "tcp://127.0.0.1:*",
            "--fps",
            "100",
            "--n-fft",
            "256",
            "--avg",
            "2",
            "--max-frames",
            "2",
        ]
    )
    assert code == 0

"""The ``jansky-observe-capture`` daemon: SDR source → Welch PSD → ZeroMQ PUB.

Reads IQ chunks from an :class:`~jansky_observe.capture.sources.SDRSource`,
reduces each chunk to a :class:`~jansky_observe.frames.SpectralFrame`, and
publishes the encoded frames on a ZeroMQ PUB socket at a paced frame rate
for the API server to fan out to browsers. A ZeroMQ REP control socket
(:mod:`jansky_observe.control`) answers ``status`` and drives capture
start/stop: ``.npz`` captures record the published frames; SigMF captures
drain the source's lossless tap while the live frames keep flowing.

Sources: ``--source synthetic`` (no hardware) or ``--source airspy`` (the
real Airspy Mini via ``airspy_rx``). SAFETY: the hardware path goes through
:func:`jansky_observe.capture.profiles.validate_profile` — the Airspy
internal bias tee is never enabled in the H-line profile.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zmq

from jansky_observe import __version__
from jansky_observe.capture.dsp import welch_psd_db
from jansky_observe.capture.profiles import HLINE_AIRSPY, validate_profile
from jansky_observe.capture.sources import SDRSource, SyntheticHISource
from jansky_observe.capture.writer import NpzCaptureWriter, SigmfCaptureWriter
from jansky_observe.config import settings_from_env
from jansky_observe.frames import SpectralFrame, encode_zmq

__all__ = ["CaptureManager", "frame_producer", "main", "run"]


class CaptureManager:
    """Owns the active capture writer and answers control requests."""

    def __init__(
        self,
        source: SDRSource,
        *,
        data_dir: str | Path,
        source_name: str,
        n_fft: int,
        fps: float,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self._source = source
        self._data_dir = Path(data_dir)
        self._source_name = source_name
        self._n_fft = n_fft
        self._fps = fps
        self._settings = dict(settings or {})
        self._format: str | None = None
        self._npz: NpzCaptureWriter | None = None
        self._sigmf: SigmfCaptureWriter | None = None
        self._path: Path | None = None
        self._started_at = 0.0

    @property
    def capturing(self) -> bool:
        """Whether a capture is active."""
        return self._format is not None

    def status(self) -> dict[str, Any]:
        """The control-protocol status payload (see :mod:`jansky_observe.control`)."""
        writer = self._npz or self._sigmf
        if self._format == "sigmf":
            rate = self._source.sample_rate_hz * 4  # ci16_le: 2 × int16 per sample
        elif self._format == "npz":
            rate = self._n_fft * 4 * self._fps
        else:
            rate = 0.0
        try:
            disk_free = shutil.disk_usage(self._data_dir).free
        except OSError:
            disk_free = shutil.disk_usage(self._data_dir.parent).free
        return {
            "ok": True,
            "capturing": self.capturing,
            "format": self._format,
            "path": str(self._path) if self._path else None,
            "bytes_written": writer.bytes_written if writer else 0,
            "elapsed_s": time.time() - self._started_at if self.capturing else 0.0,
            "rate_bytes_per_s": rate,
            "disk_free_bytes": disk_free,
            "source": self._source_name,
            "overrun": self._source.overrun,
        }

    def start(self, fmt: str) -> dict[str, Any]:
        """Start a capture; returns the new status or an error reply."""
        if self.capturing:
            return {"ok": False, "error": f"already capturing to {self._path}"}
        if fmt not in ("npz", "sigmf"):
            return {"ok": False, "error": f"unknown capture format {fmt!r}"}
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")
        captures = self._data_dir / "captures"
        captures.mkdir(parents=True, exist_ok=True)
        settings = {
            **self._settings,
            "source": self._source_name,
            "center_freq_hz": self._source.center_freq_hz,
            "sample_rate_hz": self._source.sample_rate_hz,
            "n_fft": self._n_fft,
            "fps": self._fps,
        }
        if fmt == "npz":
            self._path = captures / f"capture-{stamp}.npz"
            self._npz = NpzCaptureWriter(self._path, settings)
        else:
            base = captures / f"capture-{stamp}"
            self._sigmf = SigmfCaptureWriter(
                base,
                sample_rate_hz=self._source.sample_rate_hz,
                center_freq_hz=self._source.center_freq_hz,
                settings=settings,
            )
            self._path = base.with_suffix(".sigmf-data")
            self._source.start_tap()
        self._format = fmt
        self._started_at = time.time()
        return self.status()

    def on_frame(self, frame: SpectralFrame) -> None:
        """Feed one published frame (npz capture) and drain the tap (sigmf)."""
        if self._npz is not None:
            self._npz.add_frame(frame)
        if self._sigmf is not None:
            values = self._source.read_tap()
            if values is not None:
                self._sigmf.write(values)

    def stop(self) -> dict[str, Any]:
        """Stop the capture; returns the final status or an error reply."""
        if not self.capturing:
            return {"ok": False, "error": "no capture in progress"}
        if self._sigmf is not None:
            values = self._source.read_tap()
            if values is not None:
                self._sigmf.write(values)
            self._source.stop_tap()
            self._sigmf.close()
        if self._npz is not None:
            self._npz.close()
        final = self.status()
        self._format = None
        self._npz = None
        self._sigmf = None
        return final

    def handle(self, raw: bytes) -> dict[str, Any]:
        """Dispatch one control-channel request."""
        try:
            request = json.loads(raw)
            cmd = request.get("cmd")
        except (ValueError, AttributeError):
            return {"ok": False, "error": "malformed control request"}
        if cmd == "status":
            return self.status()
        if cmd == "start_capture":
            return self.start(request.get("format", ""))
        if cmd == "stop_capture":
            return self.stop()
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def frame_producer(
    source: SDRSource,
    *,
    n_fft: int = 2048,
    averages: int = 8,
) -> Iterator[SpectralFrame]:
    """Yield an endless stream of spectral frames from ``source``.

    Each frame reduces ``n_fft * averages`` fresh samples to an ``n_fft``-bin
    Welch PSD. Sequence numbers start at 0 and increment by one; timestamps
    are ``time.time()`` at reduction.

    Parameters
    ----------
    source
        The SDR (or synthetic) sample source.
    n_fft
        FFT length / output bins per frame.
    averages
        Welch segments averaged per frame.
    """
    if n_fft <= 0 or averages <= 0:
        raise ValueError("n_fft and averages must be positive")
    seq = 0
    while True:
        iq = source.read(n_fft * averages)
        yield SpectralFrame(
            seq=seq,
            timestamp=time.time(),
            center_freq_hz=source.center_freq_hz,
            sample_rate_hz=source.sample_rate_hz,
            power_db=welch_psd_db(iq, source.sample_rate_hz, n_fft),
        )
        seq += 1


def run(
    source: SDRSource,
    endpoint: str,
    *,
    fps: float = 4.0,
    n_fft: int = 2048,
    averages: int = 8,
    max_frames: int | None = None,
    warmup_s: float = 0.25,
    on_bound: Callable[[str], None] | None = None,
    context: zmq.Context | None = None,
    ctl_endpoint: str | None = None,
    data_dir: str | Path = "data",
    source_name: str = "synthetic",
    on_ctl_bound: Callable[[str], None] | None = None,
) -> int:
    """Bind a ZeroMQ PUB socket and publish spectral frames at a paced rate.

    Parameters
    ----------
    source
        The sample source to reduce and publish.
    endpoint
        ZeroMQ endpoint to bind, e.g. ``tcp://127.0.0.1:8410``. Ephemeral
        (``tcp://127.0.0.1:*``) is fine; ``on_bound`` reports the resolved
        endpoint.
    fps
        Target frames per second.
    n_fft, averages
        Frame reduction parameters (see :func:`frame_producer`).
    max_frames
        Stop after publishing this many frames; ``None`` runs forever.
    warmup_s
        Sleep between bind and the first publish, so late-joining SUB
        sockets don't miss the opening frames (the ZeroMQ PUB/SUB
        slow-joiner problem).
    on_bound
        Optional callback invoked with the resolved endpoint after bind —
        lets tests (and supervisors) discover ephemeral ports.
    context
        ZeroMQ context to use; defaults to the process-global instance.

    Returns
    -------
    int
        The number of frames published.
    """
    if fps <= 0:
        raise ValueError("fps must be positive")
    ctx = context if context is not None else zmq.Context.instance()
    socket = ctx.socket(zmq.PUB)
    rep: zmq.Socket | None = None
    manager = CaptureManager(
        source, data_dir=data_dir, source_name=source_name, n_fft=n_fft, fps=fps
    )
    published = 0
    try:
        socket.bind(endpoint)
        if on_bound is not None:
            on_bound(socket.getsockopt_string(zmq.LAST_ENDPOINT))
        if ctl_endpoint is not None:
            rep = ctx.socket(zmq.REP)
            rep.bind(ctl_endpoint)
            if on_ctl_bound is not None:
                on_ctl_bound(rep.getsockopt_string(zmq.LAST_ENDPOINT))
        time.sleep(warmup_s)
        interval = 1.0 / fps
        next_send = time.monotonic()
        for frame in frame_producer(source, n_fft=n_fft, averages=averages):
            socket.send_multipart(encode_zmq(frame))
            published += 1
            manager.on_frame(frame)
            # Answer any pending control requests between frames (non-blocking).
            while rep is not None and rep.poll(0, zmq.POLLIN):
                rep.send(json.dumps(manager.handle(rep.recv())).encode())
            if max_frames is not None and published >= max_frames:
                break
            next_send += interval
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    finally:
        if manager.capturing:
            manager.stop()
        if rep is not None:
            rep.close(linger=0)
        socket.close(linger=0)
    return published


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``jansky-observe-capture``."""
    parser = argparse.ArgumentParser(
        prog="jansky-observe-capture",
        description="Capture daemon: stream spectral frames over ZeroMQ PUB.",
    )
    env = settings_from_env()
    parser.add_argument(
        "--source",
        choices=("synthetic", "airspy"),
        default=None,
        help="sample source: synthetic (no hardware) or airspy (the real Airspy Mini)",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="deprecated alias for --source synthetic",
    )
    parser.add_argument(
        "--endpoint",
        default=env.zmq_endpoint,
        help="ZeroMQ PUB endpoint to bind (default: JANSKY_OBSERVE_ZMQ_ENDPOINT or %(default)s)",
    )
    parser.add_argument(
        "--ctl-endpoint",
        default=env.ctl_endpoint,
        help="ZeroMQ REP control endpoint (default: JANSKY_OBSERVE_CTL_ENDPOINT or %(default)s)",
    )
    parser.add_argument(
        "--data-dir",
        default=env.data_dir,
        help="capture output directory (default: JANSKY_OBSERVE_DATA_DIR or %(default)s)",
    )
    parser.add_argument(
        "--gain", type=int, default=16, help="Airspy linearity gain 0..21 (default 16)"
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=3_000_000,
        choices=(3_000_000, 6_000_000),
        help="Airspy sample rate in Hz (default 3000000)",
    )
    parser.add_argument(
        "--airspy-binary",
        default="airspy_rx",
        help="airspy_rx executable (tests inject a fake)",
    )
    parser.add_argument("--fps", type=float, default=4.0, help="frames per second (default 4.0)")
    parser.add_argument("--n-fft", type=int, default=2048, help="FFT bins per frame (default 2048)")
    parser.add_argument("--avg", type=int, default=8, help="Welch averages per frame (default 8)")
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="stop after this many frames (default: run forever)",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="seed for the synthetic source (default 0)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    source_name = args.source or ("synthetic" if args.synthetic else None)
    if source_name is None:
        parser.error("pass --source synthetic or --source airspy")

    source: SDRSource
    if source_name == "airspy":
        from jansky_observe.capture.airspy_cli import AirspyRxSource

        profile = validate_profile(
            dataclasses.replace(HLINE_AIRSPY, sample_rate_hz=float(args.sample_rate))
        )
        try:
            source = AirspyRxSource(profile, gain=args.gain, binary=args.airspy_binary)
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))
    else:
        source = SyntheticHISource(seed=args.seed)
    try:
        run(
            source,
            args.endpoint,
            fps=args.fps,
            n_fft=args.n_fft,
            averages=args.avg,
            max_frames=args.max_frames,
            ctl_endpoint=args.ctl_endpoint,
            data_dir=args.data_dir,
            source_name=source_name,
        )
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

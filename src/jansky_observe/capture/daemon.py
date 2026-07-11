"""The ``jansky-observe-capture`` daemon: SDR source → Welch PSD → ZeroMQ PUB.

Reads IQ chunks from an :class:`~jansky_observe.capture.sources.SDRSource`,
reduces each chunk to a :class:`~jansky_observe.frames.SpectralFrame`, and
publishes the encoded frames on a ZeroMQ PUB socket at a paced frame rate
for the API server to fan out to browsers.

At M0 the only source is ``--synthetic`` (no hardware); the real Airspy Mini
source arrives at M1. SAFETY: any future hardware path must go through
:func:`jansky_observe.capture.profiles.validate_profile` — the Airspy
internal bias tee is never enabled in the H-line profile.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Iterator

import zmq

from jansky_observe import __version__
from jansky_observe.capture.dsp import welch_psd_db
from jansky_observe.capture.sources import SDRSource, SyntheticHISource
from jansky_observe.config import settings_from_env
from jansky_observe.frames import SpectralFrame, encode_zmq

__all__ = ["frame_producer", "main", "run"]


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
    published = 0
    try:
        socket.bind(endpoint)
        if on_bound is not None:
            on_bound(socket.getsockopt_string(zmq.LAST_ENDPOINT))
        time.sleep(warmup_s)
        interval = 1.0 / fps
        next_send = time.monotonic()
        for frame in frame_producer(source, n_fft=n_fft, averages=averages):
            socket.send_multipart(encode_zmq(frame))
            published += 1
            if max_frames is not None and published >= max_frames:
                break
            next_send += interval
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    finally:
        socket.close(linger=0)
    return published


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``jansky-observe-capture``."""
    parser = argparse.ArgumentParser(
        prog="jansky-observe-capture",
        description="Capture daemon: stream spectral frames over ZeroMQ PUB.",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="stream synthetic noise + fake-HI frames (required at M0: no hardware yet)",
    )
    parser.add_argument(
        "--endpoint",
        default=settings_from_env().zmq_endpoint,
        help="ZeroMQ PUB endpoint to bind (default: JANSKY_OBSERVE_ZMQ_ENDPOINT or %(default)s)",
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

    if not args.synthetic:
        parser.error(
            "no hardware source exists at M0: pass --synthetic (real Airspy support arrives at M1)"
        )

    source = SyntheticHISource(seed=args.seed)
    try:
        run(
            source,
            args.endpoint,
            fps=args.fps,
            n_fft=args.n_fft,
            averages=args.avg,
            max_frames=args.max_frames,
        )
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

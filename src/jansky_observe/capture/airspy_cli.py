"""The real Airspy Mini via an ``airspy_rx`` subprocess. SAFETY-CRITICAL PATH.

``airspy_rx`` streams interleaved INT16 I/Q to stdout at the full sample rate
(~12 MB/s at 3 MSPS). A reader thread always drains the pipe — if nobody
drains it, the pipe fills, ``airspy_rx`` blocks, and its internal buffers
overrun — into a latest-wins ring for the live view, plus a bounded lossless
queue while a capture tap is active (see :class:`SDRSource`).

**The bias-tee rule** (CLAUDE.md safety invariants): :func:`build_airspy_cmd`
validates the profile and has *no parameter* that could emit ``-b`` — the
H-line feed is powered by the inline USB-C injector, never by the Airspy's
internal bias tee. Do not add one.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque

import numpy as np

from jansky_observe.capture.profiles import HLINE_AIRSPY, DeviceProfile, validate_profile

__all__ = ["AirspyRxSource", "build_airspy_cmd"]

_READ_BLOCK_BYTES = 262_144  # 128 Ki int16 values ≈ 22 ms of stream at 3 MSPS


def build_airspy_cmd(
    profile: DeviceProfile,
    *,
    gain: int = 16,
    binary: str | list[str] = "airspy_rx",
) -> list[str]:
    """Build the ``airspy_rx`` command line for ``profile``.

    ``-t 2`` selects INT16 IQ (half the pipe bandwidth of float32; the ADC is
    12-bit, so nothing is lost). ``-g`` is the linearity gain ladder (0–21).
    SAFETY: the profile is validated and there is deliberately no way to emit
    the ``-b`` bias-tee flag — see the module docstring.
    """
    validate_profile(profile)
    if not 0 <= gain <= 21:
        raise ValueError(f"linearity gain must be 0..21, got {gain}")
    argv = [binary] if isinstance(binary, str) else list(binary)
    return [
        *argv,
        "-r",
        "/dev/stdout",
        "-f",
        f"{profile.center_freq_hz / 1e6:.4f}",
        "-a",
        str(int(profile.sample_rate_hz)),
        "-t",
        "2",
        "-g",
        str(gain),
    ]


class AirspyRxSource:
    """``SDRSource`` over a live ``airspy_rx`` subprocess.

    Parameters
    ----------
    profile
        Device profile (validated; default :data:`HLINE_AIRSPY`).
    gain
        Linearity gain, 0–21.
    binary
        The ``airspy_rx`` executable — a path/name, or an argv prefix list
        (tests inject ``[sys.executable, "tests/fake_airspy_rx.py"]``).
    ring_seconds
        How much recent stream the live-view ring retains.
    tap_max_seconds
        Bound on the capture-tap queue; beyond it samples drop and the sticky
        :attr:`overrun` flag latches (captures report gaps, never hide them).
    """

    def __init__(
        self,
        profile: DeviceProfile = HLINE_AIRSPY,
        *,
        gain: int = 16,
        binary: str | list[str] = "airspy_rx",
        ring_seconds: float = 0.5,
        tap_max_seconds: float = 4.0,
    ) -> None:
        validate_profile(profile)
        self.center_freq_hz = profile.center_freq_hz
        self.sample_rate_hz = profile.sample_rate_hz
        self._cmd = build_airspy_cmd(profile, gain=gain, binary=binary)
        self._ring_max_values = int(2 * profile.sample_rate_hz * ring_seconds)
        self._tap_max_values = int(2 * profile.sample_rate_hz * tap_max_seconds)
        self._lock = threading.Lock()
        self._data_ready = threading.Condition(self._lock)
        self._ring: deque[np.ndarray] = deque()
        self._ring_values = 0
        self._tap: deque[np.ndarray] = deque()
        self._tap_values = 0
        self._tap_active = False
        self._overrun = False
        self._closed = False
        try:
            self._proc = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"airspy_rx not found ({self._cmd[0]!r}) — install the 'airspy' apt package"
            ) from exc
        self._reader = threading.Thread(target=self._drain, name="airspy-drain", daemon=True)
        self._reader.start()

    @property
    def overrun(self) -> bool:
        """Sticky flag: the capture tap dropped samples."""
        return self._overrun

    def _drain(self) -> None:
        """Reader thread: always consume the pipe so ``airspy_rx`` never blocks."""
        stdout = self._proc.stdout
        assert stdout is not None
        leftover = b""
        while True:
            block = stdout.read(_READ_BLOCK_BYTES)
            if not block:
                with self._data_ready:
                    self._data_ready.notify_all()
                return
            block = leftover + block
            usable = len(block) - (len(block) % 4)  # whole (I, Q) int16 pairs
            leftover = block[usable:]
            if usable == 0:
                continue
            values = np.frombuffer(block[:usable], dtype="<i2")
            with self._data_ready:
                self._ring.append(values)
                self._ring_values += values.size
                while self._ring_values > self._ring_max_values and len(self._ring) > 1:
                    self._ring_values -= self._ring.popleft().size
                if self._tap_active:
                    if self._tap_values + values.size > self._tap_max_values:
                        self._overrun = True  # drop, honestly
                    else:
                        self._tap.append(values)
                        self._tap_values += values.size
                self._data_ready.notify_all()

    def read(self, n_samples: int) -> np.ndarray:
        """Return the newest ``n_samples`` complex64 samples (latest wins)."""
        needed = 2 * n_samples
        deadline = time.monotonic() + 5.0
        with self._data_ready:
            while self._ring_values < needed:
                if self._closed:
                    raise RuntimeError("read() on a closed AirspyRxSource")
                if self._proc.poll() is not None:
                    raise RuntimeError(
                        f"airspy_rx exited with code {self._proc.returncode} — "
                        "check the device is connected (airspy_info) and not in use"
                    )
                if not self._data_ready.wait(timeout=deadline - time.monotonic()):
                    raise RuntimeError("timed out waiting for samples from airspy_rx")
            tail = np.concatenate(list(self._ring))[-needed:]
        iq = tail.astype(np.float32) / 32768.0
        return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)

    def start_tap(self) -> None:
        """Begin lossless tap collection (see class docstring for the bound)."""
        with self._lock:
            self._tap.clear()
            self._tap_values = 0
            self._tap_active = True

    def read_tap(self) -> np.ndarray | None:
        """Drain queued tap values (interleaved int16 I/Q); ``None`` when empty."""
        with self._lock:
            if not self._tap:
                return None
            chunks = list(self._tap)
            self._tap.clear()
            self._tap_values = 0
        return np.concatenate(chunks)

    def stop_tap(self) -> None:
        """Stop tap collection and discard anything still queued."""
        with self._lock:
            self._tap_active = False
            self._tap.clear()
            self._tap_values = 0

    def close(self) -> None:
        """Terminate ``airspy_rx`` and release the reader thread."""
        self._closed = True
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._reader.join(timeout=5)

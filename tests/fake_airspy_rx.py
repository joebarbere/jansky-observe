"""A stand-in for ``airspy_rx`` used by tests — no hardware, verifiable output.

Accepts the same flags :func:`build_airspy_cmd` emits and writes interleaved
INT16 I/Q to stdout where each int16 value is a counter modulo 16384 — so a
capture is gapless iff consecutive values differ by exactly 1 (mod 16384).
Runs until the pipe closes or SIGTERM. If ``-b`` appears in argv it exits 86
loudly: the bias-tee flag must never reach a real airspy_rx.
"""

from __future__ import annotations

import struct
import sys

WRAP = 16384
BLOCK_VALUES = 8192


def main() -> int:
    if "-b" in sys.argv:
        print("FATAL: bias-tee flag passed to fake airspy_rx", file=sys.stderr)
        return 86
    out = sys.stdout.buffer
    counter = 0
    block = bytearray(2 * BLOCK_VALUES)
    while True:
        for i in range(BLOCK_VALUES):
            struct.pack_into("<h", block, 2 * i, counter % WRAP)
            counter += 1
        try:
            out.write(block)
            out.flush()
        except (BrokenPipeError, OSError):
            return 0


if __name__ == "__main__":
    raise SystemExit(main())

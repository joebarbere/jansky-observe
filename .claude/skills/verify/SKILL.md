---
name: verify
description: The pre-commit gate for jansky-observe — lint, typecheck, coverage (85% floor), then the end-to-end synthetic smoke (capture daemon → ZMQ → API server → WebSocket frame). Use before every commit, and whenever asked whether the whole pipe still works.
---

# Verify: does the whole pipe still work?

Run the four steps **in order, stopping at the first failure**. Report pass/fail per step at the
end. Everything runs through `uv` (the pinned env); no hardware is needed — the smoke uses the
synthetic source.

## 1. Lint

```bash
make lint
```

## 2. Typecheck

```bash
make typecheck
```

## 3. Tests + coverage (85% floor)

```bash
make cov
```

The floor is enforced by `fail_under = 85` in `pyproject.toml` — a pass here means coverage held.

## 4. End-to-end synthetic smoke

Proves daemon → ZMQ → server → WebSocket end to end: start both processes, wait for health, read
one binary frame off the live WebSocket, assert it's non-empty. **Always kill both processes**,
pass or fail (trap / try-finally). Use port 8765 (or another free port — the server reads
`JANSKY_OBSERVE_PORT`).

```bash
uv run jansky-observe-capture --synthetic &
DAEMON_PID=$!
JANSKY_OBSERVE_PORT=8765 uv run jansky-observe --port 8765 &
SERVER_PID=$!
trap 'kill $DAEMON_PID $SERVER_PID 2>/dev/null' EXIT

# Poll healthz until ok (give it ~15 s):
for i in $(seq 1 30); do
    curl -fsS localhost:8765/healthz >/dev/null 2>&1 && break
    sleep 0.5
done
curl -fsS localhost:8765/healthz

# Read ONE binary frame from the live WebSocket (websockets is an installed dep):
uv run python - <<'EOF'
import asyncio

import websockets


async def main() -> None:
    async with websockets.connect("ws://localhost:8765/ws/live") as ws:
        frame = await asyncio.wait_for(ws.recv(), timeout=15)
    assert isinstance(frame, bytes) and len(frame) > 0, "empty frame"
    print(f"smoke ok: received {len(frame)}-byte frame")


asyncio.run(main())
EOF
```

A non-empty binary frame here means: the daemon generated a synthetic spectrum, published it over
ZMQ, the server decoded and re-packed it, and the WebSocket fanned it out — the whole M0 pipe.

## Report

Print a one-line verdict per step (`lint: PASS`, …). On any failure: stop, show the failing
output, and do not proceed to later steps — a red step means **do not commit**.

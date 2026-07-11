---
name: synthetic-fixture
description: Generate deterministic synthetic IQ/spectrum test fixtures — noise floor + fake-HI line, plus composed RFI spikes, baseline ripple, and frequency offsets — so DSP and classifier tests never need hardware or sky. Use when writing or extending any test that needs a spectrum, a waterfall, or an "is the line there?" scenario.
---

# Synthetic fixtures: the jansky-research "slice" pattern for this station

Every DSP/classifier test runs offline on a deterministic synthetic signal. No hardware, no sky,
no recorded data files — the fixture is generated inline in the test, seeded, and asserted on
**physical properties**, not exact arrays.

## Base signal: `jansky_observe.synthetic`

Start from the package's own generators: a noise floor plus a fake-HI line near
**1420.4057517667 MHz**, with configurable width, amplitude, and frequency offset. Seed
everything through `jansky.signals.rng` (the course library's seeded `np.random.Generator`
helper) so **the same seed produces identical output** — assert that in at least one test when
adding a new generator path.

```python
from jansky.signals import rng

from jansky_observe import synthetic

gen = rng(42)
# noise floor + HI line; see synthetic.py for the current signature
```

## Composing scenario extras (inline, in the test)

Build harder scenarios by composing on top of the base spectrum, right in the test body:

- **RFI spikes** — set a few narrow bins to a high value (single-bin or 2–3-bin spikes, tens of
  dB above the floor) to test that classifiers don't false-flag carriers as HI.
- **Baseline ripple** — add a slow sinusoid in dB across the band (period ≫ line width) to test
  baseline fitting/subtraction.
- **Frequency offsets** — shift the injected line center to fake v_LSR structure
  (±1.4 MHz spans ±300 km/s at 1420 MHz); test that peak-finding reports the shifted bin.

## Assert physics, not arrays

Never `assert_array_equal` against a golden array. Assert the properties that matter:

- **SNR in the line window** — peak power vs baseline RMS outside the window, above/below the
  intended threshold.
- **Peak bin location** — the detected peak lands at the injected offset (± a bin or two).
- **False-flag rates** — RFI-spike and no-line spectra do NOT produce a detection.
- **Determinism** — same seed ⇒ identical output (this one exact-equality check is the
  exception, and it's comparing the generator to itself).

## House style

- Fixtures live **inline in the test function** — no `conftest.py`, no fixture files on disk,
  no `tests/data/` for synthetic signals. A reader sees the whole scenario in one screen.
- Pure numpy/scipy; keep each fixture cheap (small `n_fft`, one spectrum unless the test is
  about accumulation).
- If a composition (e.g. an RFI-spike helper) is needed by 3+ tests, promote it into
  `jansky_observe.synthetic` with its own tests — not into a shared test helper.

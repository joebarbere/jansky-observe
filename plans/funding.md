# Funding plan — sustaining the jansky station buildout

Decision (2026-07-12): **GitHub Sponsors + Ko-fi**, under the GitHub handle
**`joebarbere`**. The pitch is **the jansky repos** (the trilogy: `jansky` the
library/course, `jansky-observe` the station software, `jansky-research` the science),
not the profile in general. Goal: a small revenue stream funding further station buildout.

## Why these two

- **GitHub Sponsors** — zero platform fees, the Sponsor button renders on every repo,
  one-time *and* monthly tiers, and the audience (people reading an open-source radio
  telescope repo) is already on GitHub. The must-have.
- **Ko-fi** — zero fee on one-off donations, no subscription pressure, familiar
  "buy me a coffee" impulse mechanics, takes ~10 minutes to set up. Complements Sponsors
  for non-GitHub audiences (Reddit r/RTLSDR, Mastodon, YouTube if guides ever get videos).
- Skipped: Buy Me a Coffee (duplicates Ko-fi, 5 % fee), Open Collective (transparent
  budgets are nice but ~10 % overhead and heavier admin — revisit if an institution ever
  wants to sponsor), Patreon (subscription-content treadmill; wrong shape for this).

## Step 1 — accounts (only Joe can do these; identity + payout verification)

**Done 2026-07-12 — both accounts created.**

1. **GitHub Sponsors**: <https://github.com/sponsors> → "Join the sponsors program" for
   the `joebarbere` account → complete the Stripe Connect onboarding (bank + identity) →
   fill the sponsor profile (see pitch below) → submit for approval (usually days).
2. **Ko-fi**: <https://ko-fi.com> → sign up (suggested page: `ko-fi.com/joebarbere`) →
   connect PayPal or Stripe for payouts → set the page copy (same pitch, shorter).

## Step 2 — wire the repos (Claude does this once accounts exist)

**Done 2026-07-12** — FUNDING.yml + README support sections landed in all three repos;
the GitHub profile README carries the same pitch.

- `.github/FUNDING.yml` in **jansky, jansky-observe, jansky-research**:

  ```yaml
  github: [joebarbere]
  ko_fi: joebarbere
  ```

- A short **"Support this project"** section in each README linking both, with the
  station-buildout framing (one paragraph, not a banner farm).
- Don't add FUNDING.yml before the accounts are live — a Sponsor button that 404s is
  worse than none.

## The pitch (sponsor profile / Ko-fi page)

Frame: *a working, open, rigorously-tested amateur radio telescope in the middle of
Philadelphia — software, science, and build guides all public.* Elements that convert:

- One paragraph on what exists: 700 mm dish on a rowhouse, hydrogen-line receiver,
  open-source observation software with a real release pipeline, research plans aimed at
  publishable rigor (12-month HI Doppler curve, drift-scan vs HI4PI, calibrated solar
  monitor).
- **Concrete, itemized goals** — hardware wishlist tied to what each unlocks (goals with
  numbers convert far better than a bare tip jar). Current buildout list, roughly
  ascending:
  - Pi 5 Active Cooler — removes the thermal soft-limit hit during 3 MSPS streaming.
  - Storage for IQ captures (SigMF runs 43 GB/h) — longer confirmation-grade recordings.
  - **Discovery Drive rotator** — automated pointing + tracking (roadmap M9).
  - Second Discovery Dish — the jansky-research plan 83 solar interferometer / Dicke
    reference for the plan-79 Doppler year.
  - KrakenSDR — 5-channel *coherent* receiver, the real interferometry path.
- Sponsor tiers: keep it simple — $3 (thanks + name in SUPPORTERS.md), $10 (same + input
  on the observing queue / guide requests), $25 (early access to build-guide PDFs — the
  M8 deliverable becomes a perk for free). One-time enabled.
- The M8 build guides and observation PDFs double as sponsor-facing material; each
  released guide is a natural "here's what your support built" update post.

## Sequencing

FUNDING.yml + README sections land as a small PR across the three repos as soon as both
accounts are approved — ping Claude with "funding accounts are live" and it's a
ten-minute change. No release needed (docs-only in observe; jansky/jansky-research have
no install surface).

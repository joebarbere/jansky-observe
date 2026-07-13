"""Guide PDFs — station build guide + observation guide (roadmap M8).

Rendered through the same WeasyPrint pipeline as the observation report. Two
house rules for every step-type guide PDF:

- **every step gets a checkbox** — the guides print as check-off-able sheets;
- a build stage's **diagram nodes are its parts** — the per-stage flow diagram
  (:func:`~jansky_observe.export.flowsvg.vertical_flow_svg`) is drawn from node
  labels that the stage's checkbox parts list repeats verbatim, so a reader maps
  box → part → checkbox.

The **build guide** is authored content (the plan's hardware chain: 700 mm dish →
KrakenRF H-line feed → inline USB-C bias-tee injector → Airspy Mini → Raspberry
Pi 5). It reinforces the station's non-negotiable safety invariant: the injector
powers the feed and the **Airspy internal bias tee stays OFF, always**.

The **observation guide** is generated from the seeded ObservationTypes and their
checklists so it stays in lockstep with the session wizard — its steps *are* the
wizard's checklist items.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from jansky_observe import __version__
from jansky_observe.export.flowsvg import vertical_flow_svg
from jansky_observe.models import (
    ChecklistTemplateItem,
    ObservationType,
    utcnow,
)

__all__ = [
    "GUIDE_KEYS",
    "Guide",
    "GuidePart",
    "GuideStage",
    "build_guide_model",
    "build_guide_pdf",
    "observation_guide_model",
]

#: The two guide keys the routes/PDF builder accept.
GUIDE_KEYS: tuple[str, ...] = ("build", "observation")


@dataclass(frozen=True)
class GuidePart:
    """One checkbox parts-list entry. ``label`` matches a diagram node label."""

    label: str
    note: str = ""


@dataclass(frozen=True)
class GuideStage:
    """One guide stage: an optional flow diagram, a parts list, and steps.

    ``nodes`` are the flow-diagram node labels (each a subset of ``parts``
    labels — the build-guide mapping rule). ``steps`` each render with a
    checkbox (the house rule). An observation-guide stage carries steps only.
    """

    title: str
    summary: str
    steps: tuple[str, ...]
    nodes: tuple[str, ...] = ()
    parts: tuple[GuidePart, ...] = ()


@dataclass(frozen=True)
class Guide:
    """A complete guide: an overview flow + ordered stages."""

    key: str
    title: str
    subtitle: str
    intro: str
    stages: tuple[GuideStage, ...]
    overview: tuple[str, ...] = field(default_factory=tuple)


def build_guide_model() -> Guide:
    """The station build guide (authored from the plan's hardware sections)."""
    stages = (
        GuideStage(
            title="Mount & dish",
            summary="Set up a stable, level alt-az mount and hang the 700 mm dish.",
            nodes=("Tripod / alt-az mount", "700 mm Discovery Dish", "Elevation angle gauge"),
            parts=(
                GuidePart("Tripod / alt-az mount"),
                GuidePart("700 mm Discovery Dish"),
                GuidePart("Elevation angle gauge"),
                GuidePart("Azimuth scale / compass reference"),
                GuidePart("Torpedo level", "tool — for plumbing the mast"),
            ),
            steps=(
                "Set the mount on a stable, level base with an open southern horizon.",
                "Plumb the mast with the torpedo level.",
                "Attach the dish to the mount and tighten fully.",
                "Zero the elevation angle gauge and mark an azimuth reference.",
            ),
        ),
        GuideStage(
            title="Feed at focus",
            summary="Mount the H-line feed at the dish focus and set its polarization.",
            nodes=("KrakenRF H-line feed (LNA + filter)", "Feed support / focus bracket"),
            parts=(
                GuidePart("KrakenRF H-line feed (LNA + filter)", "1420 MHz, ~120 mA"),
                GuidePart("Feed support / focus bracket"),
            ),
            steps=(
                "Mount the feed at the dish focus (f/D ≈ 0.38).",
                "Aim the feed phase-centre at the dish vertex; set the polarization.",
                "Route and strain-relieve the feed lead back toward the injector.",
            ),
        ),
        GuideStage(
            title="Bias-tee injector & SDR",
            summary=(
                "Power the feed from the inline injector — the Airspy internal bias tee "
                "stays OFF, always."
            ),
            nodes=("Inline USB-C bias-tee injector", "Airspy Mini"),
            parts=(
                GuidePart("Inline USB-C bias-tee injector", "supplies the feed's ~120 mA"),
                GuidePart("Airspy Mini"),
                GuidePart("Short SMA jumper"),
                GuidePart("USB cable", "injector power"),
            ),
            steps=(
                "Connect the feed output to the injector's RF+DC (feed) port.",
                "Connect the injector's RF-only port to the Airspy Mini RF input.",
                "Confirm the Airspy INTERNAL bias tee is OFF — the injector supplies the "
                "feed; the internal tee must never be enabled (it is not exposed anywhere "
                "in the software by design).",
                "Apply injector power; verify the feed LNA draws its expected current "
                "(~120 mA, not the ~50 mA of the internal tee alone).",
            ),
        ),
        GuideStage(
            title="Compute & case",
            summary="Seat the Pi 5 in its cooled case and flash the pinned OS.",
            nodes=("Raspberry Pi 5", "Argon ONE V2 case"),
            parts=(
                GuidePart("Raspberry Pi 5"),
                GuidePart("Argon ONE V2 case", "with cooling"),
                GuidePart("microSD or SSD"),
                GuidePart("USB-C power supply", "Pi 5, 27 W"),
            ),
            steps=(
                "Install the Pi 5 in the Argon ONE V2 case with its cooling seated.",
                'Flash Raspberry Pi OS Lite (64-bit) "Trixie" with SSH enabled.',
                "Connect the Airspy Mini to a USB port on the Pi.",
                "First boot; confirm SSH access to the Pi.",
            ),
        ),
        GuideStage(
            title="Network & software",
            summary="Put the Pi on the LAN, install the services, and calibrate pointing.",
            nodes=("PoE switch / access point", "Laptop or tablet on the LAN"),
            parts=(
                GuidePart("Ethernet cable"),
                GuidePart("PoE switch / access point", "rooftop networking"),
                GuidePart("Laptop or tablet on the LAN"),
            ),
            steps=(
                "Put the Pi on the LAN (ethernet to the PoE switch, or Wi-Fi).",
                "Run the install script: curl -fsSL …/install.sh | sudo bash.",
                "Browse to http://raspberrypi.local:8000 and confirm the live waterfall.",
                "Run the Sun pointing calibration before any pointed observation.",
            ),
        ),
    )
    return Guide(
        key="build",
        title="Station build guide",
        subtitle="Discovery Dish hydrogen-line telescope",
        intro=(
            "Build order for the station, feed to first light. Each stage lists its parts "
            "(check them off) and its steps (check them off as you go). The RF chain runs "
            "feed → injector → Airspy → Pi; the feed is powered only by the inline injector."
        ),
        stages=stages,
        overview=(
            "700 mm Discovery Dish",
            "KrakenRF H-line feed",
            "USB-C bias-tee injector",
            "Airspy Mini",
            "Raspberry Pi 5",
        ),
    )


def observation_guide_model(session: Session) -> Guide:
    """The observation guide, generated from the seeded ObservationTypes.

    Each ObservationType becomes a stage whose steps are its checklist items (in
    order, with required/recommended noted) — so the printed guide is exactly the
    wizard's checklist. Kept in the DB, not hard-coded, so the two never drift.
    """
    types = session.exec(select(ObservationType).order_by(col(ObservationType.id))).all()
    stages: list[GuideStage] = []
    for obs_type in types:
        items = session.exec(
            select(ChecklistTemplateItem)
            .where(ChecklistTemplateItem.observation_type_id == obs_type.id)
            .order_by(col(ChecklistTemplateItem.order_index))
        ).all()
        steps = tuple(
            f"{item.text}  ({'required' if item.required else 'recommended'})" for item in items
        )
        if not steps:
            steps = ("(no checklist items for this type)",)
        stages.append(GuideStage(title=obs_type.name, summary=obs_type.description, steps=steps))
    return Guide(
        key="observation",
        title="Observation guide",
        subtitle="Run a session end to end",
        intro=(
            "Every observing procedure the station knows, as printable checklists. The steps "
            "here are the same checklist items the session wizard walks you through — start "
            "with Sun pointing calibration, then work up the observing ladder."
        ),
        stages=tuple(stages),
        overview=("Plan", "Point", "Record", "Confirm", "Report"),
    )


def guide_model(kind: str, session: Session) -> Guide:
    """Return the guide model for ``kind`` (``"build"`` | ``"observation"``)."""
    if kind == "build":
        return build_guide_model()
    if kind == "observation":
        return observation_guide_model(session)
    raise ValueError(f"unknown guide {kind!r}; known: {GUIDE_KEYS}")


def _fmt_dt(value: datetime | None) -> str:
    """Render a datetime for the guide footer (UTC, minute precision)."""
    return "—" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


def build_guide_pdf(
    kind: str,
    engine: Engine,
    data_dir: str | Path,
    templates_dir: str | Path,
) -> Path:
    """Render a guide to ``<data_dir>/guides/<kind>.pdf`` and return its path.

    Parameters
    ----------
    kind : str
        ``"build"`` or ``"observation"``.
    engine : Engine
        The station database engine (the observation guide reads the seeds).
    data_dir : str or Path
        The station data directory; the PDF lands under ``guides/``.
    templates_dir : str or Path
        Directory containing ``guide.html``.

    Returns
    -------
    Path
        The written PDF's path (overwritten on rebuild).
    """
    from weasyprint import HTML

    data = Path(data_dir)
    with Session(engine) as session:
        guide = guide_model(kind, session)

    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=True)
    html = env.get_template("guide.html").render(
        guide=guide,
        flow_svg=vertical_flow_svg,
        generated_at=_fmt_dt(utcnow()),
        version=__version__,
    )

    out = data / "guides" / f"{kind}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(data)).write_pdf(str(out))
    return out

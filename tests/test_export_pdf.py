"""Tests for export.figures + export.pdf: report figures and the WeasyPrint PDF.

Fixtures follow the /synthetic-fixture pattern: a seeded tmp database
(``db.init_db``), real ``.npz`` captures from synthetic fake-HI IQ, and
Pillow-generated photos. WeasyPrint font noise is tolerated (logging
silenced, nothing asserted on stderr).
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jansky.signals import rng
from PIL import Image
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe import synthetic
from jansky_observe.astro.pointing import target_coord
from jansky_observe.capture.dsp import welch_psd_db
from jansky_observe.capture.writer import NpzCaptureWriter
from jansky_observe.db import init_db
from jansky_observe.export.figures import profile_figure, waterfall_figure
from jansky_observe.export.pdf import build_report, report_path
from jansky_observe.frames import SpectralFrame
from jansky_observe.models import (
    Capture,
    ChecklistItemState,
    ChecklistTemplateItem,
    ClassifierResult,
    Observation,
    ObservationObserver,
    Observer,
    Photo,
    RadioSource,
    utcnow,
)

CENTER_HZ, RATE_HZ = 1420.4e6, 3e6
LAT, LON, ELEV_M = 40.02, -75.16, 100.0
WHEN = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "jansky_observe" / "server" / "templates"
)


@pytest.fixture(autouse=True)
def _quiet_weasyprint() -> Iterator[None]:
    """Silence WeasyPrint/fontTools font warnings; never assert on stderr."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        levels = {}
        for name in ("weasyprint", "fontTools", "fontTools.subset"):
            logger = logging.getLogger(name)
            levels[name] = logger.level
            logger.setLevel(logging.ERROR)
        try:
            yield
        finally:
            for name, level in levels.items():
                logging.getLogger(name).setLevel(level)


def _write_capture(path: Path, n_frames: int = 8, n_fft: int = 256) -> Path:
    """Write an .npz capture of synthetic HI frames via the real writer."""
    gen = rng(7)
    writer = NpzCaptureWriter(path, settings={"gain": 15, "source": "synthetic"})
    samples_per_frame = n_fft * 32
    for i in range(n_frames):
        iq = synthetic.hi_iq_chunk(
            samples_per_frame,
            gen,
            t0_s=i * samples_per_frame / RATE_HZ,
            center_freq_hz=CENTER_HZ,
            sample_rate_hz=RATE_HZ,
        )
        writer.add_frame(
            SpectralFrame(
                seq=i,
                timestamp=1_750_000_000.0 + i * 0.5,
                center_freq_hz=CENTER_HZ,
                sample_rate_hz=RATE_HZ,
                power_db=welch_psd_db(iq, RATE_HZ, n_fft),
            )
        )
    return writer.close()


def _write_photo(path: Path, color: tuple[int, int, int]) -> Path:
    """Write a small solid-color JPEG (the ingest pipeline's output stand-in)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 240), color).save(path, "JPEG", quality=85)
    return path


# ---- figures ------------------------------------------------------------------------


class TestFigures:
    def test_profile_without_pointing(self, tmp_path: Path) -> None:
        npz = _write_capture(tmp_path / "capture.npz")
        out = profile_figure(npz, tmp_path / "figs" / "profile.png")
        assert out == tmp_path / "figs" / "profile.png"
        data = out.read_bytes()
        assert data.startswith(PNG_MAGIC)
        assert len(data) > 10_000  # a real plot, not an empty canvas

    def test_profile_with_pointing_dual_axes(self, tmp_path: Path) -> None:
        npz = _write_capture(tmp_path / "capture.npz")
        out = profile_figure(
            npz,
            tmp_path / "profile-vlsr.png",
            coord=target_coord("radec", ra_deg=299.8682, dec_deg=40.7339),
            lat=LAT,
            lon=LON,
            elev=ELEV_M,
            when=WHEN,
        )
        data = out.read_bytes()
        assert data.startswith(PNG_MAGIC)
        assert len(data) > 10_000

    def test_waterfall(self, tmp_path: Path) -> None:
        npz = _write_capture(tmp_path / "capture.npz")
        out = waterfall_figure(npz, tmp_path / "figs" / "waterfall.png")
        assert out == tmp_path / "figs" / "waterfall.png"
        data = out.read_bytes()
        assert data.startswith(PNG_MAGIC)
        assert len(data) > 10_000


# ---- report seeding helpers ---------------------------------------------------------


def _seeded_observation(
    engine: Engine,
    tmp_path: Path,
    *,
    with_capture: bool = True,
    with_photos: bool = True,
) -> int:
    """A done observation with checklist state, notes, weather — plus, on
    request, one classified .npz capture and a highlight + extra photo."""
    with Session(engine) as session:
        source = session.exec(
            select(RadioSource).where(RadioSource.name == "Cygnus region HI")
        ).one()
        template_item = session.exec(
            select(ChecklistTemplateItem).order_by(ChecklistTemplateItem.order_index)
        ).first()
        assert template_item is not None and source.id is not None
        observer = Observer(name="Joe")
        session.add(observer)
        observation = Observation(
            name="Cygnus HI first light",
            observation_type_id=template_item.observation_type_id,
            station_id=1,
            location_id=1,
            source_id=source.id,
            status="done",
            actual_start=WHEN,
            actual_end=WHEN.replace(hour=2),
            pointing_az_deg=123.0,
            pointing_el_deg=45.0,
            computed_az_deg=122.5,
            computed_el_deg=45.3,
            weather_snapshot={
                "provider": "nws",
                "now": {
                    "temp_c": 3.5,
                    "wind_speed_ms": 2.0,
                    "sky_cover_pct": 40,
                    "humidity_pct": 60,
                    "summary": "Partly cloudy",
                },
            },
            notes="Strong line at the expected v_LSR.\n\nNudged the dish once at 01:10.",
        )
        session.add(observation)
        session.commit()
        session.refresh(observation)
        session.refresh(observer)
        assert observation.id is not None and observer.id is not None
        obs_id = observation.id
        session.add(ObservationObserver(observation_id=obs_id, observer_id=observer.id))
        session.add(
            ChecklistItemState(
                observation_id=obs_id,
                template_item_id=template_item.id or 0,
                checked=True,
                checked_at=utcnow(),
                checked_by="Joe",
            )
        )

        if with_capture:
            npz = _write_capture(tmp_path / "captures" / "c.npz")
            capture = Capture(
                observation_id=obs_id,
                device="synthetic",
                path=str(npz),
                format="npz_spectra",
                size_bytes=npz.stat().st_size,
                start=WHEN,
                end=WHEN.replace(hour=1),
                sdr_settings={"gain": 15, "bias_tee": False},
            )
            session.add(capture)
            session.commit()
            session.refresh(capture)
            assert capture.id is not None
            session.add(
                ClassifierResult(
                    capture_id=capture.id,
                    name="hline_v1",
                    version="1",
                    verdict="detected",
                    score=8.2,
                    params={"peak_freq_hz": 1420.35e6, "window_source": "lsr"},
                    mode="post",
                )
            )

        if with_photos:
            highlight = _write_photo(
                tmp_path / "observations" / str(obs_id) / "photos" / "dish.jpg", (200, 60, 40)
            )
            extra = _write_photo(
                tmp_path / "observations" / str(obs_id) / "photos" / "sky.jpg", (40, 60, 200)
            )
            session.add(
                Photo(
                    observation_id=obs_id,
                    path=str(highlight),
                    caption="The dish at dusk",
                    is_highlight=True,
                )
            )
            session.add(Photo(observation_id=obs_id, path=str(extra), caption="Sky"))

        session.commit()
        return obs_id


# ---- build_report -------------------------------------------------------------------


class TestBuildReport:
    def test_full_report_builds_a_real_pdf(self, tmp_path: Path) -> None:
        engine = init_db(tmp_path)
        obs_id = _seeded_observation(engine, tmp_path)
        out = build_report(engine, obs_id, tmp_path, TEMPLATES_DIR)

        assert out == report_path(tmp_path, obs_id)
        data = out.read_bytes()
        assert data.startswith(b"%PDF")
        assert len(data) > 20_000  # figures + photos make it a real document

        figures_dir = tmp_path / "observations" / str(obs_id) / "figures"
        pngs = sorted(p.name for p in figures_dir.glob("*.png"))
        assert any(name.endswith("-profile.png") for name in pngs)
        assert any(name.endswith("-waterfall.png") for name in pngs)

    def test_rebuild_overwrites(self, tmp_path: Path) -> None:
        engine = init_db(tmp_path)
        obs_id = _seeded_observation(engine, tmp_path)
        first = build_report(engine, obs_id, tmp_path, TEMPLATES_DIR)
        second = build_report(engine, obs_id, tmp_path, TEMPLATES_DIR)
        assert first == second
        assert second.read_bytes().startswith(b"%PDF")

    def test_degrades_without_photos_or_captures(self, tmp_path: Path) -> None:
        engine = init_db(tmp_path)
        obs_id = _seeded_observation(engine, tmp_path, with_capture=False, with_photos=False)
        out = build_report(engine, obs_id, tmp_path, TEMPLATES_DIR)
        assert out.read_bytes().startswith(b"%PDF")

    def test_degrades_when_capture_file_missing(self, tmp_path: Path) -> None:
        engine = init_db(tmp_path)
        obs_id = _seeded_observation(engine, tmp_path)
        with Session(engine) as session:
            capture = session.exec(select(Capture)).one()
            Path(capture.path).unlink()
        out = build_report(engine, obs_id, tmp_path, TEMPLATES_DIR)
        assert out.read_bytes().startswith(b"%PDF")  # inventory row, no figures

    def test_unknown_observation_raises_lookup_error(self, tmp_path: Path) -> None:
        engine = init_db(tmp_path)
        with pytest.raises(LookupError, match="9999"):
            build_report(engine, 9999, tmp_path, TEMPLATES_DIR)

    def test_report_carries_calibration_epoch_and_kind(self, tmp_path: Path) -> None:
        """A capture stamped with a calibration epoch surfaces in the report
        context and PDF (roadmap M7)."""
        from jansky_observe.export.pdf import _gather_context
        from jansky_observe.models import CalibrationEpoch, Observation

        engine = init_db(tmp_path)
        obs_id = _seeded_observation(engine, tmp_path)
        with Session(engine) as session:
            epoch = CalibrationEpoch(notes="ref/cold/hot done")
            session.add(epoch)
            session.commit()
            session.refresh(epoch)
            capture = session.exec(select(Capture)).one()
            capture.kind = "cold_sky"
            capture.cal_epoch_id = epoch.id
            session.add(capture)
            session.commit()
            ctx = _gather_context(session, session.get(Observation, obs_id), tmp_path)
        assert [e.id for e in ctx["cal_epochs"]] == [epoch.id]
        assert ctx["captures"][0]["capture"].kind == "cold_sky"
        out = build_report(engine, obs_id, tmp_path, TEMPLATES_DIR)
        assert out.read_bytes().startswith(b"%PDF")

    def test_report_carries_vlsr_axis_figures(self, tmp_path: Path) -> None:
        """The profile figure for a pointed capture is the dual-axis variant."""
        engine = init_db(tmp_path)
        obs_id = _seeded_observation(engine, tmp_path)
        build_report(engine, obs_id, tmp_path, TEMPLATES_DIR)
        figures_dir = tmp_path / "observations" / str(obs_id) / "figures"
        profile = next(figures_dir.glob("*-profile.png"))
        # The pointed capture's profile differs from a plain single-axis
        # rendering — the dual v_LSR axis was drawn (plan §4.6).
        with Session(engine) as session:
            capture = session.exec(select(Capture)).one()
        plain = profile_figure(capture.path, tmp_path / "plain.png")
        assert profile.read_bytes() != plain.read_bytes()

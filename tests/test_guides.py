"""Tests for the guide PDFs (roadmap M8): content model, flow SVG, and routes."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.export.flowsvg import vertical_flow_svg
from jansky_observe.export.guides import (
    GUIDE_KEYS,
    build_guide_model,
    observation_guide_model,
)
from jansky_observe.models import ChecklistTemplateItem, ObservationType
from jansky_observe.server.app import create_app

DEAD_ENDPOINT = "tcp://127.0.0.1:1"


@pytest.fixture(autouse=True)
def _quiet_weasyprint() -> Iterator[None]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for name in ("weasyprint", "fontTools", "fontTools.subset"):
            logging.getLogger(name).setLevel(logging.ERROR)
        yield


@pytest.fixture()
def engine(tmp_path) -> Engine:
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    return TestClient(create_app(settings, engine=engine))


# ---- flow SVG -------------------------------------------------------------------


def test_flow_svg_is_well_formed_and_labels_nodes() -> None:
    svg = vertical_flow_svg(["Feed", "Injector", "Airspy"])
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    for label in ("Feed", "Injector", "Airspy"):
        assert f">{label}</text>" in svg
    assert "marker-end" in svg  # arrows join the nodes


def test_flow_svg_escapes_and_handles_empty() -> None:
    assert vertical_flow_svg([]) == ""
    assert "&amp;" in vertical_flow_svg(["A & B"])
    assert "marker-end" not in vertical_flow_svg(["only one"])  # no arrow for a lone node


# ---- build guide model ----------------------------------------------------------


def test_build_guide_nodes_are_a_subset_of_parts() -> None:
    guide = build_guide_model()
    assert guide.overview  # the RF-chain overview diagram
    for stage in guide.stages:
        part_labels = {p.label for p in stage.parts}
        assert set(stage.nodes) <= part_labels, f"{stage.title}: diagram node not in parts"
        assert stage.steps  # every stage has steps (each checkboxed in the template)


def test_build_guide_reinforces_bias_tee_off_invariant() -> None:
    guide = build_guide_model()
    text = " ".join(step for stage in guide.stages for step in stage.steps)
    summaries = " ".join(stage.summary for stage in guide.stages)
    assert "bias tee stays OFF" in summaries
    assert "internal tee must never be enabled" in text.lower()
    assert "internal bias tee is OFF" in text or "INTERNAL bias tee is OFF" in text


# ---- observation guide model (from the seeds) -----------------------------------


def test_observation_guide_tracks_the_seeded_checklists(engine: Engine) -> None:
    with Session(engine) as session:
        guide = observation_guide_model(session)
        types = session.exec(select(ObservationType)).all()
        # one stage per ObservationType
        assert len(guide.stages) == len(types)
        titles = {s.title for s in guide.stages}
        assert "Sun pointing calibration" in titles
        # a stage's steps count matches that type's checklist length
        sun = next(s for s in guide.stages if s.title == "Sun pointing calibration")
        sun_type = next(t for t in types if t.name == "Sun pointing calibration")
        n_items = len(
            session.exec(
                select(ChecklistTemplateItem).where(
                    ChecklistTemplateItem.observation_type_id == sun_type.id
                )
            ).all()
        )
        assert len(sun.steps) == n_items
        assert any("required" in step or "recommended" in step for step in sun.steps)


# ---- routes ---------------------------------------------------------------------


def test_guides_index_lists_both_guides(client: TestClient) -> None:
    body = client.get("/guides").text
    assert "/guides/build.pdf" in body
    assert "/guides/observation.pdf" in body


@pytest.mark.parametrize("kind", GUIDE_KEYS)
def test_guide_pdf_builds_and_serves(client: TestClient, kind: str) -> None:
    resp = client.get(f"/guides/{kind}.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    assert len(resp.content) > 8_000
    assert f"jansky-{kind}-guide.pdf" in resp.headers.get("content-disposition", "")


def test_unknown_guide_404(client: TestClient) -> None:
    assert client.get("/guides/nope.pdf").status_code == 404

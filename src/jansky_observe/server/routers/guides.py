"""Guide PDF endpoints (roadmap M8).

``GET /guides`` is a small index page linking the printable guides; ``GET
/guides/{kind}.pdf`` builds the guide through the WeasyPrint pipeline
(``export/guides.py``) and serves it. Guides are views of the plan + the seeded
checklists, rebuilt on every request — never a source of truth, so there is no
"build" step or stored state.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from jansky_observe.export.guides import GUIDE_KEYS, build_guide_pdf
from jansky_observe.server.routers import TEMPLATES

__all__ = ["router"]

router = APIRouter(tags=["guides"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

#: Display metadata for the index page (order matters).
_GUIDES = (
    ("build", "Station build guide", "Assemble the dish, feed, injector, SDR, and Pi — feed to first light."),
    ("observation", "Observation guide", "Every observing procedure as printable, check-off-able checklists."),
)


@router.get("/guides", response_class=HTMLResponse)
def guides_index(request: Request) -> HTMLResponse:
    """The guides index: a card per printable guide with a PDF download link."""
    return TEMPLATES.TemplateResponse(request, "guides.html", {"guides": _GUIDES})


@router.get("/guides/{kind}.pdf")
def guide_pdf(request: Request, kind: str) -> FileResponse:
    """Build and serve a guide PDF (``build`` | ``observation``)."""
    if kind not in GUIDE_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown guide {kind!r}")
    out = build_guide_pdf(
        kind,
        request.app.state.engine,
        request.app.state.settings.data_dir,
        _TEMPLATES_DIR,
    )
    return FileResponse(out, media_type="application/pdf", filename=f"jansky-{kind}-guide.pdf")

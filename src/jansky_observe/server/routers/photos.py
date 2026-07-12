"""Photo attachment routes: upload, highlight, caption, delete (plan §3, §5.3).

HTML: the htmx photos fragment on the observation detail page — multipart
upload (drag-drop, multiple files), per-photo caption edits, the
"make highlight" switch, and delete. Every POST re-renders ``_photos.html``
for an htmx swap.

The **exactly-one-highlight** invariant (plan §3) lives here: the first photo
of an observation becomes the highlight automatically, switching the highlight
clears every sibling's flag in the same transaction, and deleting the
highlight promotes the oldest survivor.

JSON: ``GET /api/observations/{id}/photos`` mirrors the fragment's data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlmodel import Session, col, select

from jansky_observe.models import Observation, Photo
from jansky_observe.photos import ingest_photo
from jansky_observe.server.routers import TEMPLATES, SessionDep, get_or_404

__all__ = ["router"]

router = APIRouter(tags=["photos"])


# ---- shared helpers ---------------------------------------------------------------


def observation_photos(session: Session, observation_id: int) -> list[Photo]:
    """The observation's photos, oldest first (highlight rendered first in the UI)."""
    rows = session.exec(
        select(Photo).where(Photo.observation_id == observation_id).order_by(col(Photo.id))
    ).all()
    return list(rows)


def _photo_in_observation(session: Session, obs_id: int, photo_id: int) -> Photo:
    """Fetch a photo, 404 unless it belongs to the observation."""
    photo = get_or_404(session, Photo, photo_id)
    if photo.observation_id != obs_id:
        raise HTTPException(status_code=404, detail="photo not in this observation")
    return photo


def _fragment(request: Request, session: Session, observation: Observation) -> HTMLResponse:
    """Render the ``_photos.html`` fragment for an htmx swap."""
    assert observation.id is not None  # fetched by primary key
    return TEMPLATES.TemplateResponse(
        request,
        "_photos.html",
        {"obs": observation, "photos": observation_photos(session, observation.id)},
    )


# ---- HTML routes (htmx fragment swaps) ----------------------------------------------


@router.get("/observations/{obs_id}/photos", response_class=HTMLResponse)
def photos_fragment(request: Request, session: SessionDep, obs_id: int) -> HTMLResponse:
    """The photos fragment (lazy-loaded into the detail page by htmx)."""
    return _fragment(request, session, get_or_404(session, Observation, obs_id))


@router.post("/observations/{obs_id}/photos", response_class=HTMLResponse)
async def photos_upload(
    request: Request,
    session: SessionDep,
    obs_id: int,
    files: Annotated[list[UploadFile], File()],
    caption: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Attach uploaded photos to the observation (plan §5.3: drag-drop, resized).

    Each file is resized on ingest (originals discarded, plan §3); the
    optional caption applies to every file in the batch. The first photo an
    observation ever gets becomes the highlight automatically. A non-image in
    the batch 400s the whole upload — nothing from the batch is kept.
    """
    observation = get_or_404(session, Observation, obs_id)
    data_dir = request.app.state.settings.data_dir
    has_highlight = any(p.is_highlight for p in observation_photos(session, obs_id))
    written: list[Path] = []
    for upload in files:
        if not upload.filename:  # empty file input submitted with no selection
            continue
        is_highlight = not has_highlight
        try:
            ingested = ingest_photo(
                await upload.read(), obs_id, data_dir, caption=caption, is_highlight=is_highlight
            )
        except ValueError as exc:
            for path in written:  # keep disk honest: drop the partial batch
                path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"{upload.filename}: {exc}") from exc
        written.append(ingested.path)
        session.add(
            Photo(
                observation_id=obs_id,
                path=str(ingested.path),
                caption=ingested.caption,
                is_highlight=is_highlight,
                taken_at=ingested.taken_at,
            )
        )
        has_highlight = True
    session.commit()
    return _fragment(request, session, observation)


@router.post("/observations/{obs_id}/photos/{photo_id}/highlight", response_class=HTMLResponse)
def photo_highlight(
    request: Request, session: SessionDep, obs_id: int, photo_id: int
) -> HTMLResponse:
    """Make this photo THE highlight (plan §3: exactly one per observation).

    Every sibling's flag is cleared and this one set in a single transaction,
    so the invariant holds no matter what the rows said before.
    """
    observation = get_or_404(session, Observation, obs_id)
    _photo_in_observation(session, obs_id, photo_id)
    for photo in observation_photos(session, obs_id):
        photo.is_highlight = photo.id == photo_id
        session.add(photo)
    session.commit()
    return _fragment(request, session, observation)


@router.post("/observations/{obs_id}/photos/{photo_id}/caption", response_class=HTMLResponse)
def photo_caption(
    request: Request,
    session: SessionDep,
    obs_id: int,
    photo_id: int,
    caption: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Update one photo's caption (inline edit on the detail page)."""
    observation = get_or_404(session, Observation, obs_id)
    photo = _photo_in_observation(session, obs_id, photo_id)
    photo.caption = caption
    session.add(photo)
    session.commit()
    return _fragment(request, session, observation)


@router.post("/observations/{obs_id}/photos/{photo_id}/delete", response_class=HTMLResponse)
def photo_delete(request: Request, session: SessionDep, obs_id: int, photo_id: int) -> HTMLResponse:
    """Delete a photo: remove the row and the file on disk.

    HTML-only tidy-up for the browser UI. Deliberately NOT exposed over MCP —
    the MCP surface carries no delete verbs of any kind (plan §12.4). If the
    deleted photo was the highlight, the oldest survivor is promoted so the
    exactly-one invariant (plan §3) still holds.
    """
    observation = get_or_404(session, Observation, obs_id)
    photo = _photo_in_observation(session, obs_id, photo_id)
    was_highlight = photo.is_highlight
    Path(photo.path).unlink(missing_ok=True)
    session.delete(photo)
    session.commit()
    if was_highlight:
        survivors = observation_photos(session, obs_id)
        if survivors:
            survivors[0].is_highlight = True
            session.add(survivors[0])
            session.commit()
    return _fragment(request, session, observation)


# ---- image bytes --------------------------------------------------------------------


@router.get("/photos/{photo_id}/image")
def photo_image(session: SessionDep, photo_id: int) -> FileResponse:
    """The resized JPEG itself; 404 when the file is gone from disk."""
    photo = get_or_404(session, Photo, photo_id)
    if not Path(photo.path).is_file():
        raise HTTPException(status_code=404, detail=f"photo file missing: {photo.path}")
    return FileResponse(photo.path, media_type="image/jpeg")


# ---- JSON API -----------------------------------------------------------------------


@router.get("/api/observations/{obs_id}/photos")
def api_observation_photos(session: SessionDep, obs_id: int) -> list[dict[str, Any]]:
    """The observation's photos, oldest first (mirror of the fragment)."""
    get_or_404(session, Observation, obs_id)
    return [
        {**photo.model_dump(), "image_url": f"/photos/{photo.id}/image"}
        for photo in observation_photos(session, obs_id)
    ]

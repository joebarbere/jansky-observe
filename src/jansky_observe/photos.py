"""Photo ingest: resize on arrival, originals discarded (plan §3, §5.3).

Every photo attached to an observation goes through :func:`ingest_photo`:
Pillow opens the upload (rejecting non-images), applies the EXIF orientation,
shrinks the longest edge to :data:`HIGHLIGHT_MAX_EDGE_PX` (the highlight, shown
large on the PDF) or :data:`OTHER_MAX_EDGE_PX` (everything else), and saves a
quality-:data:`JPEG_QUALITY` JPEG under
``<data_dir>/observations/<id>/photos/``. The original bytes are never written
to disk — the plan's "keep disk honest" rule. ``taken_at`` comes from the EXIF
``DateTimeOriginal`` when the camera recorded one, else the ingest time.

The exactly-one-highlight-per-observation invariant is enforced where the
:class:`~jansky_observe.models.Photo` rows are written — the photos router —
not here; this module only knows how large each role renders.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

__all__ = [
    "HIGHLIGHT_MAX_EDGE_PX",
    "JPEG_QUALITY",
    "OTHER_MAX_EDGE_PX",
    "IngestedPhoto",
    "ingest_photo",
    "photos_dir",
]

HIGHLIGHT_MAX_EDGE_PX = 2000
"""Longest-edge cap for the highlight photo (plan §3)."""

OTHER_MAX_EDGE_PX = 1200
"""Longest-edge cap for every non-highlight photo (plan §3)."""

JPEG_QUALITY = 85
"""JPEG quality every ingested photo is saved at (plan §3)."""

_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"
_TAG_DATETIME_ORIGINAL = ExifTags.Base.DateTimeOriginal  # 36867
_TAG_DATETIME = ExifTags.Base.DateTime  # 306


@dataclass(frozen=True)
class IngestedPhoto:
    """What :func:`ingest_photo` wrote: the resized JPEG + its metadata."""

    #: The resized JPEG on disk (the only copy — the original is discarded).
    path: Path
    #: EXIF ``DateTimeOriginal`` when present, else the ingest time (UTC).
    taken_at: datetime
    #: The caption passed through for the :class:`~jansky_observe.models.Photo` row.
    caption: str = ""


def photos_dir(data_dir: str | Path, observation_id: int) -> Path:
    """The directory an observation's photos live in (plan §3: files on disk)."""
    return Path(data_dir) / "observations" / str(observation_id) / "photos"


def _taken_at(image: Image.Image) -> datetime | None:
    """The EXIF capture time (UTC by convention), or ``None`` when absent."""
    exif = image.getexif()
    raw = (
        exif.get_ifd(ExifTags.IFD.Exif).get(_TAG_DATETIME_ORIGINAL)
        or exif.get(_TAG_DATETIME_ORIGINAL)
        or exif.get(_TAG_DATETIME)
    )
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw, _EXIF_DATETIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def ingest_photo(
    raw: bytes,
    observation_id: int,
    data_dir: str | Path,
    *,
    caption: str = "",
    is_highlight: bool = False,
) -> IngestedPhoto:
    """Resize an uploaded photo and write it to the observation's photo dir.

    The upload is decoded with Pillow, EXIF-rotated upright, shrunk (never
    enlarged) so its longest edge is at most :data:`HIGHLIGHT_MAX_EDGE_PX`
    (``is_highlight``) or :data:`OTHER_MAX_EDGE_PX`, converted to RGB, and
    saved as a quality-:data:`JPEG_QUALITY` JPEG named
    ``photo-<utcstamp>-<shortrand>.jpg``. The original bytes are discarded —
    only the resized JPEG ever touches disk (plan §3).

    Parameters
    ----------
    raw : bytes
        The uploaded file content.
    observation_id : int
        The observation the photo illustrates (names the directory).
    data_dir : str or Path
        The station data directory (``Settings.data_dir``).
    caption : str
        Caption passed through on the result for the ``Photo`` row.
    is_highlight : bool
        ``True`` sizes for the highlight role (larger on PDF/display).

    Returns
    -------
    IngestedPhoto
        The written path, the capture time (EXIF ``DateTimeOriginal`` when
        present, else now UTC), and the caption.

    Raises
    ------
    ValueError
        When ``raw`` is not a decodable image.
    """
    try:
        image = Image.open(BytesIO(raw))
        image.load()
    except Exception as exc:  # UnidentifiedImageError, truncated-file OSError, …
        raise ValueError(f"not a decodable image: {exc}") from exc

    taken_at = _taken_at(image) or datetime.now(UTC)
    upright = ImageOps.exif_transpose(image) or image  # None only for in_place=True
    max_edge = HIGHLIGHT_MAX_EDGE_PX if is_highlight else OTHER_MAX_EDGE_PX
    upright.thumbnail((max_edge, max_edge))  # shrink-only, aspect preserved
    if upright.mode != "RGB":
        upright = upright.convert("RGB")

    directory = photos_dir(data_dir, observation_id)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = directory / f"photo-{stamp}-{secrets.token_hex(3)}.jpg"
    upright.save(path, format="JPEG", quality=JPEG_QUALITY)
    return IngestedPhoto(path=path, taken_at=taken_at, caption=caption)

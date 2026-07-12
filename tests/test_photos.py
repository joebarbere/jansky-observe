"""Tests for photo ingest (resize/EXIF, plan §3) and the photos router (plan §5.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import ExifTags, Image
from sqlalchemy import Engine
from sqlmodel import Session, select

from jansky_observe.config import Settings
from jansky_observe.db import init_db
from jansky_observe.models import Observation, Photo
from jansky_observe.photos import ingest_photo
from jansky_observe.server.app import create_app
from jansky_observe.server.routers.photos import router as photos_router

DEAD_ENDPOINT = "tcp://127.0.0.1:1"


def _jpeg(size: tuple[int, int] = (3000, 2000), exif: Image.Exif | None = None) -> bytes:
    """A Pillow-generated JPEG of the given size (optionally with EXIF)."""
    buf = BytesIO()
    kwargs = {} if exif is None else {"exif": exif}
    Image.new("RGB", size, (40, 80, 120)).save(buf, format="JPEG", **kwargs)
    return buf.getvalue()


@pytest.fixture()
def engine(tmp_path) -> Engine:
    """A fully migrated + seeded database in a tmp dir."""
    return init_db(tmp_path)


@pytest.fixture()
def client(engine: Engine, tmp_path) -> TestClient:
    """A TestClient over an app with an injected engine + the photos router.

    ``create_app`` does not wire the photos router yet (that lands with the
    M4 app integration), so the test app includes it directly.
    """
    settings = Settings(zmq_endpoint=DEAD_ENDPOINT, data_dir=str(tmp_path))
    app = create_app(settings, engine=engine)
    app.include_router(photos_router)
    return TestClient(app)


def _create_observation(engine: Engine, name: str = "photo obs") -> int:
    """A minimal observation row over the seeded catalog (type/station/location/source 1)."""
    with Session(engine) as session:
        obs = Observation(
            name=name, observation_type_id=1, station_id=1, location_id=1, source_id=1
        )
        session.add(obs)
        session.commit()
        session.refresh(obs)
        assert obs.id is not None
        return obs.id


def _upload(
    client: TestClient, obs_id: int, *blobs: bytes, caption: str = ""
) -> list[dict[str, object]]:
    """POST photos and return the observation's JSON photo list."""
    resp = client.post(
        f"/observations/{obs_id}/photos",
        files=[("files", (f"p{i}.jpg", blob, "image/jpeg")) for i, blob in enumerate(blobs)],
        data={"caption": caption},
    )
    assert resp.status_code == 200
    return client.get(f"/api/observations/{obs_id}/photos").json()


# ---- ingest_photo -----------------------------------------------------------------


def test_ingest_resizes_highlight_to_2000(tmp_path) -> None:
    result = ingest_photo(_jpeg((3000, 2000)), 1, tmp_path, is_highlight=True)
    with Image.open(result.path) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 2000
        assert img.size[0] / img.size[1] == pytest.approx(1.5, rel=1e-2)


def test_ingest_resizes_other_to_1200(tmp_path) -> None:
    result = ingest_photo(_jpeg((3000, 2000)), 1, tmp_path)
    with Image.open(result.path) as img:
        assert img.format == "JPEG"
        assert img.size == (1200, 800)
    assert result.path.parent == tmp_path / "observations" / "1" / "photos"
    assert result.path.name.startswith("photo-")
    assert result.path.suffix == ".jpg"


def test_ingest_never_enlarges(tmp_path) -> None:
    result = ingest_photo(_jpeg((640, 480)), 1, tmp_path)
    with Image.open(result.path) as img:
        assert img.size == (640, 480)


def test_ingest_honors_exif_orientation(tmp_path) -> None:
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6  # camera rotated: stored landscape, is portrait
    result = ingest_photo(_jpeg((300, 200), exif=exif), 1, tmp_path)
    with Image.open(result.path) as img:
        assert img.size == (200, 300)


def test_ingest_taken_at_from_exif_datetime_original(tmp_path) -> None:
    exif = Image.Exif()
    exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = "2026:07:10 12:34:56"
    result = ingest_photo(_jpeg((300, 200), exif=exif), 1, tmp_path)
    assert result.taken_at == datetime(2026, 7, 10, 12, 34, 56, tzinfo=UTC)


def test_ingest_taken_at_defaults_to_now(tmp_path) -> None:
    before = datetime.now(UTC)
    result = ingest_photo(_jpeg((300, 200)), 1, tmp_path)
    assert before <= result.taken_at <= datetime.now(UTC)


def test_ingest_rejects_non_image(tmp_path) -> None:
    with pytest.raises(ValueError, match="not a decodable image"):
        ingest_photo(b"definitely not an image", 1, tmp_path)
    assert not (tmp_path / "observations").exists()  # nothing written


def test_ingest_carries_caption(tmp_path) -> None:
    result = ingest_photo(_jpeg((300, 200)), 1, tmp_path, caption="the dish at dusk")
    assert result.caption == "the dish at dusk"


# ---- upload endpoint ----------------------------------------------------------------


def test_upload_creates_rows_and_files_first_is_highlight(
    client: TestClient, engine: Engine, tmp_path
) -> None:
    obs_id = _create_observation(engine)
    photos = _upload(client, obs_id, _jpeg((3000, 2000)), _jpeg((3000, 2000)), caption="setup")
    assert len(photos) == 2
    assert [p["is_highlight"] for p in photos] == [True, False]
    assert all(p["caption"] == "setup" for p in photos)
    for photo, max_edge in zip(photos, (2000, 1200), strict=True):
        path = Path(str(photo["path"]))
        assert path.is_file()
        assert path.parent == tmp_path / "observations" / str(obs_id) / "photos"
        with Image.open(path) as img:
            assert max(img.size) == max_edge  # first photo sized for the highlight role


def test_upload_second_batch_keeps_existing_highlight(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(engine)
    _upload(client, obs_id, _jpeg((300, 200)))
    photos = _upload(client, obs_id, _jpeg((300, 200)))
    assert [p["is_highlight"] for p in photos] == [True, False]


def test_upload_rejects_non_image(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(engine)
    resp = client.post(
        f"/observations/{obs_id}/photos",
        files=[("files", ("junk.jpg", b"not an image", "image/jpeg"))],
    )
    assert resp.status_code == 400
    assert "junk.jpg" in resp.json()["detail"]
    assert client.get(f"/api/observations/{obs_id}/photos").json() == []


def test_upload_unknown_observation_404s(client: TestClient) -> None:
    resp = client.post(
        "/observations/9999/photos",
        files=[("files", ("p.jpg", _jpeg((300, 200)), "image/jpeg"))],
    )
    assert resp.status_code == 404


# ---- highlight invariant ------------------------------------------------------------


def test_highlight_switch_keeps_exactly_one(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(engine)
    photos = _upload(client, obs_id, _jpeg((300, 200)), _jpeg((300, 200)), _jpeg((300, 200)))
    target = photos[2]["id"]
    resp = client.post(f"/observations/{obs_id}/photos/{target}/highlight")
    assert resp.status_code == 200
    photos = client.get(f"/api/observations/{obs_id}/photos").json()
    assert [p["id"] for p in photos if p["is_highlight"]] == [target]
    with Session(engine) as session:
        flags = [p.is_highlight for p in session.exec(select(Photo)).all()]
    assert flags.count(True) == 1


def test_delete_highlight_promotes_oldest_survivor(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(engine)
    photos = _upload(client, obs_id, _jpeg((300, 200)), _jpeg((300, 200)))
    resp = client.post(f"/observations/{obs_id}/photos/{photos[0]['id']}/delete")
    assert resp.status_code == 200
    photos = client.get(f"/api/observations/{obs_id}/photos").json()
    assert [p["is_highlight"] for p in photos] == [True]


# ---- caption / delete / image / JSON mirror -----------------------------------------


def test_caption_update(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(engine)
    photo_id = _upload(client, obs_id, _jpeg((300, 200)))[0]["id"]
    resp = client.post(
        f"/observations/{obs_id}/photos/{photo_id}/caption", data={"caption": "feed arm shadow"}
    )
    assert resp.status_code == 200
    assert "feed arm shadow" in resp.text
    photos = client.get(f"/api/observations/{obs_id}/photos").json()
    assert photos[0]["caption"] == "feed arm shadow"


def test_delete_removes_row_and_file(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(engine)
    photo = _upload(client, obs_id, _jpeg((300, 200)))[0]
    path = Path(str(photo["path"]))
    assert path.is_file()
    resp = client.post(f"/observations/{obs_id}/photos/{photo['id']}/delete")
    assert resp.status_code == 200
    assert not path.exists()
    assert client.get(f"/api/observations/{obs_id}/photos").json() == []


def test_photo_routes_404_across_observations(client: TestClient, engine: Engine) -> None:
    obs_a = _create_observation(engine, "a")
    obs_b = _create_observation(engine, "b")
    photo_id = _upload(client, obs_a, _jpeg((300, 200)))[0]["id"]
    assert client.post(f"/observations/{obs_b}/photos/{photo_id}/highlight").status_code == 404
    assert client.post(f"/observations/{obs_b}/photos/{photo_id}/delete").status_code == 404


def test_image_route_serves_jpeg(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(engine)
    photo = _upload(client, obs_id, _jpeg((300, 200)))[0]
    resp = client.get(f"/photos/{photo['id']}/image")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    with Image.open(BytesIO(resp.content)) as img:
        assert img.format == "JPEG"
    # gone from disk → 404
    Path(str(photo["path"])).unlink()
    assert client.get(f"/photos/{photo['id']}/image").status_code == 404


def test_json_mirror_shape(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(engine)
    exif = Image.Exif()
    exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = "2026:07:10 12:34:56"
    _upload(client, obs_id, _jpeg((300, 200), exif=exif), caption="dish")
    photos = client.get(f"/api/observations/{obs_id}/photos").json()
    assert len(photos) == 1
    photo = photos[0]
    assert photo["observation_id"] == obs_id
    assert photo["caption"] == "dish"
    assert photo["is_highlight"] is True
    # read back naive-UTC by convention (models.py: SQLite drops the tz)
    assert photo["taken_at"] == "2026-07-10T12:34:56"
    assert photo["image_url"] == f"/photos/{photo['id']}/image"
    assert {"id", "path", "created_at"} <= photo.keys()
    assert client.get("/api/observations/9999/photos").status_code == 404


def test_fragment_and_detail_page_render(client: TestClient, engine: Engine) -> None:
    obs_id = _create_observation(engine)
    _upload(client, obs_id, _jpeg((300, 200)))
    fragment = client.get(f"/observations/{obs_id}/photos")
    assert fragment.status_code == 200
    assert "drop-zone" in fragment.text
    assert "Make highlight" not in fragment.text  # sole photo is the highlight
    page = client.get(f"/observations/{obs_id}")
    assert page.status_code == 200
    assert f"/observations/{obs_id}/report" in page.text
    assert f"/observations/{obs_id}/report.pdf" in page.text

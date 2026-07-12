"""The session start wizard (plan §5.1) — four steps, state in the Observation row.

Steps 1–2 carry their form fields forward as hidden inputs (nothing is
persisted until the source is chosen); the Observation row is created at
"create" with status ``planned`` and *is* the wizard state from then on — no
server-side session machinery. Step 3 records the dialed az/el; step 4 is the
checklist, whose Start button posts to the shared
``POST /observations/{id}/start`` transition.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, col, select

from jansky_observe.astro.stellarium import (
    StellariumClient,
    StellariumUnavailable,
    angular_separation_deg,
)
from jansky_observe.models import (
    ChecklistItemState,
    ChecklistTemplateItem,
    Location,
    Observation,
    ObservationObserver,
    ObservationType,
    Observer,
    RadioSource,
    utcnow,
)
from jansky_observe.server.routers import (
    TEMPLATES,
    SessionDep,
    default_location,
    default_station,
    get_or_404,
    source_pointing,
    weather_or_none,
)
from jansky_observe.server.routers.observations import (
    checklist_rows,
    observation_observers,
    required_items_ticked,
)

__all__ = ["router"]

router = APIRouter(prefix="/wizard", tags=["wizard"])


def _default_name(obs_type: ObservationType) -> str:
    """The default observation name: ``"<type> — <date>"`` (plan §5.1 step 1)."""
    return f"{obs_type.name} — {utcnow().date().isoformat()}"


def _step1_fields(session: Session) -> dict[str, Any]:
    """Pickers for step 1: types (with default names), observers, locations."""
    types = session.exec(select(ObservationType).order_by(col(ObservationType.id))).all()
    return {
        "types": types,
        "default_names": {t.id: _default_name(t) for t in types},
        "observers": session.exec(select(Observer).order_by(Observer.name)).all(),
        "locations": session.exec(select(Location).order_by(Location.name)).all(),
        "default_location": default_location(session),
    }


@router.get("", response_class=HTMLResponse)
def step1(request: Request, session: SessionDep) -> HTMLResponse:
    """Step 1: name, ObservationType, observers (multi), location (default Home)."""
    return TEMPLATES.TemplateResponse(request, "wizard_step1.html", _step1_fields(session))


@router.post("/step2", response_class=HTMLResponse)
def step2(
    request: Request,
    session: SessionDep,
    observation_type_id: Annotated[int, Form()],
    location_id: Annotated[int, Form()],
    name: Annotated[str, Form()] = "",
    observer_ids: Annotated[list[int] | None, Form()] = None,
) -> HTMLResponse:
    """Step 2: pick a source; pointing + weather load into the page via htmx."""
    obs_type = get_or_404(session, ObservationType, observation_type_id)
    sources = session.exec(select(RadioSource).order_by(RadioSource.name)).all()
    return TEMPLATES.TemplateResponse(
        request,
        "wizard_step2.html",
        {
            "name": name.strip() or _default_name(obs_type),
            "obs_type": obs_type,
            "location_id": location_id,
            "observer_ids": observer_ids or [],
            "sources": sources,
        },
    )


@router.get("/pointing", response_class=HTMLResponse)
def pointing_fragment(
    request: Request, session: SessionDep, source_id: int, location_id: int
) -> HTMLResponse:
    """htmx fragment: current az/el (offsets applied + raw), drift, transit,
    beam-crossing, rise/set, and weather now + next 3 h (plan §5.1 step 2).

    Weather degrades gracefully to "weather unavailable" — the wizard never
    blocks on the network.
    """
    source = get_or_404(session, RadioSource, source_id)
    location = get_or_404(session, Location, location_id)
    station = default_station(session)
    pointing = source_pointing(source, location, station)
    weather = weather_or_none(location.lat_deg, location.lon_deg, hours=3)
    return TEMPLATES.TemplateResponse(
        request,
        "_pointing_info.html",
        {
            "pointing": pointing,
            "weather": weather,
            "source": source,
            "location_id": location_id,
            "stellarium_url": station.stellarium_url,
        },
    )


_STELLARIUM_AGREE_DEG = 0.3
_STELLARIUM_WARN_DEG = 1.0


@router.post("/stellarium/show", response_class=HTMLResponse)
def stellarium_show(
    request: Request,
    session: SessionDep,
    source_id: Annotated[int, Form()],
    location_id: Annotated[int, Form()],
) -> HTMLResponse:
    """htmx fragment: slew the desktop Stellarium view to the source (plan §4.3).

    Focuses the source by name when Stellarium's catalogs know it, then reads
    back Stellarium's az/alt and shows the delta against astropy's — agreement
    within 0.3° is the happy path, above 1° a warning; **astropy is
    authoritative** either way. Pseudo-sources Stellarium cannot find (e.g.
    the HI regions) fall back to a plain view slew at the computed az/el.
    A dead or unreachable Stellarium renders an inline message — the wizard
    never blocks on it.
    """
    source = get_or_404(session, RadioSource, source_id)
    location = get_or_404(session, Location, location_id)
    station = default_station(session)
    if not station.stellarium_url:
        raise HTTPException(status_code=409, detail="no stellarium_url configured on the station")
    pointing = source_pointing(source, location, station, full=False)
    client = StellariumClient(station.stellarium_url)
    context: dict[str, Any] = {
        "source": source,
        "az_deg": pointing["raw_az_deg"],
        "el_deg": pointing["raw_el_deg"],
    }
    try:
        if client.find_object(source.name) is not None and client.focus(source.name):
            info = client.object_info(source.name)
            az, alt = info.get("azimuth"), info.get("altitude")
            if az is not None and alt is not None:
                delta = angular_separation_deg(
                    float(az), float(alt), pointing["raw_az_deg"], pointing["raw_el_deg"]
                )
                context.update(
                    {
                        "mode": "focused",
                        "delta_deg": delta,
                        "agree": delta <= _STELLARIUM_AGREE_DEG,
                        "warn": delta > _STELLARIUM_WARN_DEG,
                    }
                )
            else:
                context["mode"] = "focused_no_info"
        else:
            client.slew_view(pointing["raw_az_deg"], pointing["raw_el_deg"])
            context["mode"] = "slewed"
    except StellariumUnavailable as exc:
        context.update({"mode": "unavailable", "error": str(exc)})
    return TEMPLATES.TemplateResponse(request, "_stellarium_result.html", context)


@router.post("/create")
def create(
    session: SessionDep,
    observation_type_id: Annotated[int, Form()],
    location_id: Annotated[int, Form()],
    source_id: Annotated[int, Form()],
    name: Annotated[str, Form()] = "",
    observer_ids: Annotated[list[int] | None, Form()] = None,
) -> RedirectResponse:
    """Create the planned Observation (the wizard state row) and go to step 3.

    Materializes the type's checklist template into
    :class:`ChecklistItemState` rows and links the chosen observers.
    """
    obs_type = get_or_404(session, ObservationType, observation_type_id)
    get_or_404(session, RadioSource, source_id)
    get_or_404(session, Location, location_id)
    station = default_station(session)
    observation = Observation(
        name=name.strip() or _default_name(obs_type),
        observation_type_id=observation_type_id,
        station_id=station.id or 0,
        location_id=location_id,
        source_id=source_id,
        status="planned",
    )
    session.add(observation)
    session.flush()
    assert observation.id is not None
    for observer_id in observer_ids or []:
        get_or_404(session, Observer, observer_id)
        session.add(ObservationObserver(observation_id=observation.id, observer_id=observer_id))
    template_items = session.exec(
        select(ChecklistTemplateItem)
        .where(ChecklistTemplateItem.observation_type_id == observation_type_id)
        .order_by(col(ChecklistTemplateItem.order_index))
    ).all()
    for item in template_items:
        assert item.id is not None
        session.add(ChecklistItemState(observation_id=observation.id, template_item_id=item.id))
    session.commit()
    return RedirectResponse(f"/wizard/{observation.id}/step3", status_code=303)


def _planned_or_redirect(
    session: Session, obs_id: int
) -> tuple[Observation, RedirectResponse | None]:
    """Load the observation; non-planned ones bounce to their detail page."""
    observation = get_or_404(session, Observation, obs_id)
    if observation.status != "planned":
        return observation, RedirectResponse(f"/observations/{obs_id}", status_code=303)
    return observation, None


@router.get("/{obs_id}/step3", response_class=HTMLResponse, response_model=None)
def step3(request: Request, session: SessionDep, obs_id: int) -> HTMLResponse | RedirectResponse:
    """Step 3: record the dialed az/el, pre-filled with the computed values."""
    observation, bounce = _planned_or_redirect(session, obs_id)
    if bounce is not None:
        return bounce
    source = get_or_404(session, RadioSource, observation.source_id)
    location = get_or_404(session, Location, observation.location_id)
    station = default_station(session)
    pointing = source_pointing(source, location, station, full=False)
    return TEMPLATES.TemplateResponse(
        request,
        "wizard_step3.html",
        {
            "obs": observation,
            "source": source,
            "pointing": pointing,
            "az_value": observation.pointing_az_deg
            if observation.pointing_az_deg is not None
            else pointing["az_deg"],
            "el_value": observation.pointing_el_deg
            if observation.pointing_el_deg is not None
            else pointing["el_deg"],
        },
    )


@router.post("/{obs_id}/step3")
def step3_save(
    session: SessionDep,
    obs_id: int,
    pointing_az_deg: Annotated[float, Form()],
    pointing_el_deg: Annotated[float, Form()],
) -> RedirectResponse:
    """Save the dialed az/el and continue to the checklist."""
    observation, bounce = _planned_or_redirect(session, obs_id)
    if bounce is not None:
        return bounce
    observation.pointing_az_deg = pointing_az_deg
    observation.pointing_el_deg = pointing_el_deg
    observation.updated_at = utcnow()
    session.add(observation)
    session.commit()
    return RedirectResponse(f"/wizard/{obs_id}/step4", status_code=303)


@router.get("/{obs_id}/step4", response_class=HTMLResponse, response_model=None)
def step4(request: Request, session: SessionDep, obs_id: int) -> HTMLResponse | RedirectResponse:
    """Step 4: the checklist — required items gate the Start button (plan §5.1)."""
    observation, bounce = _planned_or_redirect(session, obs_id)
    if bounce is not None:
        return bounce
    observers = observation_observers(session, observation)
    if not observers:
        observers = list(session.exec(select(Observer).order_by(Observer.name)).all())
    return TEMPLATES.TemplateResponse(
        request,
        "wizard_step4.html",
        {
            "obs": observation,
            "observers": observers,
            "checklist": checklist_rows(session, observation),
            "required_ok": required_items_ticked(session, observation),
            "view": "wizard",
        },
    )

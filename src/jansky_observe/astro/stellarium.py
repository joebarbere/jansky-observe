"""Stellarium RemoteControl client (plan §4.3): a visual aid + sanity check.

The desktop Stellarium's RemoteControl plugin serves an HTTP API on port
8090. The server uses it to (a) slew the Stellarium *view* to the session's
source so the desktop shows a finder image (plan §5.1 step 2) and (b) read
back Stellarium's az/alt for the object as a cross-check. **Astropy is
authoritative** for every pointing number (plan §4.3) — Stellarium being
unreachable or disagreeing never blocks anything; callers catch
:class:`StellariumUnavailable` and degrade.

Units — verified against the RemoteControl API description
(https://stellarium.org/doc/24.0/remoteControlApi.html) and
``plugins/RemoteControl/src/MainService.cpp`` in the Stellarium 24.1 source:

- ``POST /api/main/view`` scalar ``az``/``alt`` parameters are **radians**
  ("az and alt must be given in radians"), and the azimuth lands directly in
  Stellarium's internal alt-az frame, which counts azimuth **from South
  toward East** — the docs give ``Az' = 180 − Az`` for the ``altAz`` vector,
  and ``MainService::updateView`` feeds the scalar ``az`` straight into that
  frame via ``spheToRect``. So ``az_stellarium_rad = radians(180° − az_north)``.
- ``GET /api/objects/info?format=json`` returns ``azimuth``/``altitude`` in
  **degrees** with azimuth from North (``StelObject::getInfoMap``), matching
  astropy's AltAz convention.
- ``POST /api/main/focus`` takes ``target`` (an object name) and answers a
  plain-text ``"true"``/``"false"`` for whether the object was found.
"""

from __future__ import annotations

import math
from typing import Any

import httpx

__all__ = [
    "StellariumClient",
    "StellariumUnavailable",
    "angular_separation_deg",
]

_TIMEOUT_S = 5.0


class StellariumUnavailable(Exception):
    """Stellarium's RemoteControl API could not be reached (or errored)."""


def angular_separation_deg(az1_deg: float, el1_deg: float, az2_deg: float, el2_deg: float) -> float:
    """Great-circle separation between two az/el directions, in degrees.

    Used for the Stellarium-vs-astropy pointing cross-check (plan §4.3);
    a plain Δaz would overstate the separation near the zenith.

    Parameters
    ----------
    az1_deg, el1_deg : float
        First direction (degrees, azimuth from North).
    az2_deg, el2_deg : float
        Second direction (degrees, azimuth from North).

    Returns
    -------
    float
        Separation in degrees, in ``[0, 180]``.
    """
    el1 = math.radians(el1_deg)
    el2 = math.radians(el2_deg)
    d_az = math.radians(az2_deg - az1_deg)
    cos_sep = math.sin(el1) * math.sin(el2) + math.cos(el1) * math.cos(el2) * math.cos(d_az)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


class StellariumClient:
    """A small client for one Stellarium RemoteControl endpoint.

    Parameters
    ----------
    base_url : str
        The RemoteControl base URL from ``Station.stellarium_url``,
        e.g. ``"http://desktop:8090"``.
    client : httpx.Client, optional
        Injectable HTTP client — every request goes through it, so tests
        pass one built on ``httpx.MockTransport`` and never hit the network.
        The default client uses a short timeout: Stellarium sits on the
        desktop LAN and the wizard must never hang on it.
    """

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=_TIMEOUT_S)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One request; every transport failure becomes :class:`StellariumUnavailable`."""
        try:
            return self._client.request(method, f"{self._base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise StellariumUnavailable(
                f"Stellarium unreachable at {self._base_url}: {exc}"
            ) from exc

    def find_object(self, name: str) -> dict[str, Any] | None:
        """Look a name up in Stellarium's catalogs (``GET /api/objects/find``).

        Parameters
        ----------
        name : str
            Search string (the RadioSource name, e.g. ``"Cygnus A"``).

        Returns
        -------
        dict or None
            ``{"name": <best match>, "matches": [<all matches>]}`` — the API
            itself returns a JSON string array — or ``None`` when Stellarium
            has no match (pseudo-sources like the HI regions never match).
        """
        response = self._request("GET", "/api/objects/find", params={"str": name})
        if response.status_code != 200:
            return None
        matches = response.json()
        if not matches:
            return None
        return {"name": matches[0], "matches": list(matches)}

    def object_info(self, name: str) -> dict[str, Any]:
        """Object details (``GET /api/objects/info?format=json``).

        Parameters
        ----------
        name : str
            An object name Stellarium knows (from :meth:`find_object`).

        Returns
        -------
        dict
            The ``StelObject::getInfoMap`` JSON — notably ``azimuth`` and
            ``altitude`` in degrees (azimuth from North) — or ``{}`` when
            Stellarium answers but does not know the object.
        """
        response = self._request(
            "GET", "/api/objects/info", params={"name": name, "format": "json"}
        )
        if response.status_code != 200:
            return {}
        data: dict[str, Any] = response.json()
        return data

    def slew_view(self, az_deg: float, alt_deg: float) -> None:
        """Slew the Stellarium *view* to an az/el (``POST /api/main/view``).

        Converts from the astropy convention (degrees, azimuth from North)
        to what the API wants: radians, azimuth from South toward East
        (``az' = 180° − az``) — see the module docstring for the paper trail.

        Parameters
        ----------
        az_deg, alt_deg : float
            Target direction in degrees, azimuth from North (astropy AltAz).

        Raises
        ------
        StellariumUnavailable
            On transport failure or a non-OK answer.
        """
        response = self._request(
            "POST",
            "/api/main/view",
            data={
                "az": str(math.radians(180.0 - az_deg)),
                "alt": str(math.radians(alt_deg)),
            },
        )
        if response.status_code != 200:
            raise StellariumUnavailable(
                f"Stellarium view slew failed ({response.status_code}): {response.text!r}"
            )

    def focus(self, target: str) -> bool:
        """Focus (select + center) an object by name (``POST /api/main/focus``).

        Parameters
        ----------
        target : str
            An object name Stellarium knows.

        Returns
        -------
        bool
            ``True`` when Stellarium found and focused the object — the API
            answers a plain-text ``"true"``/``"false"``.
        """
        response = self._request("POST", "/api/main/focus", data={"target": target})
        return response.status_code == 200 and response.text.strip() == "true"

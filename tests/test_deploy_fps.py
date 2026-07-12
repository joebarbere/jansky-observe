"""The --fps operator knob is wired through deploy/ (roadmap M6).

The daemon's ``--fps`` flag is exercised in ``test_daemon``; here we guard that
the installed station can actually set it from ``/etc/default/jansky-observe``:
the capture unit passes ``--fps ${JANSKY_OBSERVE_FPS}`` with a default, and the
default-file the installer writes documents the knob. The unit copy embedded in
``install.sh`` must match ``deploy/systemd/`` (CI enforces the byte-level drift
check separately).
"""

from __future__ import annotations

from pathlib import Path

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
_CAPTURE_UNIT = _DEPLOY / "systemd" / "jansky-observe-capture.service"
_INSTALL_SH = _DEPLOY / "install.sh"


def test_capture_unit_passes_fps_env() -> None:
    unit = _CAPTURE_UNIT.read_text()
    assert "Environment=JANSKY_OBSERVE_FPS=4.0" in unit
    assert "--fps ${JANSKY_OBSERVE_FPS}" in unit
    # The source knob still works alongside it.
    assert "--source ${JANSKY_OBSERVE_SOURCE}" in unit


def test_install_embeds_matching_fps_wiring() -> None:
    install = _INSTALL_SH.read_text()
    # The embedded unit heredoc mirrors the deploy copy (drift check territory).
    assert "Environment=JANSKY_OBSERVE_FPS=4.0" in install
    assert "--fps ${JANSKY_OBSERVE_FPS}" in install
    # And the default file it writes documents the knob for the operator.
    assert "JANSKY_OBSERVE_FPS=4.0" in install
    assert "spectrometer cadence" in install  # the recorded rationale

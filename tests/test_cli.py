"""Tests for the ``jansky-observe`` console entry point."""

from __future__ import annotations

import pytest

from jansky_observe import __version__
from jansky_observe.server import cli


def test_version_prints_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "jansky-observe" in out
    assert __version__ in out


def test_bad_flag_errors_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--no-such-flag"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "usage:" in err


def test_bad_port_errors_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--port", "not-a-port"])
    assert excinfo.value.code == 2


def test_main_runs_uvicorn_with_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    assert cli.main(["--host", "127.0.0.1", "--port", "1234"]) == 0
    assert calls["app"] == "jansky_observe.server.app:app"
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 1234


def test_defaults_come_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JANSKY_OBSERVE_HOST", "10.0.0.5")
    monkeypatch.setenv("JANSKY_OBSERVE_PORT", "9001")
    args = cli.build_parser().parse_args([])
    assert args.host == "10.0.0.5"
    assert args.port == 9001

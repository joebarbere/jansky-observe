"""Smoke tests: the package imports, versions agree, and the jansky dependency resolves."""

from __future__ import annotations

import tomllib
from pathlib import Path

import jansky_observe


def test_package_version():
    assert isinstance(jansky_observe.__version__, str)
    assert jansky_observe.__version__


def test_version_matches_pyproject():
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    assert jansky_observe.__version__ == data["project"]["version"]


def test_jansky_dependency_importable():
    # The whole point of the sibling repo: jansky's tested helpers are available.
    from jansky import formats, signals

    assert hasattr(signals, "rng")  # seeded generators for synthetic fixtures
    assert hasattr(formats, "write_sigmf")  # self-describing IQ captures

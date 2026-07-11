"""Console entry point for the API server (``jansky-observe``).

``jansky-observe --version`` prints the version and exits 0 (deploy/install.sh
health-checks this); otherwise it runs uvicorn against
``jansky_observe.server.app:app``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from jansky_observe import __version__
from jansky_observe.config import settings_from_env

__all__ = ["main"]

APP_IMPORT_STRING = "jansky_observe.server.app:app"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (defaults come from ``JANSKY_OBSERVE_*`` env vars)."""
    defaults = settings_from_env()
    parser = argparse.ArgumentParser(
        prog="jansky-observe",
        description="Run the jansky-observe API server (REST + WebSocket live view + UI).",
    )
    parser.add_argument("--version", action="version", version=f"jansky-observe {__version__}")
    parser.add_argument(
        "--host",
        default=defaults.host,
        help=f"bind address (default: {defaults.host})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=defaults.port,
        help=f"bind port (default: {defaults.port})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the server.

    Parameters
    ----------
    argv : sequence of str, optional
        Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    uvicorn.run(APP_IMPORT_STRING, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

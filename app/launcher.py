"""Packaged entry point: no arguments launches the web app; arguments run the CLI."""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1:
        from app.cli.main import app

        app()
        return

    from app.web.main import run_server

    run_server()


if __name__ == "__main__":
    main()

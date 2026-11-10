"""Configurazione del logging del core."""

from __future__ import annotations

import logging
import sys


def setup(verbose: int = 0, quiet: bool = False) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    level = logging.WARNING if quiet else (logging.DEBUG if verbose >= 2 else logging.INFO if verbose == 1 else logging.INFO)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s" if verbose >= 2 else "%(levelname)-7s %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr, force=True)
    logging.getLogger("grabmedia").setLevel(level)

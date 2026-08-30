"""Shared app-data directory resolution.

All on-disk state lives under one root (``~/.st-explorer`` by default).
``ST_EXPLORER_HOME`` overrides the root so tests (and portable installs)
never touch the user's real data.
"""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Root app-data dir; ``ST_EXPLORER_HOME`` overrides for tests/portability."""
    override = os.environ.get('ST_EXPLORER_HOME')
    if override:
        return Path(override)
    return Path.home() / '.st-explorer'

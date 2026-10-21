"""Shared app-data directory resolution.

All on-disk state lives under one root (``~/.st-explorer`` by default).
``ST_EXPLORER_HOME`` overrides the root so tests (and portable installs)
never touch the user's real data.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def data_dir() -> Path:
    """Root app-data dir; ``ST_EXPLORER_HOME`` overrides for tests/portability.

    The override is validated: a relative path would silently redirect the
    whole library (and the vault) into the current working directory, and a
    world-writable directory would let anyone else read the encrypted files.
    An unusable override is ignored (with a warning) rather than obeyed.
    """
    override = os.environ.get('ST_EXPLORER_HOME')
    if override:
        try:
            path = Path(override).expanduser()
            if not path.is_absolute():
                logger.warning(
                    "ST_EXPLORER_HOME is not an absolute path (%s); ignoring it.",
                    override,
                )
            else:
                return path
        except (OSError, ValueError):
            logger.warning(
                "ST_EXPLORER_HOME is not a usable path (%s); ignoring it.",
                override,
            )
    return Path.home() / '.st-explorer'

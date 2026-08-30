"""Filesystem safety helpers: atomic writes and guarded replaces.

All card/session writes go through these helpers so that a crash, power
loss, or disk-full error can never leave a truncated file at the target
path (``Path.write_bytes`` truncates before writing).
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def atomic_replace(src: str | Path, dst: str | Path, retries: int = 5, delay: float = 0.3) -> None:
    """Replace *dst* with *src* atomically, retrying Windows ``PermissionError``.

    On Windows, ``os.replace`` can fail with "Access is denied" when the
    target file is briefly locked by another process (antivirus scan,
    thumbnail generation, Explorer, etc.).  A short retry resolves
    transient locks.  *src* must be on the same volume as *dst* for the
    replace to be atomic.  Other platforms never see transient locks on
    ``os.replace``, so a single attempt is made there.
    """
    attempts = retries if sys.platform == 'win32' else 1
    for attempt in range(attempts):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def atomic_write_bytes(dst: str | Path, data: bytes) -> None:
    """Write *data* to *dst* via a temp file + atomic replace."""
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dst_path.parent), prefix=f'.{dst_path.name}.', suffix='.tmp',
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        atomic_replace(tmp_path, dst_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_copy(src: str | Path, dst: str | Path) -> None:
    """Copy *src* to *dst* via a temp file in *dst*'s directory.

    Preserves metadata like ``shutil.copy2`` but never leaves a partial
    file at *dst*.
    """
    import shutil

    src_path = Path(src)
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dst_path.parent), prefix=f'.{dst_path.name}.', suffix='.tmp',
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copy2(str(src_path), str(tmp_path))
        atomic_replace(tmp_path, dst_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def unique_temp_path(directory: str | Path, prefix: str, suffix: str = '') -> Path:
    """Return a unique temp path inside *directory* (same volume as targets).

    Used by sync code that needs a staging file next to its final
    destination; callers are responsible for cleaning it up.
    """
    fd, name = tempfile.mkstemp(dir=str(directory), prefix=prefix, suffix=suffix)
    os.close(fd)
    path = Path(name)
    path.unlink(missing_ok=True)
    return path

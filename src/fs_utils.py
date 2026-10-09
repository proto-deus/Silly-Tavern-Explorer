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


def _is_retryable_lock_error(exc: OSError) -> bool:
    """True if *exc* is a transient Windows file-lock condition.

    CPython maps only ``ERROR_ACCESS_DENIED`` (5) to ``PermissionError``. The
    far more common transient case — ``ERROR_SHARING_VIOLATION`` (32) from the
    Explorer preview pane, a virus scanner or the thumbnailer — surfaces as a
    bare ``OSError``, so retrying only ``PermissionError`` missed exactly the
    scenario this function exists for.

    Both ``winerror`` and ``errno`` are consulted: a real OS-raised error
    carries ``winerror``, but Python does not populate it for every
    OSError-derived class (errno 32 maps to BrokenPipeError with
    ``winerror is None``).
    """
    if isinstance(exc, PermissionError):
        return True
    if sys.platform != 'win32':
        return False
    # 32 = ERROR_SHARING_VIOLATION, 33 = ERROR_LOCK_VIOLATION,
    # 5 = ERROR_ACCESS_DENIED, 21 = ERROR_NOT_READY (device busy).
    codes = {5, 21, 32, 33}
    winerr = getattr(exc, 'winerror', None)
    if winerr is not None:
        return winerr in codes
    # 13 = EACCES, 32/33 = sharing/lock violations in the CRT errno space.
    return exc.errno in {13, 32, 33}


def atomic_replace(src: str | Path, dst: str | Path, retries: int = 5, delay: float = 0.3) -> None:
    """Replace *dst* with *src* atomically, retrying transient file locks.

    On Windows, ``os.replace`` fails while the target is briefly held by
    another process (antivirus scan, thumbnail generation, Explorer preview
    pane, ...).  A short retry resolves those.  *src* must be on the same
    volume as *dst* for the replace to be atomic.  Other platforms don't see
    these transient locks, so a single attempt is made there.
    """
    attempts = retries if sys.platform == 'win32' else 1
    for attempt in range(attempts):
        try:
            os.replace(str(src), str(dst))
            return
        except OSError as exc:
            if attempt == attempts - 1 or not _is_retryable_lock_error(exc):
                raise
            time.sleep(delay)


def _temp_sibling(dst_path: Path) -> tuple[int, Path]:
    """Create a temp file next to *dst_path* for an atomic replace.

    The temp name is deliberately short (``.ste-<rand>.tmp``) rather than
    derived from the full destination name: a card name is already capped near
    MAX_PATH, and prefixing it made the *temp* name longer than the final one,
    so the write failed with ENAMETOOLONG on paths the final name fits.  A
    non-descriptive name also keeps the staging file out of the way of tools
    that scan the library directory.
    """
    return tempfile.mkstemp(
        dir=str(dst_path.parent), prefix='.ste-', suffix='.tmp',
    )


def atomic_write_bytes(dst: str | Path, data: bytes) -> None:
    """Write *data* to *dst* via a temp file + atomic replace."""
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = _temp_sibling(dst_path)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
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
    fd, tmp_name = _temp_sibling(dst_path)
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

    The name is reserved by ``mkstemp`` and then unlinked, so a concurrent
    process could claim it before the caller writes. Callers must therefore
    treat the result as advisory, or use :func:`atomic_write_bytes`.
    """
    fd, name = tempfile.mkstemp(dir=str(directory), prefix=prefix, suffix=suffix)
    os.close(fd)
    path = Path(name)
    path.unlink(missing_ok=True)
    return path

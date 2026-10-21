from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from src.app_paths import data_dir

logger = logging.getLogger(__name__)


class SingleInstance:
    """Lock-file based single-instance enforcement.

    Uses OS-level locking (msvcrt on Windows, fcntl on Unix) so the lock
    is automatically released if the process crashes.
    """

    def __init__(self, lock_path: str | Path | None = None):
        if lock_path is not None:
            self.lock_path = Path(lock_path)
        else:
            self.lock_path = data_dir() / 'st-explorer.lock'
        self._fd: int | None = None

    def acquire(self, wait_seconds: float = 0.0, on_wait=None) -> bool:
        """Attempt to acquire the lock. Returns True if this is the only instance.

        With *wait_seconds* > 0 the attempt is repeated for that long
        before giving up.  A restart launches the new instance while the
        old one is still shutting down, and the OS-level lock is only
        released when the old process exits - without the grace period the
        new instance loses the race and quits with "already running".

        *on_wait* is called with the number of seconds waited so far, so
        the caller can tell the user that the app is waiting for the other
        instance to close instead of appearing to hang.
        """
        if self._fd is not None:
            return True

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            # Ensure at least 1 byte exists for msvcrt.locking on Windows.
            if os.fstat(fd).st_size < 1:
                os.write(fd, b'\x00')
        except OSError:
            os.close(fd)
            return False

        started = time.monotonic()
        deadline = started + max(0.0, wait_seconds)
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                self._try_lock(fd)
                self._fd = fd
                logger.info("Acquired single-instance lock: %s", self.lock_path)
                return True
            except OSError:
                now = time.monotonic()
                if now >= deadline:
                    os.close(fd)
                    logger.info("Another instance is already running")
                    return False
                if on_wait is not None:
                    try:
                        on_wait(now - started)
                    except Exception:
                        logger.debug("The wait callback failed", exc_info=True)
                # The lock is held by the instance that is shutting down.
                time.sleep(0.15)

    def _try_lock(self, fd: int) -> None:
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        """Release the lock. Safe to call multiple times."""
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)
            logger.info("Released single-instance lock")

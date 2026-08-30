from __future__ import annotations

import logging
import os
import sys
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

    def acquire(self) -> bool:
        """Attempt to acquire the lock. Returns True if this is the only instance."""
        if self._fd is not None:
            return True

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            # Ensure at least 1 byte exists for msvcrt.locking on Windows.
            if os.fstat(fd).st_size < 1:
                os.write(fd, b'\x00')
            os.lseek(fd, 0, os.SEEK_SET)
            self._try_lock(fd)
            self._fd = fd
            logger.info("Acquired single-instance lock: %s", self.lock_path)
            return True
        except OSError:
            os.close(fd)
            logger.info("Another instance is already running")
            return False

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

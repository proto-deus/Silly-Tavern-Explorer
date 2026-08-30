from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def get_containing_folder(source_path: str | Path | None) -> Path | None:
    """Return the parent directory of *source_path* if it exists.

    Pure function (no Qt) so it can be unit-tested directly.
    """
    if not source_path:
        return None
    p = Path(source_path)
    if not p.exists():
        return None
    return p.parent


def open_containing_folder(source_path: str | Path | None) -> bool:
    """Open the folder containing *source_path* in the OS file manager.

    On Windows the file itself is selected via ``explorer /select,``. On other
    platforms the parent folder is opened with ``QDesktopServices``.

    Returns ``True`` when an action was taken, ``False`` when the path is
    missing/invalid.
    """
    folder = get_containing_folder(source_path)
    if folder is None:
        return False
    if sys.platform == 'win32':
        import subprocess
        subprocess.Popen(['explorer', '/select,', str(Path(source_path).resolve())])
        return True
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
    return True

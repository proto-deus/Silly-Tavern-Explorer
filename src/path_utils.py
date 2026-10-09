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
        # Explorer needs "/select," and the path as ONE argument with no space
        # between them. Passing them as separate argv entries made
        # CreateProcess join them with a space, so the file was never selected
        # (Explorer fell back to just opening the folder). CREATE_NO_WINDOW
        # stops a console window flashing for what is a background action.
        target = f'/select,{Path(source_path).resolve()}'
        startupinfo = None
        creationflags = 0
        if sys.platform == 'win32':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        subprocess.Popen(
            ['explorer', target],
            shell=False,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        return True
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
    return True

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QLabel, QWidget

from src.settings_manager import load_st_characters_path
from src.sillytavern_sync import list_st_characters


class _StCountWorker(QThread):
    """Count valid ST character cards off the UI thread.

    Parsing every PNG in the ST characters directory to verify it holds
    valid card data is expensive on large libraries; doing so on the UI
    thread (triggered by ``QFileSystemWatcher``) freezes the window.
    """

    # Named ``completed`` so the built-in QThread.finished stays available
    # for lifetime management (deleteLater).
    completed = pyqtSignal(int)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path

    def run(self) -> None:
        try:
            count = len(list_st_characters(self._path))
        except Exception:
            count = -1
        self.completed.emit(count)


class STStatusWidget(QWidget):
    """A compact status-bar indicator showing SillyTavern connection state.

    Clickable to signal that the user wants to open the config dialog.
    ``refresh()`` is debounced and runs the card count off the UI thread
    so that directory-watcher events never freeze the application.
    """

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._label = QLabel('ST: Not configured')
        self._label.setStyleSheet('color: #aaa; padding: 0 6px;')
        self.setFixedSize(220, 20)
        self._count_worker: _StCountWorker | None = None
        self._refresh_gen = 0
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(300)
        self._debounce_timer.timeout.connect(self._do_refresh)

    def refresh(self) -> None:
        """Re-read the saved path and update the indicator.

        Debounced: rapid calls (e.g. from the directory watcher firing
        multiple events) coalesce into a single background refresh.
        """
        self._debounce_timer.start()

    def _do_refresh(self) -> None:
        path = load_st_characters_path()
        if not path:
            self._label.setText('ST: Not configured')
            return
        p = Path(path)
        if not p.is_dir():
            self._label.setText('ST: Directory missing')
            return
        self._label.setText('ST: Scanning...')
        self._refresh_gen += 1
        gen = self._refresh_gen
        # Cancel any in-flight count; the old worker is parented so it is
        # never destroyed while running, and its stale result is dropped
        # by the generation counter.
        worker = _StCountWorker(path, self)
        self._count_worker = worker
        worker.completed.connect(lambda count: self._on_count_finished(count, gen))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_count_finished(self, count: int, gen: int) -> None:
        if gen != self._refresh_gen:
            return
        self._count_worker = None
        if count < 0:
            self._label.setText('ST: Directory missing')
        else:
            self._label.setText(f'ST: Connected ({count} cards)')

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

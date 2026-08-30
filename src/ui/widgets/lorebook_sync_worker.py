from __future__ import annotations

import logging

from PyQt6.QtCore import QThread, pyqtSignal

from src.lorebook_sync import (
    LorebookPlanItem,
    LorebookSyncPair,
    bulk_lorebook_sync,
    compare_lorebooks,
    list_explorer_lorebooks,
    list_st_lorebooks,
    load_sync_state,
)
from src.sillytavern_sync import SyncSummary

logger = logging.getLogger(__name__)


class LorebookScanWorker(QThread):
    """Scan both lorebook libraries off the UI thread.

    Signals:
        completed(pairs) — ``list[LorebookSyncPair]`` on success.
        failed(message)  — error message string on failure.
    """

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, worlds_dir, parent=None):
        super().__init__(parent)
        self.worlds_dir = worlds_dir
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            explorer_entries = list_explorer_lorebooks()
            if self._cancel:
                return
            st_entries = list_st_lorebooks(self.worlds_dir)
            if self._cancel:
                return
            pairs = compare_lorebooks(explorer_entries, st_entries, load_sync_state())
            if self._cancel:
                return
            self.completed.emit(pairs)
        except Exception as e:
            logger.exception("Lorebook sync scan failed")
            self.failed.emit(str(e))


class LorebookActionWorker(QThread):
    """Execute a lorebook sync plan off the UI thread, then rescan.

    Signals:
        progress(current, total, name) — per-item progress update.
        completed(result)              — ``(SyncSummary, list[LorebookSyncPair])``
                                         once the plan + rescan complete.
    """

    progress = pyqtSignal(int, int, str)
    completed = pyqtSignal(object)

    def __init__(self, plan: list[LorebookPlanItem], worlds_dir, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.worlds_dir = worlds_dir
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            summary = bulk_lorebook_sync(
                self.plan,
                self.worlds_dir,
                is_cancelled=lambda: self._cancel,
                on_progress=lambda c, t, n: self.progress.emit(c, t, n),
            )
        except Exception as e:
            logger.exception("LorebookActionWorker crashed during bulk sync")
            summary = SyncSummary(errors=[f"Unexpected error: {e}"])

        # Rescan so the dialog can refresh its tree in one step.
        pairs: list[LorebookSyncPair] = []
        try:
            if not self._cancel:
                pairs = compare_lorebooks(
                    list_explorer_lorebooks(),
                    list_st_lorebooks(self.worlds_dir),
                    load_sync_state(),
                )
        except Exception:
            logger.exception("LorebookActionWorker crashed during rescan")

        self.completed.emit((summary, pairs))

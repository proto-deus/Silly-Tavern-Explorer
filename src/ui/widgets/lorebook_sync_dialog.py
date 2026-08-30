from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.lorebook_sync import (
    LorebookPlanItem,
    LorebookSyncCategory,
    LorebookSyncPair,
    active_plan_items,
    build_lorebook_pull_all_plan,
    build_lorebook_push_all_plan,
    build_lorebook_sync_plan,
    lorebook_category_label,
    summarize_lorebook_pairs,
)
from src.sillytavern_sync import SyncAction

logger = logging.getLogger(__name__)

# Strong refs to every worker this dialog started (superseded ones included)
# so a Python wrapper can't be GC'd — destroying the C++ QThread mid-run
# would abort the process.
_LIVE_WORKERS: list[object] = []

_CATEGORY_ORDER = [
    LorebookSyncCategory.ST_CHANGED,
    LorebookSyncCategory.EXPLORER_CHANGED,
    LorebookSyncCategory.BOTH_CHANGED,
    LorebookSyncCategory.ONLY_ST,
    LorebookSyncCategory.ONLY_EXPLORER,
    LorebookSyncCategory.IN_SYNC,
]

_PULLABLE = {
    LorebookSyncCategory.ONLY_ST,
    LorebookSyncCategory.ST_CHANGED,
    LorebookSyncCategory.BOTH_CHANGED,
}
_PUSHABLE = {
    LorebookSyncCategory.ONLY_EXPLORER,
    LorebookSyncCategory.EXPLORER_CHANGED,
    LorebookSyncCategory.BOTH_CHANGED,
}


class LorebookSyncDialog(QDialog):
    """Dialog for syncing ST Explorer lorebooks with SillyTavern world info.

    Shows a grouped tree of book pairs classified by sync status.  Per-book
    Push/Pull actions and bulk Pull All / Push All / Sync All operations run
    in background threads so large world directories never block the UI.
    """

    sync_completed = pyqtSignal()

    def __init__(self, worlds_dir: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._worlds_dir = worlds_dir
        self._pairs: list[LorebookSyncPair] = []
        self._scan_worker = None
        self._action_worker = None
        self._scan_gen = 0
        self._progress: QProgressDialog | None = None

        self.setWindowTitle('Lorebook Sync')
        self.setMinimumSize(760, 520)

        layout = QVBoxLayout(self)

        header_row = QHBoxLayout()
        self._path_label = QLabel('')
        self._path_label.setStyleSheet('font-size: 12px; color: #ccc;')
        header_row.addWidget(self._path_label)
        header_row.addStretch()
        refresh_btn = QPushButton('Refresh')
        refresh_btn.clicked.connect(self._scan_and_refresh)
        header_row.addWidget(refresh_btn)
        layout.addLayout(header_row)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(['Name', 'Status', 'Entries', 'Filename'])
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._tree, 1)

        detail_container = QWidget()
        detail_layout = QVBoxLayout(detail_container)
        detail_layout.setContentsMargins(4, 4, 4, 4)
        self._detail_text = QTextEdit()
        self._detail_text.setReadOnly(True)
        self._detail_text.setMaximumHeight(140)
        self._detail_text.setStyleSheet(
            'font-family: Consolas, "Courier New", monospace; font-size: 11px;'
        )
        detail_layout.addWidget(self._detail_text)
        layout.addWidget(detail_container)

        btn_row = QHBoxLayout()
        self._pull_all_btn = QPushButton('Pull All from ST')
        self._pull_all_btn.clicked.connect(self._on_pull_all)
        btn_row.addWidget(self._pull_all_btn)

        self._push_all_btn = QPushButton('Push All to ST')
        self._push_all_btn.clicked.connect(self._on_push_all)
        btn_row.addWidget(self._push_all_btn)

        btn_row.addStretch()

        self._pull_btn = QPushButton('Pull')
        self._pull_btn.setEnabled(False)
        self._pull_btn.clicked.connect(self._on_pull_selected)
        btn_row.addWidget(self._pull_btn)

        self._push_btn = QPushButton('Push')
        self._push_btn.setEnabled(False)
        self._push_btn.clicked.connect(self._on_push_selected)
        btn_row.addWidget(self._push_btn)

        btn_row.addStretch()

        self._sync_all_btn = QPushButton('Sync All')
        self._sync_all_btn.clicked.connect(self._on_sync_all)
        btn_row.addWidget(self._sync_all_btn)

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._status_label = QLabel('')
        self._status_label.setStyleSheet('color: #aaa; font-size: 11px;')
        layout.addWidget(self._status_label)

        self._update_path_label()
        if self._worlds_dir and Path(self._worlds_dir).is_dir():
            self._scan_and_refresh()
        else:
            self._status_label.setText(
                'SillyTavern world-info directory not found. '
                'Use Configure SillyTavern... to set it up.'
            )

    # ---- path label ----

    def _update_path_label(self) -> None:
        if self._worlds_dir:
            self._path_label.setText(f'ST World Info Directory: {self._worlds_dir}')
        else:
            self._path_label.setText('ST World Info Directory: (not configured)')

    # ---- scanning ----

    def _scan_and_refresh(self) -> None:
        if not self._worlds_dir:
            self._status_label.setText(
                'SillyTavern world-info directory not configured.'
            )
            return
        # Ask any in-flight scan to stop; stale results are discarded by the
        # sender-identity check so we never block the UI thread on wait().
        if self._worker_is_alive(self._scan_worker):
            self._scan_worker.cancel()
        self._scan_gen += 1
        self._set_busy(True)
        self._status_label.setText('Scanning...')
        from src.ui.widgets.lorebook_sync_worker import LorebookScanWorker

        worker = LorebookScanWorker(self._worlds_dir)
        self._scan_worker = worker
        _LIVE_WORKERS.append(worker)
        # Bound-method slots run on the GUI thread (queued connection);
        # lambdas would execute on the worker thread.
        worker.completed.connect(self._on_scan_finished)
        worker.failed.connect(self._on_scan_failed)
        # Cleanup on the built-in signal for every outcome (success included).
        worker.finished.connect(self._release_worker)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @staticmethod
    def _worker_is_alive(worker) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            # The C++ QThread was already destroyed by deleteLater.
            return False

    def _release_worker(self) -> None:
        try:
            _LIVE_WORKERS.remove(self.sender())
        except ValueError:
            pass

    def _on_scan_finished(self, pairs) -> None:
        if self.sender() is not self._scan_worker:
            logger.warning("Stale lorebook scan result ignored")
            return
        self._scan_worker = None
        self._pairs = pairs
        self._refresh_tree()
        self._set_busy(False)

    def _on_scan_failed(self, message: str) -> None:
        if self.sender() is not self._scan_worker:
            return
        self._scan_worker = None
        self._set_busy(False)
        self._status_label.setText(f'Scan failed: {message}')

    def _set_busy(self, busy: bool) -> None:
        """Toggle action buttons during a background scan or sync."""
        has_dir = bool(self._worlds_dir)
        self._sync_all_btn.setEnabled(not busy and has_dir)
        self._pull_all_btn.setEnabled(not busy and has_dir)
        self._push_all_btn.setEnabled(not busy and has_dir)
        if busy:
            self._pull_btn.setEnabled(False)
            self._push_btn.setEnabled(False)
        else:
            self._on_selection_changed()

    def _refresh_tree(self) -> None:
        self._tree.clear()
        if not self._pairs:
            self._status_label.setText('No lorebooks to compare.')
            return

        groups: dict[LorebookSyncCategory, list[LorebookSyncPair]] = {}
        for pair in self._pairs:
            groups.setdefault(pair.category, []).append(pair)

        for cat in _CATEGORY_ORDER:
            if cat not in groups:
                continue
            pairs = groups[cat]
            top = QTreeWidgetItem(
                [f"{lorebook_category_label(cat)} ({len(pairs)})", '', '', '']
            )
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            for pair in pairs:
                entry = pair.explorer or pair.st
                name = entry.name if entry else '?'
                filename = entry.filename if entry else '?'
                count = str(entry.entry_count) if entry else ''
                child = QTreeWidgetItem(
                    [name, lorebook_category_label(cat), count, filename]
                )
                child.setData(0, Qt.ItemDataRole.UserRole, pair)
                top.addChild(child)
            self._tree.addTopLevelItem(top)
            top.setExpanded(True)

        counts = summarize_lorebook_pairs(self._pairs)
        summary_parts = [
            f"{v} {lorebook_category_label(LorebookSyncCategory(k))}"
            for k, v in counts.items()
        ]
        self._status_label.setText('  |  '.join(summary_parts))

    # ---- detail pane + button enable/disable ----

    def _selected_pair(self) -> LorebookSyncPair | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        return data if isinstance(data, LorebookSyncPair) else None

    def _on_selection_changed(self) -> None:
        pair = self._selected_pair()
        can_pull = pair is not None and pair.st is not None \
            and pair.category in _PULLABLE
        can_push = pair is not None and pair.explorer is not None \
            and pair.category in _PUSHABLE
        self._pull_btn.setEnabled(can_pull)
        self._push_btn.setEnabled(can_push)

        if pair is None:
            self._detail_text.clear()
            return
        parts = [f"Status: {lorebook_category_label(pair.category)}"]
        if pair.explorer:
            parts.append('\n=== ST Explorer ===')
            parts.append(f"File: {pair.explorer.filename}")
            parts.append(f"Name: {pair.explorer.name}")
            parts.append(f"Entries: {pair.explorer.entry_count}")
        if pair.st:
            parts.append('\n=== SillyTavern ===')
            parts.append(f"File: {pair.st.filename}")
            parts.append(f"Name: {pair.st.name}")
            parts.append(f"Entries: {pair.st.entry_count}")
        self._detail_text.setPlainText('\n'.join(parts))

    # ---- actions ----

    def _run_plan(self, plan: list[LorebookPlanItem]) -> None:
        items = active_plan_items(plan)
        if not items:
            self._status_label.setText('Nothing to sync.')
            return
        from src.ui.widgets.lorebook_sync_worker import LorebookActionWorker

        self._set_busy(True)
        self._action_worker = LorebookActionWorker(items, self._worlds_dir)
        _LIVE_WORKERS.append(self._action_worker)
        self._progress = QProgressDialog(
            'Syncing lorebooks...', 'Cancel', 0, len(items), self,
        )
        self._progress.setWindowTitle('Lorebook Sync')
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setMinimumDuration(300)
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.canceled.connect(self._action_worker.cancel)
        self._action_worker.progress.connect(self._on_action_progress)
        self._action_worker.completed.connect(self._on_action_finished)
        self._action_worker.finished.connect(self._release_worker)
        self._action_worker.finished.connect(self._action_worker.deleteLater)
        self._action_worker.start()

    def _on_pull_all(self) -> None:
        self._run_plan(build_lorebook_pull_all_plan(self._pairs))

    def _on_push_all(self) -> None:
        self._run_plan(build_lorebook_push_all_plan(self._pairs))

    def _on_sync_all(self) -> None:
        self._run_plan(build_lorebook_sync_plan(self._pairs))

    def _on_pull_selected(self) -> None:
        pair = self._selected_pair()
        if pair is not None and pair.st is not None:
            self._run_plan([LorebookPlanItem(pair, SyncAction.PULL)])

    def _on_push_selected(self) -> None:
        pair = self._selected_pair()
        if pair is not None and pair.explorer is not None:
            self._run_plan([LorebookPlanItem(pair, SyncAction.PUSH)])

    def _on_action_progress(self, current: int, total: int, name: str) -> None:
        progress = self._progress
        if progress is None:
            return
        progress.setMaximum(total)
        progress.setValue(current)
        progress.setLabelText(f"Syncing {current}/{total}: {name}")

    def _on_action_finished(self, result) -> None:
        summary, pairs = result
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        self._action_worker = None
        self._status_label.setText(summary.message())
        if summary.errors:
            QMessageBox.warning(
                self, 'Lorebook Sync Errors',
                summary.message() + "\n\nErrors:\n" + '\n'.join(summary.errors[:20]),
            )
        self.sync_completed.emit()
        self._pairs = pairs
        self._refresh_tree()
        self._set_busy(False)

    # ---- cleanup ----

    def _shutdown_workers(self) -> bool:
        """Cooperatively cancel running workers.

        Returns True when it is safe to close the dialog. A worker still
        finishing after a short wait keeps the dialog open — destroying a
        live QThread aborts the process.
        """
        ok = True
        for worker in (self._scan_worker, self._action_worker):
            if not self._worker_is_alive(worker):
                continue
            try:
                worker.cancel()
            except AttributeError:
                pass
            if not worker.wait(3000):
                ok = False
        if not ok:
            self._status_label.setText(
                'Cancelling… please close again in a moment.'
            )
            self._status_label.setVisible(True)
        return ok

    def accept(self) -> None:
        if self._shutdown_workers():
            super().accept()

    def reject(self) -> None:
        # Also covers Esc.
        if self._shutdown_workers():
            super().reject()

    def closeEvent(self, event) -> None:
        if not self._shutdown_workers():
            event.ignore()
            return
        super().closeEvent(event)

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
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.sillytavern_sync import (
    SyncAction,
    SyncCategory,
    SyncPair,
    SyncPlanItem,
    build_pull_all_plan,
    build_push_all_plan,
    build_sync_plan,
    category_label,
    link_pair,
    summarize_pairs,
)

logger = logging.getLogger(__name__)

# Strong refs to every worker this dialog started (superseded ones included)
# so a Python wrapper can't be GC'd — destroying the C++ QThread mid-run
# would abort the process.
_LIVE_WORKERS: list[object] = []

_CATEGORY_ORDER = [
    SyncCategory.ONLY_ST,
    SyncCategory.DELETED_IN_EXPLORER,
    SyncCategory.ONLY_EXPLORER,
    SyncCategory.ST_CHANGED,
    SyncCategory.EXPLORER_CHANGED,
    SyncCategory.BOTH_CHANGED,
    SyncCategory.UNLINKED_MATCH,
    SyncCategory.IN_SYNC,
]


class STSyncDialog(QDialog):
    """Dialog for syncing the ST Explorer library with a SillyTavern directory.

    Shows a grouped tree of card pairs classified by sync status.  Per-card
    actions (Pull / Push / Link / Unlink / Delete in ST) and a bulk
    "Sync All" operation (runs in a background :class:`SyncWorker`) are
    provided.  Cards deleted in ST Explorer while linked are remembered
    (tombstoned) and their ST copies are removed by Sync All instead of
    being re-imported.
    """

    sync_completed = pyqtSignal()

    def __init__(self, db, characters_dir: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._characters_dir = characters_dir
        self._pairs: list[SyncPair] = []
        self._sync_worker = None
        self._scan_worker = None
        self._scan_gen = 0
        self._progress: QProgressDialog | None = None

        self.setWindowTitle('SillyTavern Sync')
        self.setMinimumSize(900, 600)

        layout = QVBoxLayout(self)

        header_row = QHBoxLayout()
        self._path_label = QLabel('')
        self._path_label.setStyleSheet('font-size: 12px; color: #ccc;')
        header_row.addWidget(self._path_label)
        header_row.addStretch()
        self._configure_btn = QPushButton('Configure...')
        self._configure_btn.clicked.connect(self._on_configure)
        header_row.addWidget(self._configure_btn)
        layout.addLayout(header_row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(['Name', 'Status', 'Creator', 'Linked'])
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self._tree)

        detail_container = QWidget()
        detail_layout = QVBoxLayout(detail_container)
        detail_layout.setContentsMargins(4, 4, 4, 4)
        self._detail_text = QTextEdit()
        self._detail_text.setReadOnly(True)
        self._detail_text.setMaximumHeight(180)
        self._detail_text.setStyleSheet(
            'font-family: Consolas, "Courier New", monospace; font-size: 11px;'
        )
        detail_layout.addWidget(self._detail_text)
        splitter.addWidget(detail_container)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        self._pull_btn = QPushButton('Pull All from ST')
        self._pull_btn.clicked.connect(self._on_pull)
        btn_row.addWidget(self._pull_btn)

        self._push_btn = QPushButton('Push All to ST')
        self._push_btn.clicked.connect(self._on_push)
        btn_row.addWidget(self._push_btn)

        self._link_btn = QPushButton('Link')
        self._link_btn.setEnabled(False)
        self._link_btn.clicked.connect(self._on_link)
        btn_row.addWidget(self._link_btn)

        self._unlink_btn = QPushButton('Unlink')
        self._unlink_btn.setEnabled(False)
        self._unlink_btn.clicked.connect(self._on_unlink)
        btn_row.addWidget(self._unlink_btn)

        self._delete_st_btn = QPushButton('Delete in ST')
        self._delete_st_btn.setEnabled(False)
        self._delete_st_btn.clicked.connect(self._on_delete_st)
        btn_row.addWidget(self._delete_st_btn)

        btn_row.addStretch()

        self._sync_all_btn = QPushButton('Sync All')
        self._sync_all_btn.clicked.connect(self._on_sync_all)
        btn_row.addWidget(self._sync_all_btn)

        refresh_btn = QPushButton('Refresh')
        refresh_btn.clicked.connect(self._scan_and_refresh)
        btn_row.addWidget(refresh_btn)

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._status_label = QLabel('')
        self._status_label.setStyleSheet('color: #aaa; font-size: 11px;')
        layout.addWidget(self._status_label)

        self._update_path_label()
        if characters_dir and Path(characters_dir).is_dir():
            self._scan_and_refresh()
        else:
            self._status_label.setText(
                'SillyTavern directory not configured. Click "Configure..." to set it up.'
            )

    def update_characters_dir(self, path: str) -> None:
        self._characters_dir = path
        self._update_path_label()
        if path and Path(path).is_dir():
            self._scan_and_refresh()

    def _update_path_label(self) -> None:
        if self._characters_dir:
            self._path_label.setText(f'ST Directory: {self._characters_dir}')
        else:
            self._path_label.setText('ST Directory: (not configured)')

    def _on_configure(self) -> None:
        from src.ui.widgets.st_config_dialog import STConfigDialog
        dlg = STConfigDialog(self)
        dlg.settings_changed.connect(self.update_characters_dir)
        dlg.exec()

    # ---- scanning ----

    def _scan_and_refresh(self) -> None:
        if not self._characters_dir or not Path(self._characters_dir).is_dir():
            self._status_label.setText('SillyTavern directory not found.')
            self._tree.clear()
            self._pairs = []
            return
        # Ask any in-flight scan to stop; stale results are discarded by the
        # sender-identity check so we never block the UI thread on wait().
        if self._worker_is_alive(self._scan_worker):
            self._scan_worker.cancel()
        self._scan_gen += 1
        self._set_busy(True)
        self._status_label.setText('Scanning...')
        from src.ui.widgets.sync_worker import ScanWorker
        worker = ScanWorker(self.db, self._characters_dir)
        self._scan_worker = worker
        _LIVE_WORKERS.append(worker)
        logger.info("ScanWorker started (gen=%d, dir=%s)", self._scan_gen, self._characters_dir)
        # Bound-method slots run on the GUI thread (queued connection);
        # lambdas would execute on the worker thread.
        worker.completed.connect(self._on_scan_finished)
        worker.failed.connect(self._on_scan_failed)
        # Cleanup on the built-in signal for every outcome (success included).
        worker.finished.connect(self._release_worker)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _release_worker(self) -> None:
        try:
            _LIVE_WORKERS.remove(self.sender())
        except ValueError:
            pass

    @staticmethod
    def _worker_is_alive(worker) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            # The C++ QThread was already destroyed by deleteLater.
            return False

    def _on_scan_finished(self, pairs) -> None:
        if self.sender() is not self._scan_worker:
            logger.info("Ignoring stale ScanWorker result")
            return
        logger.info("ScanWorker finished (pairs=%d)", len(pairs) if pairs else -1)
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
        self._sync_all_btn.setEnabled(not busy)
        self._pull_btn.setEnabled(not busy)
        self._push_btn.setEnabled(not busy)
        if busy:
            self._link_btn.setEnabled(False)
            self._unlink_btn.setEnabled(False)
            self._delete_st_btn.setEnabled(False)
        else:
            # Re-evaluate link/unlink button states from the current selection.
            self._on_selection_changed()

    def _refresh_tree(self) -> None:
        self._tree.clear()
        if not self._pairs:
            self._status_label.setText('No cards to compare.')
            return

        groups: dict[SyncCategory, list[SyncPair]] = {}
        for pair in self._pairs:
            groups.setdefault(pair.category, []).append(pair)

        for cat in _CATEGORY_ORDER:
            if cat not in groups:
                continue
            pairs = groups[cat]
            label = f"{category_label(cat)} ({len(pairs)})"
            top = QTreeWidgetItem([label, '', '', ''])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            for pair in pairs:
                name = pair.explorer.name if pair.explorer else (
                    pair.st.name if pair.st else '?'
                )
                creator = pair.explorer.creator if pair.explorer else (
                    pair.st.creator if pair.st else ''
                )
                linked = 'Yes' if (pair.explorer and pair.explorer.st_avatar_url) else 'No'
                child = QTreeWidgetItem([name, category_label(cat), creator, linked])
                child.setData(0, Qt.ItemDataRole.UserRole, pair)
                top.addChild(child)
            self._tree.addTopLevelItem(top)
            top.setExpanded(True)

        counts = summarize_pairs(self._pairs)
        summary_parts = [f"{v} {category_label(SyncCategory(k))}" for k, v in counts.items()]
        self._status_label.setText('  |  '.join(summary_parts))

    # ---- detail pane + button enable/disable ----

    def _selected_pair(self) -> SyncPair | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        return data if isinstance(data, SyncPair) else None

    def _on_selection_changed(self) -> None:
        pair = self._selected_pair()
        if pair is None:
            self._detail_text.clear()
            self._link_btn.setEnabled(False)
            self._unlink_btn.setEnabled(False)
            self._delete_st_btn.setEnabled(False)
            return

        self._link_btn.setEnabled(
            pair.explorer is not None
            and pair.st is not None
            and pair.explorer.st_avatar_url is None
        )
        self._unlink_btn.setEnabled(
            pair.explorer is not None
            and pair.explorer.st_avatar_url is not None
        )
        # Manual ST-side deletion is offered for files no Explorer card
        # claims: ST-only leftovers and tombstoned (deleted-in-Explorer)
        # files — the latter also covers the case where Sync All skipped
        # the deletion because the ST copy changed after the deletion.
        self._delete_st_btn.setEnabled(
            pair.st is not None and pair.explorer is None
        )

        parts = [f"Status: {category_label(pair.category)}"]
        if pair.tombstone is not None:
            parts.append(f"Deleted from ST Explorer: {pair.tombstone.deleted_at or 'unknown date'}")
            if pair.st is not None and pair.tombstone.st_sync_hash \
                    and pair.st.card_hash != pair.tombstone.st_sync_hash:
                parts.append('Note: the SillyTavern copy changed after the '
                             'card was deleted; deleting it is manual.')
        if pair.explorer:
            parts.append('\n=== ST Explorer ===')
            parts.append(f"ID: {pair.explorer.char_id}")
            parts.append(f"Name: {pair.explorer.name}")
            parts.append(f"Creator: {pair.explorer.creator}")
            parts.append(f"Linked: {pair.explorer.st_avatar_url or 'No'}")
            parts.append(f"Hash: {pair.explorer.current_hash[:12]}")
        if pair.st:
            parts.append('\n=== SillyTavern ===')
            parts.append(f"File: {pair.st.filename}")
            parts.append(f"Name: {pair.st.name}")
            parts.append(f"Creator: {pair.st.creator}")
            parts.append(f"Hash: {pair.st.card_hash[:12]}")
        self._detail_text.setPlainText('\n'.join(parts))

    # ---- per-card actions ----

    def _on_pull(self) -> None:
        plan = build_pull_all_plan(self._pairs)
        if not plan:
            self._status_label.setText('Nothing to pull.')
            return
        self._start_sync_worker(plan)

    def _on_push(self) -> None:
        plan = build_push_all_plan(self._pairs)
        if not plan:
            self._status_label.setText('Nothing to push.')
            return
        self._start_sync_worker(plan)

    def _on_link(self) -> None:
        pair = self._selected_pair()
        if pair is None:
            return
        if link_pair(pair, self.db):
            name = pair.explorer.name if pair.explorer else '?'
            self._status_label.setText(f"Linked '{name}'.")
            self.sync_completed.emit()
            self._scan_and_refresh()

    def _on_unlink(self) -> None:
        pair = self._selected_pair()
        if pair is None or pair.explorer is None:
            return
        self.db.unlink_from_st(pair.explorer.char_id)
        self._status_label.setText("Unlinked.")
        self.sync_completed.emit()
        self._scan_and_refresh()

    def _on_delete_st(self) -> None:
        pair = self._selected_pair()
        if pair is None or pair.st is None or pair.explorer is not None:
            return
        name = pair.st.name or pair.st.filename
        confirm = QMessageBox.question(
            self, 'Delete from SillyTavern',
            f"Delete '{name}' ({pair.st.filename}) from the SillyTavern directory?\n\n"
            "The character file will be permanently removed.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        plan = [SyncPlanItem(pair, SyncAction.DELETE_ST)]
        self._start_sync_worker(plan)

    # ---- bulk sync ----

    def _on_sync_all(self) -> None:
        plan = build_sync_plan(self._pairs)
        if not plan:
            self._status_label.setText('Nothing to sync.')
            return
        self._start_sync_worker(plan)

    def _start_sync_worker(self, plan: list[SyncPlanItem]) -> None:
        from src.ui.widgets.sync_worker import SyncWorker

        self._set_busy(True)
        self._sync_worker = SyncWorker(self.db, plan, self._characters_dir)
        _LIVE_WORKERS.append(self._sync_worker)
        self._progress = QProgressDialog('Syncing...', 'Cancel', 0, len(plan), self)
        self._progress.setWindowTitle('SillyTavern Sync')
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setMinimumDuration(300)
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.canceled.connect(self._sync_worker.cancel)
        self._sync_worker.progress.connect(self._on_sync_progress)
        self._sync_worker.completed.connect(self._on_sync_finished)
        # Cleanup on the built-in signal so cancelled runs are still deleted.
        self._sync_worker.finished.connect(self._release_worker)
        self._sync_worker.finished.connect(self._sync_worker.deleteLater)
        self._sync_worker.start()

    def _on_sync_progress(self, current: int, total: int, name: str) -> None:
        progress = self._progress
        if progress is None:
            return
        progress.setMaximum(total)
        progress.setValue(current)
        progress.setLabelText(f"Syncing {current}/{total}: {name}")

    def _on_sync_finished(self, result) -> None:
        summary, pairs = result
        logger.info("SyncWorker finished: pushed=%d pulled=%d linked=%d skipped=%d errors=%d, pairs=%d",
                     summary.pushed, summary.pulled, summary.linked, summary.skipped,
                     summary.error_count, len(pairs))
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        self._sync_worker = None
        self._status_label.setText(summary.message())
        if summary.errors:
            QMessageBox.warning(
                self, 'Sync Errors',
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
        for worker in (self._scan_worker, self._sync_worker):
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

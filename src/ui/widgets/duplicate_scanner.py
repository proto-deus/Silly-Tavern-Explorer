from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.database import LibraryDatabase

logger = logging.getLogger(__name__)


class _DuplicateScanWorker(QThread):
    """Run one duplicate scan off the UI thread.

    ``find_image_duplicates`` perceptual-hashes every card image, which can
    take a while on large libraries; running it synchronously froze the
    window.
    """

    # Named ``completed`` so the built-in QThread.finished stays available
    # for lifetime management (deleteLater).
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, db: LibraryDatabase, use_image: bool):
        super().__init__()
        self.db = db
        self.use_image = use_image

    def run(self) -> None:
        try:
            if self.use_image:
                groups = self.db.find_image_duplicates()
            else:
                groups = self.db.find_all_duplicates()
            self.completed.emit(groups)
        except Exception as e:
            logger.exception("Duplicate scan failed")
            self.failed.emit(str(e))


# Parentless workers are tracked here so the Python wrapper (and therefore
# the C++ QThread) cannot be garbage-collected while run() is executing —
# even if the dialog closes before the scan finishes.
_LIVE_SCAN_WORKERS: list[_DuplicateScanWorker] = []


def _release_scan_worker(worker) -> None:
    """Drop *worker* from the live registry (callable from any thread)."""
    try:
        _LIVE_SCAN_WORKERS.remove(worker)
    except ValueError:
        pass


def _format_group_label(group: list[dict]) -> str:
    """Build the label for a duplicate-group tree item.

    Pure function so the formatting can be unit-tested without Qt.
    """
    name = group[0].get('name', '?')
    creator = group[0].get('creator') or 'Unknown'
    return f"{name}  by  {creator}  ({len(group)} copies)"


def _split_keep_delete(group: list[dict], keep_index: int) -> tuple[list[dict], list[dict]]:
    """Split a duplicate group into ``(to_keep, to_delete)`` lists.

    *keep_index* is clamped to the valid range.  Pure function so the
    selection logic can be unit-tested without Qt.
    """
    if not group:
        return [], []
    idx = max(0, min(keep_index, len(group) - 1))
    keep = [group[idx]]
    delete = [row for i, row in enumerate(group) if i != idx]
    return keep, delete


class DuplicateScannerDialog(QDialog):
    """Dialog for finding and resolving duplicate cards in the library.

    Groups cards by ``(name, creator)`` case-insensitively and optionally by
    perceptual image hash.  Each group is shown as a tree node with the
    individual cards as children.  Actions: delete selected card, keep one and
    delete the rest of a group, refresh, and a detail pane for comparison.
    """

    _ICON_SIZE = 32

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle('Duplicate Scanner')
        self.setMinimumSize(900, 600)
        self._groups: list[list[dict]] = []
        self._scan_worker: _DuplicateScanWorker | None = None
        self._active_worker: _DuplicateScanWorker | None = None

        layout = QVBoxLayout(self)

        header_row = QHBoxLayout()
        self._mode_label = QLabel('Scanning by name + creator')
        self._mode_label.setStyleSheet('color: #ccc;')
        header_row.addWidget(self._mode_label)
        header_row.addStretch()
        self._image_hash_btn = QPushButton('Scan by Image')
        self._image_hash_btn.setCheckable(True)
        self._image_hash_btn.clicked.connect(self._on_toggle_mode)
        header_row.addWidget(self._image_hash_btn)
        layout.addLayout(header_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(['Name', 'Creator', 'Tokens', 'Date Added', 'Spec'])
        self._tree.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self._tree)

        detail_container = QWidget()
        detail_layout = QVBoxLayout(detail_container)
        detail_layout.setContentsMargins(4, 4, 4, 4)
        self._detail_title = QLabel('Select a card to compare')
        self._detail_title.setStyleSheet('font-weight: bold; color: #e0e0e0;')
        detail_layout.addWidget(self._detail_title)
        self._detail_thumb = QLabel()
        self._detail_thumb.setFixedSize(200, 200)
        self._detail_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail_thumb.setStyleSheet(
            'background-color: #2a2a2a; border: 2px solid #3a3a3a; border-radius: 6px;'
        )
        detail_layout.addWidget(self._detail_thumb)
        self._detail_meta = QLabel('')
        self._detail_meta.setStyleSheet('color: #aaa;')
        detail_layout.addWidget(self._detail_meta)
        self._detail_text = QLabel('')
        self._detail_text.setWordWrap(True)
        self._detail_text.setStyleSheet(
            'color: #b0b0b0; padding: 6px; '
            'background-color: #222; border-radius: 4px;'
        )
        detail_layout.addWidget(self._detail_text, 1)
        splitter.addWidget(detail_container)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        self._delete_btn = QPushButton('Delete Selected')
        self._delete_btn.clicked.connect(self._on_delete_selected)
        btn_row.addWidget(self._delete_btn)
        self._keep_first_btn = QPushButton('Keep Selected, Delete Rest of Group')
        self._keep_first_btn.clicked.connect(self._on_keep_selected)
        btn_row.addWidget(self._keep_first_btn)
        self._refresh_btn = QPushButton('Refresh')
        self._refresh_btn.clicked.connect(self._refresh)
        btn_row.addWidget(self._refresh_btn)
        btn_row.addStretch()
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._status = QLabel('')
        self._status.setStyleSheet('color: #aaa; ')
        layout.addWidget(self._status)

        self._refresh()

    # ---- icon loading ----

    def _load_icon(self, thumb_path: str) -> QIcon:
        if not thumb_path or not Path(thumb_path).exists():
            return QIcon()
        pix = QPixmap(thumb_path)
        if pix.isNull():
            return QIcon()
        return QIcon(pix.scaled(
            self._ICON_SIZE, self._ICON_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    # ---- tree population ----

    @staticmethod
    def _worker_is_alive(worker) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def _refresh(self) -> None:
        """Start a background scan; results arrive in the slots below."""
        if self._worker_is_alive(self._scan_worker):
            return  # buttons are disabled while scanning anyway
        # Drop wrappers of finished scans so the registry cannot grow without
        # bound even when a dialog was destroyed before its release slot ran.
        _LIVE_SCAN_WORKERS[:] = [w for w in _LIVE_SCAN_WORKERS if not w.isFinished()]
        use_image = self._image_hash_btn.isChecked()
        self._mode_label.setText(
            'Scanning by image perceptual hash...'
            if use_image else 'Scanning by name + creator...'
        )
        self._set_scanning(True)
        self._tree.clear()
        self._status.setText('Scanning...')
        self._delete_btn.setEnabled(False)
        self._keep_first_btn.setEnabled(False)

        worker = _DuplicateScanWorker(self.db, use_image)
        self._scan_worker = worker
        self._active_worker = worker
        _LIVE_SCAN_WORKERS.append(worker)
        worker.completed.connect(self._on_scan_finished)
        worker.failed.connect(self._on_scan_failed)
        # Release on the built-in signal, not only from the result handlers.
        # The lambda is owned by the worker's signal (not the dialog), so a
        # dialog destroyed mid-scan can no longer sever the release and leave
        # every finished worker in the module-level registry forever.
        worker.finished.connect(lambda w=worker: _release_scan_worker(w))
        worker.finished.connect(self._release_worker)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _set_scanning(self, busy: bool) -> None:
        self._image_hash_btn.setEnabled(not busy)
        self._refresh_btn.setEnabled(not busy)

    def _on_scan_finished(self, groups) -> None:
        if self.sender() is not self._active_worker:
            return  # stale result from a superseded scan
        self._populate(groups)

    def _on_scan_failed(self, message: str) -> None:
        if self.sender() is not self._active_worker:
            return
        self._mode_label.setText('Scan failed')
        self._status.setText(f'Scan failed: {message}')
        self._set_scanning(False)

    def _release_worker(self) -> None:
        sender = self.sender()
        if sender is not None:
            try:
                _LIVE_SCAN_WORKERS.remove(sender)
            except ValueError:
                pass
        if self._scan_worker is sender:
            self._scan_worker = None

    def _populate(self, groups: list[list[dict]]) -> None:
        self._groups = groups
        mode = self._image_hash_btn.isChecked()
        self._mode_label.setText(
            'Scanned by image perceptual hash' if mode
            else 'Scanned by name + creator'
        )
        self._tree.clear()
        for group in self._groups:
            top = QTreeWidgetItem([_format_group_label(group), '', '', '', ''])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            for row in group:
                child = QTreeWidgetItem([
                    row.get('name', ''),
                    row.get('creator') or 'Unknown',
                    str(row.get('token_count', 0)),
                    row.get('create_date') or '',
                    row.get('spec_version') or '',
                ])
                child.setData(0, Qt.ItemDataRole.UserRole, row)
                thumb = row.get('thumbnail_path', '')
                child.setIcon(0, self._load_icon(thumb))
                top.addChild(child)
            self._tree.addTopLevelItem(top)
            top.setExpanded(True)
        total = sum(len(g) for g in self._groups)
        self._status.setText(
            f"{len(self._groups)} group(s)  |  {total} duplicate card(s).",
        )
        self._delete_btn.setEnabled(False)
        self._keep_first_btn.setEnabled(False)
        self._set_scanning(False)

    def _on_toggle_mode(self) -> None:
        self._refresh()

    # ---- detail pane ----

    def _on_selection_changed(self) -> None:
        row = self._selected_row()
        if row is None:
            self._detail_title.setText('Select a card to compare')
            self._detail_thumb.setPixmap(QPixmap())
            self._detail_meta.setText('')
            self._detail_text.setText('')
            self._delete_btn.setEnabled(False)
            self._keep_first_btn.setEnabled(False)
            return
        self._detail_title.setText(row.get('name', '?'))
        thumb = row.get('thumbnail_path', '')
        if thumb and Path(thumb).exists():
            pix = QPixmap(thumb)
            if not pix.isNull():
                self._detail_thumb.setPixmap(pix.scaled(
                    200, 200,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ))
        else:
            self._detail_thumb.setPixmap(QPixmap())
        meta_parts = []
        if row.get('creator'):
            meta_parts.append(f"Creator: {row['creator']}")
        if row.get('spec_version'):
            meta_parts.append(f"Spec: v{row['spec_version']}")
        meta_parts.append(f"Tokens: {row.get('token_count', 0):,}")
        meta_parts.append(f"Favorite: {'yes' if row.get('is_favorite') else 'no'}")
        self._detail_meta.setText('  |  '.join(meta_parts))
        self._detail_text.setText(
            (row.get('description_preview') or 'No description available.')[:500],
        )
        self._delete_btn.setEnabled(True)
        self._keep_first_btn.setEnabled(True)

    def _selected_row(self) -> dict | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return None
        return data

    def _selected_group(self) -> list[dict] | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        top = item if item.parent() is None else item.parent()
        if top is None:
            return None
        group: list[dict] = []
        for i in range(top.childCount()):
            row = top.child(i).data(0, Qt.ItemDataRole.UserRole)
            if row:
                group.append(row)
        return group or None

    # ---- actions ----

    def _on_delete_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        char_id = row.get('id')
        if char_id is None:
            return
        reply = QMessageBox.question(
            self, 'Delete Card',
            f"Delete '{row.get('name', '?')}'?\n\n"
            "This removes the card from the library and permanently deletes "
            "its card file from disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.db.remove_card(char_id, delete_files=True)
            self._status.setText(f"Deleted card ID {char_id}.")
            logger.info("Duplicate scanner deleted card %s", char_id)
        except Exception as e:
            QMessageBox.critical(self, 'Error', f"Failed to delete: {e}")
            logger.exception("Delete failed in duplicate scanner")
        self._refresh()

    def _on_keep_selected(self) -> None:
        row = self._selected_row()
        group = self._selected_group()
        if row is None or group is None:
            return
        try:
            keep_index = next(i for i, r in enumerate(group) if r.get('id') == row.get('id'))
        except StopIteration:
            return
        _, to_delete = _split_keep_delete(group, keep_index)
        if not to_delete:
            self._status.setText('Only one card in this group; nothing to delete.')
            return
        names = ', '.join(r.get('name', '?') for r in to_delete)
        reply = QMessageBox.question(
            self, 'Keep One, Delete Rest',
            f"Keep '{row.get('name', '?')}' and delete {len(to_delete)} other card(s)?\n"
            f"({names})\n\n"
            "The deleted cards' files will be permanently removed from disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        deleted = 0
        for r in to_delete:
            cid = r.get('id')
            if cid is None:
                continue
            try:
                self.db.remove_card(cid, delete_files=True)
                deleted += 1
            except Exception:
                logger.exception("Failed to delete card %s in bulk dedupe", cid)
        self._status.setText(f"Kept 1, deleted {deleted} card(s).")
        logger.info("Dedupe: kept 1, deleted %d", deleted)
        self._refresh()

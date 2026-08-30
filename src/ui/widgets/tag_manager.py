from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.database import LibraryDatabase

logger = logging.getLogger(__name__)


class TagManagerDialog(QDialog):
    """Dialog for renaming, merging, and deleting tags across the whole library.

    Operations are atomic (single DB transaction) and refresh the list when
    complete.  Launched from the Edit tab.
    """

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle('Tag Manager')
        self.setMinimumWidth(420)
        self.setMinimumHeight(420)

        layout = QVBoxLayout(self)

        header = QLabel('All tags in the library (with usage counts):')
        header.setStyleSheet('font-size: 12px; font-weight: bold; color: #ccc;')
        layout.addWidget(header)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        layout.addWidget(self._list)

        self._status = QLabel('')
        self._status.setStyleSheet('color: #aaa; font-size: 11px;')
        layout.addWidget(self._status)

        btn_row = QHBoxLayout()
        self._rename_btn = QPushButton('Rename...')
        self._rename_btn.clicked.connect(self._on_rename)
        btn_row.addWidget(self._rename_btn)
        self._merge_btn = QPushButton('Merge into...')
        self._merge_btn.clicked.connect(self._on_merge)
        btn_row.addWidget(self._merge_btn)
        self._delete_btn = QPushButton('Delete')
        self._delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self._delete_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

        self._refresh()

    def _refresh(self) -> None:
        counts = self.db.get_tag_counts()
        self._list.clear()
        for tag in sorted(counts):
            item = QListWidgetItem(f"{tag}  ({counts[tag]})")
            item.setData(Qt.ItemDataRole.UserRole, tag)
            self._list.addItem(item)
        self._status.setText(f"{len(counts)} tag(s).")

    def _selected_tag(self) -> str | None:
        item = self._list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_rename(self) -> None:
        old = self._selected_tag()
        if old is None:
            QMessageBox.information(self, 'Rename', 'Select a tag to rename.')
            return
        new, ok = QInputDialog.getText(self, 'Rename Tag', f'Rename "{old}" to:', text=old)
        if not ok:
            return
        from src.tag_ops import normalize_tag
        new_n = normalize_tag(new)
        if not new_n:
            QMessageBox.warning(self, 'Rename', 'Tag name cannot be empty.')
            return
        if new_n == old:
            return
        changed = self.db.rename_tag_all(old, new_n)
        self._status.setText(f"Renamed '{old}' -> '{new_n}' on {changed} card(s).")
        logger.info("Tag rename: '%s' -> '%s' (%d cards)", old, new_n, changed)
        self._refresh()

    def _on_merge(self) -> None:
        source = self._selected_tag()
        if source is None:
            QMessageBox.information(self, 'Merge', 'Select a source tag to merge.')
            return
        counts = self.db.get_tag_counts()
        targets = sorted(t for t in counts if t != source)
        if not targets:
            QMessageBox.information(self, 'Merge', 'No other tags to merge into.')
            return
        target, ok = QInputDialog.getItem(
            self, 'Merge Tag',
            f'Merge "{source}" into which tag?',
            targets, 0, False,
        )
        if not ok:
            return
        changed = self.db.merge_tag_all(source, target)
        self._status.setText(f"Merged '{source}' into '{target}' on {changed} card(s).")
        logger.info("Tag merge: '%s' -> '%s' (%d cards)", source, target, changed)
        self._refresh()

    def _on_delete(self) -> None:
        tag = self._selected_tag()
        if tag is None:
            QMessageBox.information(self, 'Delete', 'Select a tag to delete.')
            return
        reply = QMessageBox.question(
            self, 'Delete Tag',
            f"Remove the tag '{tag}' from all cards?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        changed = self.db.delete_tag_all(tag)
        self._status.setText(f"Deleted '{tag}' from {changed} card(s).")
        logger.info("Tag delete: '%s' (%d cards)", tag, changed)
        self._refresh()

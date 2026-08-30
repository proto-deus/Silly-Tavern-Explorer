from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
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


def normalize_collection_name(name: str) -> str:
    """Collapse whitespace and strip a collection name. Pure function."""
    return ' '.join((name or '').split())


class CollectionsManagerDialog(QDialog):
    """Create / rename / delete collections for the library filter."""

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.changed = False
        self.setWindowTitle('Manage Collections')
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _item: self._rename())
        layout.addWidget(self._list)

        buttons = QHBoxLayout()
        new_btn = QPushButton('New...')
        new_btn.clicked.connect(self._create)
        buttons.addWidget(new_btn)
        rename_btn = QPushButton('Rename...')
        rename_btn.clicked.connect(self._rename)
        buttons.addWidget(rename_btn)
        delete_btn = QPushButton('Delete')
        delete_btn.clicked.connect(self._delete)
        buttons.addWidget(delete_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        layout.addWidget(close_box)

        self._refresh()

    def _refresh(self) -> None:
        selected_id = self._selected_collection_id()
        self._list.clear()
        for col in self.db.list_collections():
            item = QListWidgetItem(f"{col['name']}  ({col['card_count']})")
            item.setData(Qt.ItemDataRole.UserRole, col['id'])
            self._list.addItem(item)
            if col['id'] == selected_id:
                item.setSelected(True)

    def _selected_collection_id(self) -> int | None:
        item = self._list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _create(self) -> None:
        name, ok = QInputDialog.getText(self, 'New Collection', 'Name:')
        if not ok:
            return
        clean = normalize_collection_name(name)
        if not clean:
            return
        try:
            self.db.create_collection(clean)
        except Exception as exc:
            QMessageBox.warning(self, 'Collections', f'Could not create collection:\n{exc}')
            return
        self.changed = True
        self._refresh()

    def _rename(self) -> None:
        col_id = self._selected_collection_id()
        if col_id is None:
            return
        current = next(
            (c['name'] for c in self.db.list_collections() if c['id'] == col_id), '',
        )
        name, ok = QInputDialog.getText(self, 'Rename Collection', 'New name:', text=current)
        if not ok:
            return
        clean = normalize_collection_name(name)
        if not clean or clean == current:
            return
        try:
            self.db.rename_collection(col_id, clean)
        except Exception as exc:
            QMessageBox.warning(self, 'Collections', f'Could not rename collection:\n{exc}')
            return
        self.changed = True
        self._refresh()

    def _delete(self) -> None:
        col_id = self._selected_collection_id()
        if col_id is None:
            return
        confirm = QMessageBox.question(
            self, 'Delete Collection',
            'Delete this collection? Cards are NOT deleted — only their '
            'membership in this collection is removed.',
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.db.delete_collection(col_id)
        self.changed = True
        self._refresh()


class CollectionAssignDialog(QDialog):
    """Assign one card to any number of collections via checkboxes."""

    def __init__(
        self,
        db: LibraryDatabase,
        char_id: int,
        char_name: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.char_id = char_id
        self.changed = False
        self.setWindowTitle(f"Collections — {char_name}")
        self.setMinimumWidth(340)

        layout = QVBoxLayout(self)
        hint = QLabel('Choose which collections this card belongs to:')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        member_ids = set(db.get_card_collection_ids(char_id))
        self._checkboxes: list[tuple[int, QCheckBox]] = []
        for col in db.list_collections():
            box = QCheckBox(f"{col['name']}  ({col['card_count']})")
            box.setChecked(col['id'] in member_ids)
            self._checkboxes.append((col['id'], box))
            layout.addWidget(box)

        if not self._checkboxes:
            empty = QLabel('No collections yet — create one first.')
            empty.setStyleSheet('color: #999; font-style: italic;')
            layout.addWidget(empty)

        new_btn = QPushButton('New Collection...')
        new_btn.clicked.connect(self._create_and_check)
        layout.addWidget(new_btn)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _create_and_check(self) -> None:
        from PyQt6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, 'New Collection', 'Name:')
        if not ok:
            return
        clean = normalize_collection_name(name)
        if not clean:
            return
        try:
            new_id = self.db.create_collection(clean)
        except Exception as exc:
            QMessageBox.warning(self, 'Collections', f'Could not create collection:\n{exc}')
            return
        self.changed = True
        box = QCheckBox(f"{clean}  (0)")
        box.setChecked(True)
        self._checkboxes.append((new_id, box))
        self.layout().insertWidget(self.layout().count() - 2, box)

    def selected_collection_ids(self) -> list[int]:
        return [
            col_id for col_id, box in self._checkboxes if box.isChecked()
        ]

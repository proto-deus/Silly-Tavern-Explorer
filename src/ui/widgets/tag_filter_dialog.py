from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class TagFilterDialog(QDialog):
    """Popup listing every known tag as a checkable list for filtering.

    Replaces the old inline checkbox strip so long tag names are never clipped:
    each tag is its own full-width, word-wrapped list row instead of a
    horizontally-flowing checkbox.
    """

    def __init__(
        self,
        all_tags: list[str],
        selected: set[str],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle('Filter by Tags')
        self.setMinimumSize(340, 420)

        layout = QVBoxLayout(self)

        header = QLabel('Select tags to filter by:')
        header.setStyleSheet('font-size: 12px; font-weight: bold; color: #ccc;')
        layout.addWidget(header)

        btn_row = QHBoxLayout()
        select_all_btn = QPushButton('Select All')
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        btn_row.addWidget(select_all_btn)
        select_none_btn = QPushButton('Select None')
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))
        btn_row.addWidget(select_none_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._list.setWordWrap(True)
        self._list.setTextElideMode(Qt.TextElideMode.ElideNone)
        layout.addWidget(self._list, 1)

        for tag in all_tags:
            item = QListWidgetItem(tag)
            item.setData(Qt.ItemDataRole.UserRole, tag)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if tag in selected else Qt.CheckState.Unchecked,
            )
            self._list.addItem(item)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(state)

    def selected_tags(self) -> list[str]:
        result: list[str] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.data(Qt.ItemDataRole.UserRole))
        return result

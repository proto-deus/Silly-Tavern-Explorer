from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from src.ui.widgets.flow_layout import FlowLayout


class TagChip(QWidget):
    removed = pyqtSignal(str)

    def __init__(self, tag: str, removable: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        self.tag = tag
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(2)

        label = QLabel(tag)
        # Tags come from card data (untrusted): plain text only.
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setStyleSheet('color: #d0d0d0;')
        layout.addWidget(label)

        if removable:
            btn = QPushButton('x')
            btn.setFixedSize(16, 16)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet('''
                QPushButton {
                    background: transparent;
                    color: #999;
                    border: none;
                    padding: 0px;
                    font-weight: bold;
                }
                QPushButton:hover { color: #ff6666; }
            ''')
            btn.clicked.connect(lambda: self.removed.emit(self.tag))
            layout.addWidget(btn)

        self.setStyleSheet('''
            TagChip {
                background-color: #3a3f4b;
                border: 1px solid #555;
                border-radius: 10px;
            }
        ''')


class TagWidget(QWidget):
    tags_changed = pyqtSignal(list)

    def __init__(self, editable: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        self._tags: list[str] = []
        self._editable = editable

        self._layout = FlowLayout(self, margin=2, h_spacing=4, v_spacing=4)

    def set_tags(self, tags: list[str]) -> None:
        self._tags = list(tags)
        self._rebuild()

    def get_tags(self) -> list[str]:
        return list(self._tags)

    def add_tag(self, tag: str) -> None:
        tag = tag.strip().lower()
        if tag and tag not in self._tags:
            self._tags.append(tag)
            self._rebuild()
            self.tags_changed.emit(self._tags)

    def _remove_tag(self, tag: str) -> None:
        if tag in self._tags:
            self._tags.remove(tag)
            self._rebuild()
            self.tags_changed.emit(self._tags)

    def _rebuild(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        for tag in self._tags:
            chip = TagChip(tag, removable=self._editable)
            if self._editable:
                chip.removed.connect(self._remove_tag)
            self._layout.addWidget(chip)

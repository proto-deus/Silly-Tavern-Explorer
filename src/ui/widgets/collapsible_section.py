from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QPushButton, QVBoxLayout, QWidget


class CollapsibleSection(QWidget):
    """A collapsible section with a clickable header and toggleable content."""

    def __init__(
        self,
        title: str,
        content_widget: QWidget,
        parent: QWidget | None = None,
        show_token_count: bool = True,
    ):
        super().__init__(parent)
        self._expanded = True
        self._title = title
        self._content = content_widget
        self._token_count = 0
        self._show_token_count = show_token_count

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QPushButton()
        self._header.setStyleSheet(
            'QPushButton { text-align: left; font-weight: bold;'
            ' color: #ccc; background-color: transparent; border: none;'
            ' padding: 6px 0; margin-top: 6px; }'
            ' QPushButton:hover { color: #5b9bd5; }'
        )
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setAccessibleName(title)
        self._header.clicked.connect(self.toggle)
        layout.addWidget(self._header)

        self._content_container = QWidget()
        content_layout = QVBoxLayout(self._content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(content_widget)
        layout.addWidget(self._content_container)

        # Build the header text here instead of hard-coding it, so a
        # section without a token counter does not start out with a
        # misleading "(0 Tokens)".
        self._refresh_header()

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._content_container.setVisible(expanded)
        self._refresh_header()

    def set_token_count(self, count: int) -> None:
        self._token_count = count
        self._refresh_header()

    def _refresh_header(self) -> None:
        arrow = '\u25bc' if self._expanded else '\u25b6'
        count = getattr(self, '_token_count', 0)
        if self._show_token_count:
            self._header.setText(f'{arrow} {self._title} ({count:,} Tokens)')
        else:
            self._header.setText(f'{arrow} {self._title}')

    def is_expanded(self) -> bool:
        return self._expanded

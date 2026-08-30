from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.database import LibraryDatabase
from src.ui.widgets.card_thumbnail import CardThumbnail


class CharacterSidebar(QWidget):
    """Shared character-card list for the Edit / Generate / Test tabs.

    A single instance is placed to the left of the tab widget so the three
    content tabs share the same selection, search and favorites filter.
    """

    card_selected = pyqtSignal(int)
    card_double_clicked = pyqtSignal(int)

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._thumb_widgets: list[CardThumbnail] = []
        self._current_id: int | None = None
        self._favorites_only: bool = False
        self._full_image_dlg: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 4, 8)

        left_label = QLabel('Characters')
        left_label.setStyleSheet('font-size: 14px; font-weight: bold; color: #e0e0e0; padding: 4px;')
        layout.addWidget(left_label)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText('Search characters...')
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._filter_thumbnails)
        layout.addWidget(self._search_box)

        self._thumb_scroll = QScrollArea()
        self._thumb_scroll.setWidgetResizable(True)
        self._thumb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._thumb_scroll.setFixedWidth(180)

        self._thumb_container = QWidget()
        self._thumb_layout = QVBoxLayout(self._thumb_container)
        self._thumb_layout.setContentsMargins(4, 4, 4, 4)
        self._thumb_layout.setSpacing(6)
        self._thumb_layout.addStretch()
        self._thumb_scroll.setWidget(self._thumb_container)
        layout.addWidget(self._thumb_scroll, 1)

        self._favorites_check = QCheckBox('Favorites only')
        self._favorites_check.setStyleSheet('font-size: 12px; color: #ccc;')
        self._favorites_check.toggled.connect(self._on_favorites_toggled)
        layout.addWidget(self._favorites_check)

    # ---- loading / filtering ----

    def load_cards(self) -> None:
        """Rebuild the thumbnail list from the DB, honouring the favorites filter."""
        for w in self._thumb_widgets:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self._thumb_widgets.clear()

        entries = self.db.get_all(favorites_only=self._favorites_only)
        for entry in entries:
            thumb = CardThumbnail(
                char_id=entry['id'],
                name=entry['name'],
                thumb_path=entry.get('thumbnail_path', ''),
                is_favorite=bool(entry.get('is_favorite')),
                token_count=entry.get('token_count', 0),
            )
            thumb.clicked.connect(self._on_thumb_clicked)
            thumb.double_clicked.connect(self._on_thumb_double_clicked)
            self._thumb_layout.insertWidget(self._thumb_layout.count() - 1, thumb)
            self._thumb_widgets.append(thumb)
        self._filter_thumbnails(self._search_box.text())
        self._apply_selection_highlight()

    def _on_favorites_toggled(self, checked: bool) -> None:
        self._favorites_only = checked
        self.load_cards()

    def _filter_thumbnails(self, text: str) -> None:
        query = text.strip().lower()
        for thumb in self._thumb_widgets:
            name = thumb._name_label.text().lower()
            thumb.setVisible(not query or query in name)

    # ---- selection ----

    def _apply_selection_highlight(self) -> None:
        for thumb in self._thumb_widgets:
            thumb.set_selected(thumb.char_id == self._current_id)

    def set_selected_id(self, char_id: int | None) -> None:
        """Set the highlighted card without loading anything."""
        self._current_id = char_id
        self._apply_selection_highlight()

    def current_id(self) -> int | None:
        return self._current_id

    def select_card(self, char_id: int) -> None:
        """Set selection and scroll the card into view."""
        self._current_id = char_id
        self._apply_selection_highlight()
        self._scroll_to_selected()

    def _scroll_to_selected(self) -> None:
        target_id = self._current_id

        def _do_scroll() -> None:
            for t in self._thumb_widgets:
                if t.char_id == target_id and not t.isHidden():
                    self._thumb_scroll.ensureWidgetVisible(t)
                    break

        QTimer.singleShot(0, _do_scroll)

    def scroll_selected_to_top(self) -> None:
        """Scroll so the selected card is at the top of the sidebar viewport."""
        target_id = self._current_id

        def _do_scroll() -> None:
            for t in self._thumb_widgets:
                if t.char_id == target_id and not t.isHidden():
                    self._thumb_scroll.verticalScrollBar().setValue(t.y())
                    break

        QTimer.singleShot(0, _do_scroll)

    def _on_thumb_clicked(self, char_id: int) -> None:
        if char_id == self._current_id:
            return
        self._current_id = char_id
        self._apply_selection_highlight()
        self.card_selected.emit(char_id)

    def _on_thumb_double_clicked(self, char_id: int) -> None:
        self.card_double_clicked.emit(char_id)

    # ---- maintenance ----

    def refresh_card(self, char_id: int) -> None:
        """Update a single thumbnail in place from the DB."""
        entry = self.db.get_by_id(char_id)
        if not entry:
            return
        for thumb in self._thumb_widgets:
            if thumb.char_id == char_id:
                thumb.update_from_entry(
                    name=entry['name'],
                    thumb_path=entry.get('thumbnail_path', ''),
                    is_favorite=bool(entry.get('is_favorite')),
                    token_count=entry.get('token_count', 0),
                )
                break

    def remove_card(self, char_id: int) -> None:
        """Remove a single thumbnail from the list."""
        for i, thumb in enumerate(self._thumb_widgets):
            if thumb.char_id == char_id:
                thumb.setParent(None)
                thumb.deleteLater()
                self._thumb_widgets.pop(i)
                break
        if self._current_id == char_id:
            self._current_id = None

    # ---- session restore ----

    def get_selected_id(self) -> int | None:
        return self._current_id

    def set_selected_ids(self, ids: list[int]) -> None:
        """Restore a saved selection (first matching id wins)."""
        valid = {t.char_id for t in self._thumb_widgets}
        for char_id in ids:
            if char_id in valid:
                self.set_selected_id(char_id)
                return

    def get_scroll_position(self) -> int:
        return self._thumb_scroll.verticalScrollBar().value()

    def set_scroll_position(self, position: int) -> None:
        self._thumb_scroll.verticalScrollBar().setValue(position)

    # ---- full image ----

    def show_full_image(self, char_id: int) -> None:
        entry = self.db.get_by_id(char_id)
        if not entry:
            return
        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            return
        from src.ui.widgets.image_viewer import ImageViewer

        existing = self._full_image_dlg
        if existing is not None:
            existing.close()
            existing.deleteLater()

        # Strong ref: the dialog is parented to this widget (Qt owns the C++
        # side), but without a Python reference the wrapper can be collected,
        # which would defeat the reuse check and stack duplicate viewers.
        dlg = ImageViewer(source, f"Full Image - {entry['name']}", self)
        dlg.show()
        self._full_image_dlg = dlg

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.database import SORT_LABELS, LibraryDatabase
from src.ui.widgets.async_image import cancel_pending_image_loads
from src.ui.widgets.card_thumbnail import CardThumbnail

# Owner tag for this list's queued thumbnail loads, so cancelling a rebuild of
# the Library grid's loads leaves the sidebar's alone (and vice versa).
SIDEBAR_LOADER_OWNER = 'sidebar'

#: How many cards the sidebar shows at most.  It is a quick picker, not the
#: full library: building a thumbnail widget (and queueing a PNG decode) for
#: every card of a large library froze the window on every refresh.
MAX_SIDEBAR_CARDS = 300


class CharacterSidebar(QWidget):
    """Shared character-card list for the Edit / Generate / Test tabs.

    A single instance is placed to the left of the tab widget so the three
    content tabs share the same selection, search and favorites filter.
    """

    card_selected = pyqtSignal(int)
    card_double_clicked = pyqtSignal(int)
    # Emitted when the user edits the search box; the Library tab mirrors it.
    search_changed = pyqtSignal(str)
    # The favorites filter and the sort order are shared with the Library
    # tab: they used to be independent, so "View > Favorites" left the
    # sidebar listing non-favourites and the two lists could disagree about
    # the order.
    favorites_filter_changed = pyqtSignal(bool)
    sort_changed = pyqtSignal(str)

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._thumb_widgets: list[CardThumbnail] = []
        self._search_blobs: dict[int, str] = {}
        self._current_id: int | None = None
        self._favorites_only: bool = False
        self._sort_by: str = 'name'
        self._full_image_dlg: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 4, 8)

        left_label = QLabel('Characters')
        left_label.setStyleSheet('font-weight: bold; color: #e0e0e0; padding: 4px;')
        layout.addWidget(left_label)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText('Search characters...')
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._on_search_text)
        layout.addWidget(self._search_box)

        # Same sort-by dropdown the Library tab uses (shared label->key map).
        sort_row = QHBoxLayout()
        sort_label = QLabel('Sort:')
        sort_label.setStyleSheet('color: #ccc;')
        sort_row.addWidget(sort_label)
        self._sort_combo = QComboBox()
        for label in SORT_LABELS:
            self._sort_combo.addItem(label)
        self._sort_combo.currentTextChanged.connect(self._on_sort_changed)
        sort_row.addWidget(self._sort_combo, 1)
        layout.addLayout(sort_row)

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
        self._favorites_check.setStyleSheet('color: #ccc;')
        self._favorites_check.toggled.connect(self._on_favorites_toggled)
        layout.addWidget(self._favorites_check)

        # The sidebar is a quick picker, not the full library: it shows at
        # most this many cards and says how many are hidden.  Without the
        # cap a library of a few thousand cards built a thumbnail widget
        # (and queued a PNG decode) for every one of them, on every import,
        # favourite toggle, sync and save.
        self._more_label = QLabel('')
        self._more_label.setWordWrap(True)
        self._more_label.setStyleSheet('color: #ccc; font-size: 11px;')
        self._more_label.setVisible(False)
        layout.addWidget(self._more_label)

    # ---- loading / filtering ----

    def load_cards(self) -> None:
        """Rebuild the thumbnail list from the DB, honouring sort and filters."""
        # Drop this list's queued image loads so a rebuild doesn't leave stale
        # decodes competing for the pool (the Library grid tags its own loads
        # separately and is unaffected).
        cancel_pending_image_loads(SIDEBAR_LOADER_OWNER)
        for w in self._thumb_widgets:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self._thumb_widgets.clear()
        self._search_blobs.clear()

        entries = self.db.get_all(light=True,
            sort_by=self._sort_by, favorites_only=self._favorites_only,
        )

        # Drop a selection that no longer exists (or is filtered out of view).
        # Keeping it left the sidebar highlighting a deleted card, which the
        # Edit tab then loaded (with Save enabled) and Push/Pull targeted.
        ids = {entry['id'] for entry in entries}
        if self._current_id is not None and self._current_id not in ids:
            self._current_id = None

        for entry in entries[:MAX_SIDEBAR_CARDS]:
            thumb = CardThumbnail(
                char_id=entry['id'],
                name=entry['name'],
                thumb_path=entry.get('thumbnail_path', ''),
                is_favorite=bool(entry.get('is_favorite')),
                token_count=entry.get('token_count', 0),
                loader_owner=SIDEBAR_LOADER_OWNER,
            )
            thumb.clicked.connect(self._on_thumb_clicked)
            thumb.double_clicked.connect(self._on_thumb_double_clicked)
            self._thumb_layout.insertWidget(self._thumb_layout.count() - 1, thumb)
            self._thumb_widgets.append(thumb)
            self._search_blobs[entry['id']] = ' '.join(
                str(entry.get(key) or '')
                for key in (
                    'name', 'description_preview', 'tags',
                    'creator', 'creator_notes',
                )
            ).lower()
        hidden = len(entries) - len(self._thumb_widgets)
        if hidden > 0:
            self._more_label.setText(
                f'{hidden} more card(s) not shown - narrow the search to see them.'
            )
            self._more_label.setVisible(True)
        else:
            self._more_label.setVisible(False)
        self._apply_search_filter(self._search_box.text())
        self._apply_selection_highlight()

    def _on_favorites_toggled(self, checked: bool) -> None:
        self._favorites_only = checked
        self.load_cards()
        self.favorites_filter_changed.emit(checked)

    def _on_sort_changed(self, label: str) -> None:
        self._sort_by = SORT_LABELS.get(label, 'name')
        self.load_cards()
        self.sort_changed.emit(label)

    def set_favorites_only(self, checked: bool) -> None:
        """Mirror the Library tab's "Favorites only" filter (no echo back)."""
        if self._favorites_only == checked:
            return
        self._favorites_only = checked
        self._favorites_check.blockSignals(True)
        self._favorites_check.setChecked(checked)
        self._favorites_check.blockSignals(False)
        self.load_cards()

    def set_sort_by_label(self, label: str) -> None:
        """Mirror the Library tab's sort order (no echo back)."""
        key = SORT_LABELS.get(label, 'name')
        if self._sort_by == key:
            return
        self._sort_by = key
        index = self._sort_combo.findText(label)
        if index < 0:
            index = self._sort_combo.findText('Name')
        if index >= 0:
            self._sort_combo.blockSignals(True)
            self._sort_combo.setCurrentIndex(index)
            self._sort_combo.blockSignals(False)
        self.load_cards()

    def _apply_search_filter(self, text: str) -> None:
        """Show only the cards matching *text* (same fields as the Library tab).

        Matches name, description, tags, creator, and creator notes, so the
        sidebar and the Library grid agree on the results for a given query.
        """
        query = text.strip().lower()
        for thumb in self._thumb_widgets:
            blob = self._search_blobs.get(thumb.char_id, '')
            thumb.setVisible(not query or query in blob)

    def _on_search_text(self, text: str) -> None:
        self._apply_search_filter(text)
        self.search_changed.emit(text)

    def set_search_text(self, text: str) -> None:
        """Mirror the Library tab's search box (no echo back to it)."""
        if self._search_box.text() == text:
            return
        self._search_box.blockSignals(True)
        self._search_box.setText(text)
        self._search_box.blockSignals(False)
        self._apply_search_filter(text)

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

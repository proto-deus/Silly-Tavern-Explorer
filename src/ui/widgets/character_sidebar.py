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
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.database import SORT_LABELS, LibraryDatabase
from src.ui.widgets.async_image import cancel_pending_image_loads
from src.ui.widgets.card_context_menu import show_card_context_menu
from src.ui.widgets.card_thumbnail import CardThumbnail
from src.ui.widgets.multi_select_dropdown import MultiSelectDropdown

# Owner tag for this list's queued thumbnail loads, so cancelling a rebuild of
# the Library grid's loads leaves the sidebar's alone (and vice versa).
SIDEBAR_LOADER_OWNER = 'sidebar'

#: How many cards are built per event-loop turn.  A thumbnail widget costs a
#: font-metrics measurement and a few layout operations, so building the whole
#: library in one go froze the window for seconds.  The list is built in small
#: batches instead, which keeps the UI responsive while it fills up.
BUILD_CHUNK_SIZE = 25

#: How far above/below the viewport a row may sit and still get its image
#: decoded.  Rows outside it keep their placeholder until they are scrolled
#: near, so a library of thousands of cards does not decode thousands of
#: images up front (and hold thousands of pixmaps in memory).
IMAGE_LOAD_MARGIN = 400

#: The gap around the rows of the list and between them.
ROW_MARGIN = 4
ROW_SPACING = 6


class CharacterSidebar(QWidget):
    """Shared character-card list for the Edit / Generate / Test tabs.

    A single instance is placed to the left of the tab widget so the three
    content tabs share the same selection, search and favorites filter.

    The list holds *every* card in the library - not just the first page of
    it - so a card picked in the Library grid (which is paged) is always
    present here and can always be followed.  To keep that affordable the
    list is built in small batches and the images are only decoded for the
    rows near the viewport.
    """

    card_selected = pyqtSignal(int)
    card_double_clicked = pyqtSignal(int)
    # A thumbnail's context menu picked an action: (action key, char_id).
    # The actions live in MainWindow / the Library tab; this list only shows
    # the menu, so it forwards the choice instead of implementing it.
    context_action = pyqtSignal(str, int)
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
        self._thumbs_by_id: dict[int, CardThumbnail] = {}
        self._search_blobs: dict[int, str] = {}
        self._current_id: int | None = None
        self._favorites_only: bool = False
        self._sort_by: str = 'name'
        self._full_image_dlg: QWidget | None = None
        # Every card in the library (light rows), plus the state of the
        # batch-wise build that turns them into thumbnails.
        self._entries: list[dict] = []
        self._build_token: int = 0
        self._build_pos: int = 0
        self._build_done: bool = True
        self._removed_ids: set[int] = set()
        # (widget, y) of every row that is currently shown, in list order.
        # The layout only catches up when Qt repaints, so the positions are
        # tracked here: they decide which images are worth decoding.
        self._rows: list[tuple[CardThumbnail, int]] = []
        # A selection made before its row existed: scrolled to as soon as
        # the row is built.
        self._pending_scroll_id: int | None = None
        # The scroll position of the list, kept across a rebuild.
        self._saved_scroll: int = 0

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

        cat_row = QHBoxLayout()
        cat_label = QLabel('Coll:')
        cat_label.setStyleSheet('color: #ccc;')
        cat_row.addWidget(cat_label)
        self._collection_filter = MultiSelectDropdown(empty_text='All')
        # Long collection names must not widen the sidebar: the scroll area
        # is fixed at 180px, but the button caption grows to fit its longest
        # label, which would stretch this whole column and leave a gap next
        # to the tab widget.  The width policy drops it from the size hint.
        self._collection_filter.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self._collection_filter.setToolTip(
            'Filter by collection - check several to combine them'
        )
        self._collection_filter.selection_changed.connect(
            self._on_collection_changed
        )
        cat_row.addWidget(self._collection_filter, 1)
        layout.addLayout(cat_row)

        self._thumb_scroll = QScrollArea()
        self._thumb_scroll.setWidgetResizable(True)
        self._thumb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._thumb_scroll.setFixedWidth(180)
        self._thumb_scroll.verticalScrollBar().valueChanged.connect(
            self._on_scrolled,
        )

        self._thumb_container = QWidget()
        # The rows are placed by hand (see ``_layout_rows``) instead of by a
        # layout: a layout re-lays-out every row each time one of them is
        # shown or hidden, which makes a list of thousands of cards quadratic.
        self._thumb_container.setMinimumHeight(0)
        self._thumb_scroll.setWidget(self._thumb_container)
        layout.addWidget(self._thumb_scroll, 1)

        self._favorites_check = QCheckBox('Favorites only')
        self._favorites_check.setStyleSheet('color: #ccc;')
        self._favorites_check.toggled.connect(self._on_favorites_toggled)
        layout.addWidget(self._favorites_check)

        # The list shows every card, so the label counts them instead of
        # reporting how many were left out.
        self._status_label = QLabel('')
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet('color: #ccc; font-size: 11px;')
        layout.addWidget(self._status_label)

    # ---- loading / filtering ----

    def load_cards(self) -> None:
        """Rebuild the thumbnail list from the DB, honouring sort and filters.

        The whole library is listed, but it is built in batches: the first
        batch is ready when this returns, the rest follows on the event loop.
        """
        # Invalidate a build that is still running: its remaining batches
        # would add cards the new query does not contain.
        self._build_token += 1
        # Drop this list's queued image loads so a rebuild doesn't leave stale
        # decodes competing for the pool (the Library grid tags its own loads
        # separately and is unaffected).
        cancel_pending_image_loads(SIDEBAR_LOADER_OWNER)
        # The list is rebuilt from scratch, so the scroll position has to be
        # put back once the new list is there.
        self._saved_scroll = self._thumb_scroll.verticalScrollBar().value()
        self._destroy_thumbs()
        self._removed_ids.clear()

        self._entries = self.db.get_all(
            light=True,
            sort_by=self._sort_by,
            favorites_only=self._favorites_only,
        )
        selected = set(self._collection_filter.selected_values())
        if selected:
            membership = self.db.get_collection_membership()
            wanted = {c for c in selected if c >= 0}
            include_uncollected = any(c < 0 for c in selected)

            def keep(entry: dict) -> bool:
                cols = membership.get(entry['id'], set())
                if include_uncollected and not cols:
                    # Sentinel: cards that belong to no collection at all.
                    return True
                return bool(cols & wanted)

            self._entries = [e for e in self._entries if keep(e)]
        self._refresh_collection_combo()

        # Drop a selection that no longer exists (or is filtered out of view).
        # Keeping it left the sidebar highlighting a deleted card, which the
        # Edit tab then loaded (with Save enabled) and Push/Pull targeted.
        ids = {entry['id'] for entry in self._entries}
        if self._current_id is not None and self._current_id not in ids:
            self._current_id = None

        self._build_pos = 0
        self._build_done = False
        self._build_batch(self._build_token)

    def _destroy_thumbs(self) -> None:
        for w in self._thumb_widgets:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self._thumb_widgets.clear()
        self._thumbs_by_id.clear()
        self._search_blobs.clear()
        self._rows.clear()
        self._thumb_container.setMinimumHeight(0)

    def _build_batch(self, token: int) -> None:
        """Build the next batch of thumbnails.

        The list is built one batch per event-loop turn, so a library with
        thousands of cards does not freeze the window.  *token* identifies
        the build: a rebuild bumps it and the stale batch stops.
        """
        if token != self._build_token:
            return
        query = self._search_box.text().strip().lower()
        entries = self._entries[self._build_pos:self._build_pos + BUILD_CHUNK_SIZE]
        appended: list[CardThumbnail] = []
        for entry in entries:
            char_id = entry['id']
            if char_id in self._removed_ids:
                continue
            thumb = CardThumbnail(
                char_id=char_id,
                name=entry['name'],
                thumb_path=entry.get('thumbnail_path', ''),
                is_favorite=bool(entry.get('is_favorite')),
                token_count=entry.get('token_count', 0),
                parent=self._thumb_container,
                loader_owner=SIDEBAR_LOADER_OWNER,
                # The image is decoded when the row scrolls near the
                # viewport, not up front.
                auto_load=False,
            )
            thumb.clicked.connect(self._on_thumb_clicked)
            thumb.double_clicked.connect(self._on_thumb_double_clicked)
            thumb.context_menu_requested.connect(self._on_thumb_context_menu)
            self._thumb_widgets.append(thumb)
            self._thumbs_by_id[char_id] = thumb
            self._search_blobs[char_id] = ' '.join(
                str(entry.get(key) or '')
                for key in (
                    'name', 'description_preview', 'tags',
                    'creator', 'creator_notes',
                )
            ).lower()
            thumb.setVisible(self._row_matches(thumb, query))
            thumb.set_selected(char_id == self._current_id)
            appended.append(thumb)
        self._build_pos += len(entries)
        self._layout_rows(appended)
        self._load_visible_images()
        self._flush_pending_scroll()
        self._update_status_label()
        if self._build_pos < len(self._entries):
            QTimer.singleShot(0, lambda t=token: self._build_batch(t))
        else:
            self._build_done = True
            self._flush_pending_scroll()
            self._update_status_label()
            # Put the list back where the user left it.
            if self._saved_scroll:
                QTimer.singleShot(0, self._restore_scroll)

    def _restore_scroll(self) -> None:
        """Restore the scroll position of a rebuilt list."""
        self._thumb_scroll.verticalScrollBar().setValue(self._saved_scroll)

    def _refresh_collection_combo(self) -> None:
        """Rebuild the collection dropdown from the library's collections.

        Mirrors the Library tab's dropdown: 'No Collection', then every
        collection with its card count.  A checked collection that still
        exists keeps its check; a deleted one is dropped and the filter
        falls back to the remaining checks (or 'All' when none are left).
        ``set_options`` never emits ``selection_changed``, so the
        re-population cannot re-trigger a reload.
        """
        options: list[tuple[str, int]] = [('No Collection', -1)]
        for col in self.db.list_collections():
            options.append((f"{col['name']} ({col['card_count']})", int(col['id'])))
        self._collection_filter.set_options(options)

    def _on_collection_changed(self) -> None:
        self.load_cards()

    def _layout_rows(self, appended: list[CardThumbnail] | None = None) -> None:
        """Place the shown rows and size the scrollable area.

        The rows are positioned by hand rather than by a layout: a layout
        re-lays-out every row each time one of them is shown or hidden, and
        with thousands of cards in the list that turns every filter change
        into quadratic work.  The positions are also used to decide which
        images are worth decoding.

        *appended* are the rows that were just added at the end of the list.
        While nothing is filtered out they simply go below the last row,
        which keeps a batch of new rows cheap.
        """
        if appended and not self._search_box.text().strip():
            y = (
                self._rows[-1][1] + self._rows[-1][0].height() + ROW_SPACING
                if self._rows
                else ROW_MARGIN
            )
            for thumb in appended:
                thumb.move(ROW_MARGIN, y)
                self._rows.append((thumb, y))
                y += thumb.height() + ROW_SPACING
            self._thumb_container.setMinimumHeight(
                max(0, y - ROW_SPACING + ROW_MARGIN),
            )
            return
        y = ROW_MARGIN
        rows: list[tuple[CardThumbnail, int]] = []
        for thumb in self._thumb_widgets:
            if thumb.isHidden():
                continue
            thumb.move(ROW_MARGIN, y)
            rows.append((thumb, y))
            y += thumb.height() + ROW_SPACING
        self._rows = rows
        # The minimum height is what the scroll area scrolls through.
        self._thumb_container.setMinimumHeight(
            max(0, y - ROW_SPACING + ROW_MARGIN),
        )

    def _load_visible_images(self) -> None:
        """Decode the images of the rows that are near the viewport.

        Only those rows are decoded: the list holds the whole library, and
        decoding every image in it would cost both time and a lot of memory
        (a pixmap per card).
        """
        bar = self._thumb_scroll.verticalScrollBar()
        top = bar.value() - IMAGE_LOAD_MARGIN
        bottom = bar.value() + self._thumb_scroll.viewport().height() + IMAGE_LOAD_MARGIN
        for thumb, y in self._rows:
            if top <= y <= bottom:
                thumb.load_image()

    def _on_scrolled(self, _value: int) -> None:
        """Load the images of the rows that just scrolled into view."""
        self._load_visible_images()

    def showEvent(self, event) -> None:
        """Load the images of the rows that are on screen when the list shows."""
        super().showEvent(event)
        self._load_visible_images()
        # A selection made while the sidebar was hidden (a Library-grid
        # click) could not scroll: a list that has never been laid out has
        # an empty scroll range, so the value would have been clamped to 0
        # and the position lost.  Catch up now that the widget is shown;
        # deferred one turn so the layout has assigned the real range.
        QTimer.singleShot(0, self._flush_pending_scroll)

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

    def _row_matches(self, thumb: CardThumbnail, query: str) -> bool:
        """Whether *thumb* should be shown for the search *query*.

        The selected card is always shown, even when the query does not match
        it: the Library grid can match a card on fields the sidebar does not
        search (the full card body), and the two lists have to agree on
        which card is selected.
        """
        if not query:
            return True
        if thumb.char_id == self._current_id:
            return True
        return query in self._search_blobs.get(thumb.char_id, '')

    def _apply_search_filter(self, text: str) -> None:
        """Show only the cards matching *text* (same fields as the Library tab).

        Matches name, description, tags, creator, and creator notes, so the
        sidebar and the Library grid agree on the results for a given query.
        """
        query = text.strip().lower()
        for thumb in self._thumb_widgets:
            thumb.setVisible(self._row_matches(thumb, query))
        self._layout_rows()
        self._load_visible_images()
        self._update_status_label()

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

    def _update_status_label(self) -> None:
        """Show how many cards the list holds (and how many match the search)."""
        total = len(self._entries) - len(self._removed_ids)
        if not self._build_done:
            self._status_label.setText(
                f'Loading {len(self._thumb_widgets):,} of {total:,}...'
            )
            return
        shown = sum(1 for thumb in self._thumb_widgets if not thumb.isHidden())
        if shown >= total:
            self._status_label.setText(
                f'{total:,} card' + ('' if total == 1 else 's')
            )
        else:
            self._status_label.setText(f'{shown:,} of {total:,} cards')

    # ---- selection ----

    def _apply_selection_highlight(self) -> None:
        for thumb in self._thumb_widgets:
            thumb.set_selected(thumb.char_id == self._current_id)

    def _set_current(self, char_id: int | None) -> None:
        """Change the selected card and bring the list in line with it."""
        previous = self._current_id
        self._current_id = char_id
        self._apply_selection_highlight()
        # The newly selected card has to be shown even when the search text
        # does not match it, and the one before it goes back to the search.
        query = self._search_box.text().strip().lower()
        relayout = False
        for cid in (previous, char_id):
            thumb = self._thumbs_by_id.get(cid) if cid is not None else None
            if thumb is None:
                continue
            visible = self._row_matches(thumb, query)
            if thumb.isHidden() == visible:
                thumb.setVisible(visible)
                relayout = True
        if relayout:
            self._layout_rows()

    def set_selected_id(self, char_id: int | None) -> None:
        """Set the highlighted card without loading anything."""
        self._set_current(char_id)

    def current_id(self) -> int | None:
        return self._current_id

    def select_card(self, char_id: int) -> None:
        """Set selection and scroll the card into view."""
        self._set_current(char_id)
        self._scroll_to_selected()

    def _scroll_to_selected(self) -> None:
        target_id = self._current_id

        def _do_scroll() -> None:
            if self._scroll_to_row(target_id):
                self._pending_scroll_id = None
                return
            # The row is not built yet (the list is still being built): keep
            # the target and scroll to it as soon as it shows up.
            self._pending_scroll_id = target_id

        QTimer.singleShot(0, _do_scroll)

    def _scroll_to_row(self, char_id: int | None) -> bool:
        """Scroll the row of *char_id* to the top of the viewport.

        Returns False when the row is not in the list (yet) - or when the
        list cannot scroll at all: a sidebar that has never been laid out
        (it is hidden while the Library tab is up) has a scrollbar range
        of 0, and setting the value then would silently clamp it to 0
        while reporting success.  Reporting False keeps the target pending
        so it is retried once the sidebar is shown.
        """
        if char_id is None:
            return False
        for thumb, y in self._rows:
            if thumb.char_id != char_id:
                continue
            bar = self._thumb_scroll.verticalScrollBar()
            if bar.maximum() <= 0 and y > 0:
                return False
            bar.setValue(max(0, min(y, bar.maximum())))
            return True
        return False

    def _flush_pending_scroll(self) -> None:
        """Scroll to a selection whose row was not built yet."""
        if self._pending_scroll_id is None:
            return
        if self._scroll_to_row(self._pending_scroll_id):
            self._pending_scroll_id = None

    def _on_thumb_clicked(self, char_id: int) -> None:
        if char_id == self._current_id:
            return
        self._set_current(char_id)
        self.card_selected.emit(char_id)

    def _on_thumb_double_clicked(self, char_id: int) -> None:
        self.card_double_clicked.emit(char_id)

    def _on_thumb_context_menu(self, char_id: int, global_pos) -> None:
        """Right-click on a row: show the shared card menu.

        The row is selected first so the menu and the rest of the app agree
        on the target.  The choices are forwarded as ``context_action``; the
        handlers live with the Library tab / MainWindow.
        """
        self._on_thumb_clicked(char_id)
        entry = self.db.get_by_id(char_id)
        if not entry:
            return
        handlers = {
            key: (lambda k=key: self.context_action.emit(k, char_id))
            for key in (
                'edit', 'favorite', 'collections', 'full_image',
                'open_folder', 'export_png', 'export_json', 'duplicate',
                'delete',
            )
        }
        show_card_context_menu(
            self, global_pos, handlers,
            favorite_on=bool(entry.get('is_favorite')),
        )

    # ---- maintenance ----

    def refresh_card(self, char_id: int) -> None:
        """Update a single thumbnail in place from the DB."""
        entry = self.db.get_by_id(char_id)
        if not entry:
            return
        thumb = self._thumbs_by_id.get(char_id)
        if thumb is None:
            return
        thumb.update_from_entry(
            name=entry['name'],
            thumb_path=entry.get('thumbnail_path', ''),
            is_favorite=bool(entry.get('is_favorite')),
            token_count=entry.get('token_count', 0),
        )
        # A new name can wrap onto another line, which changes the height of
        # the row and the position of the rows below it.
        self._layout_rows()

    def remove_card(self, char_id: int) -> None:
        """Remove a single thumbnail from the list."""
        # Remember the removal: a build that is still running would otherwise
        # add the card back.
        self._removed_ids.add(char_id)
        thumb = self._thumbs_by_id.pop(char_id, None)
        if thumb is not None:
            # Hide before reparenting: a widget without a parent becomes a
            # top-level window and would flash on screen.
            thumb.hide()
            thumb.setParent(None)
            thumb.deleteLater()
            try:
                self._thumb_widgets.remove(thumb)
            except ValueError:
                pass
        if self._current_id == char_id:
            self._current_id = None
        self._layout_rows()
        self._update_status_label()

    # ---- session restore ----

    def get_selected_id(self) -> int | None:
        return self._current_id

    def set_selected_ids(self, ids: list[int]) -> None:
        """Restore a saved selection (first matching id wins)."""
        known = {entry['id'] for entry in self._entries}
        known.update(self._thumbs_by_id)
        for char_id in ids:
            if char_id in known:
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

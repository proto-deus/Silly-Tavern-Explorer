from __future__ import annotations

import json
import logging
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.card_models import CharacterCard
from src.card_parser import read_card_data
from src.database import LibraryDatabase, sanitize_filename
from src.settings_manager import load_font_size
from src.ui.dialog_helper import exec_dialog, exec_dialog_with
from src.ui.widgets.async_image import (
    THUMBNAIL_LOADER_OWNER,
    cancel_pending_image_loads,
)
from src.ui.widgets.card_thumbnail import CardThumbnail
from src.ui.widgets.flow_layout import FlowLayout
from src.ui.widgets.rating_widget import RatingWidget
from src.ui.widgets.tag_widget import TagChip, TagWidget

logger = logging.getLogger(__name__)

# File types that can be drag-and-dropped / imported.  Lower-cased suffixes
# (no dot) so the check is suffix-agnostic.
_IMPORTABLE_EXTENSIONS: tuple[str, ...] = ('.png', '.json')


def _filter_importable_paths(paths: list[str]) -> list[str]:
    """Return only the paths whose suffix is an importable file type.

    Pure function so the filter logic can be unit-tested without Qt.
    Case-insensitive on the file extension.
    """
    return [p for p in paths if Path(p).suffix.lower() in _IMPORTABLE_EXTENSIONS]


def _build_import_summary(imported: int, skipped: int) -> str:
    """Build the short status-bar summary for an import operation.

    Pure function so the message formatting can be unit-tested without Qt.
    """
    msg = f"Imported {imported} card(s)."
    if skipped:
        msg += f" Skipped {skipped} duplicate(s)."
    return msg


class SelectionModel:
    """Track multi-selection state for the card grid.

    Pure-Python helper (no Qt) so the selection logic — plain click, Ctrl+click
    toggle and Shift+click range — can be unit-tested without a running Qt
    event loop.  ``_ids`` is an ordered list (preserves click order) and
    ``_anchor`` is the last anchor used for Shift+click range extension.
    """

    def __init__(self) -> None:
        self._ids: list[int] = []
        self._anchor: int | None = None

    @property
    def selected_ids(self) -> list[int]:
        return list(self._ids)

    @property
    def anchor(self) -> int | None:
        return self._anchor

    def __len__(self) -> int:
        return len(self._ids)

    def is_selected(self, char_id: int) -> bool:
        return char_id in self._ids

    def select_single(self, char_id: int) -> None:
        """Replace the selection with a single id (plain click)."""
        self._ids = [char_id]
        self._anchor = char_id

    def toggle(self, char_id: int) -> None:
        """Ctrl+click: add or remove a single id without clearing others."""
        if char_id in self._ids:
            self._ids.remove(char_id)
        else:
            self._ids.append(char_id)
        self._anchor = char_id

    def extend_range(self, char_id: int, all_ids: list[int]) -> None:
        """Shift+click: select the range from the anchor to *char_id* inclusive.

        Additive — merges the range into the current selection so Ctrl+click
        then Shift+click accumulates.  *all_ids* is the full ordered list of
        currently-displayed ids so a contiguous range can be computed.
        """
        if self._anchor is None or self._anchor not in all_ids:
            self.select_single(char_id)
            return
        if char_id not in all_ids:
            return
        anchor_idx = all_ids.index(self._anchor)
        click_idx = all_ids.index(char_id)
        lo, hi = min(anchor_idx, click_idx), max(anchor_idx, click_idx)
        for rid in all_ids[lo:hi + 1]:
            if rid not in self._ids:
                self._ids.append(rid)
        self._anchor = char_id

    def set_selection(self, char_ids: list[int]) -> None:
        """Replace the whole selection (used when restoring after a rebuild)."""
        self._ids = list(char_ids)
        self._anchor = self._ids[-1] if self._ids else None

    def prune(self, valid_ids: set[int]) -> None:
        """Drop any ids no longer present in the grid."""
        self._ids = [i for i in self._ids if i in valid_ids]
        if self._anchor is not None and self._anchor not in valid_ids:
            self._anchor = self._ids[-1] if self._ids else None

    def clear(self) -> None:
        self._ids = []
        self._anchor = None


class _DetailContainer(QWidget):
    """Detail pane that caps the summary's height to ~1/3 of its own height."""

    def __init__(self, summary_scroll: QScrollArea, parent: QWidget | None = None):
        super().__init__(parent)
        self._summary_scroll = summary_scroll

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._summary_scroll.setMaximumHeight(max(48, int(self.height() / 3)))


class LibraryTab(QWidget):
    card_selected = pyqtSignal(int)
    edit_requested = pyqtSignal(int)
    library_changed = pyqtSignal()
    status_message = pyqtSignal(str, int)
    sort_label_changed = pyqtSignal(str)
    selection_changed = pyqtSignal(list)
    settings_requested = pyqtSignal()

    # Map dropdown display text -> DB sort key (mirrors the whitelist in
    # LibraryDatabase._SORT_WHITELIST so invalid values can never reach SQL).
    _SORT_LABELS: dict[str, str] = {
        'Name': 'name',
        'Date Added': 'date_added',
        'Token Count': 'token_count',
        'Favorites': 'is_favorite',
        'Rating': 'rating',
        'Random': 'random',
    }

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.db = db
        self._thumbnails: list[CardThumbnail] = []
        self._selected_id: int | None = None
        self._selection = SelectionModel()
        self._all_ids: list[int] = []
        self._full_image_dlg: QWidget | None = None
        self._selected_filter_tags: set[str] = set()
        self._sort_by: str = 'name'
        self._thumb_img_size: int = 152
        # In-memory cache of card data read from disk, keyed by char_id.
        # Invalidated when a card is updated or deleted.
        self._card_cache: dict[int, CharacterCard] = {}
        # Async import worker + progress dialog (Phase 8A).  ``_import_force_pending``
        # guards against repeatedly prompting to force-import duplicates.
        # ``_import_cancelled`` is set when the user cancels the progress dialog
        # so the finished-handler skips the "import duplicates anyway?" prompt.
        self._import_worker = None
        self._progress_dialog: QProgressDialog | None = None
        self._import_force_pending = False
        self._import_cancelled = False

        # Debounce timer for search input so we don't hit the DB on every keystroke.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_filters)

        # Debounce timer for thumbnail zoom so repeated Ctrl+=/Ctrl+- don't flood
        # the thread pool with one reload per step.
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(120)
        self._zoom_timer.timeout.connect(self._apply_zoom)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Row 1: search + primary actions.
        toolbar = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText('Search characters...')
        self._search_edit.textChanged.connect(self._on_search)
        toolbar.addWidget(self._search_edit, 1)

        import_btn = QPushButton('Import Card')
        import_btn.clicked.connect(self._import_card)
        toolbar.addWidget(import_btn)

        refresh_btn = QPushButton('Refresh')
        refresh_btn.clicked.connect(self.load_cards)
        toolbar.addWidget(refresh_btn)

        dup_btn = QPushButton('Find Duplicates')
        dup_btn.clicked.connect(self.find_duplicates)
        toolbar.addWidget(dup_btn)

        settings_btn = QPushButton('Settings')
        settings_btn.clicked.connect(self._on_settings_requested)
        toolbar.addWidget(settings_btn)

        layout.addLayout(toolbar)

        # Row 2: sort + view filters (kept on its own row so nothing is
        # squashed when the window is narrow).
        filter_row = QHBoxLayout()
        sort_label = QLabel('Sort:')
        sort_label.setStyleSheet('color: #ccc;')
        filter_row.addWidget(sort_label)
        self._sort_combo = QComboBox()
        for label in self._SORT_LABELS:
            self._sort_combo.addItem(label)
        self._sort_combo.currentTextChanged.connect(self._on_sort_changed)
        filter_row.addWidget(self._sort_combo)

        self._favorites_only_cb = QCheckBox('Favorites only')
        self._favorites_only_cb.setStyleSheet('color: #ccc;')
        self._favorites_only_cb.toggled.connect(self._apply_filters)
        filter_row.addWidget(self._favorites_only_cb)

        self._collection_combo = QComboBox()
        self._collection_combo.setToolTip('Filter by collection')
        self._collection_combo.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self._collection_combo)

        self._manage_cols_btn = QPushButton('Manage...')
        self._manage_cols_btn.setToolTip('Create, rename, or delete collections')
        self._manage_cols_btn.clicked.connect(self._on_manage_collections)
        filter_row.addWidget(self._manage_cols_btn)

        filter_row.addStretch()
        layout.addLayout(filter_row)

        filter_container = QWidget()
        filter_layout = QVBoxLayout(filter_container)
        filter_layout.setContentsMargins(0, 4, 0, 4)
        filter_layout.setSpacing(2)

        # The filter bar is a single wrapping row: the label, the button that
        # opens the tag-selection popup, the clear button, and the currently
        # selected tags (shown as removable chips) all flow together.
        self._filter_bar_scroll = QScrollArea()
        self._filter_bar_scroll.setMaximumHeight(max(106, load_font_size() * 9))
        self._filter_bar_scroll.setWidgetResizable(True)
        self._filter_bar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._filter_bar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._filter_bar_widget = QWidget()
        self._filter_bar_layout = FlowLayout(self._filter_bar_widget, margin=2, h_spacing=4, v_spacing=2, wrap_fudge=8)
        self._filter_bar_scroll.setWidget(self._filter_bar_widget)

        filter_label = QLabel('Filter by tags:')
        filter_label.setStyleSheet('font-weight: bold; color: #ccc;')
        self._filter_bar_layout.addWidget(filter_label)

        self._tag_filter_btn = QPushButton('Select Tags...')
        self._tag_filter_btn.clicked.connect(self._open_tag_filter_dialog)
        self._filter_bar_layout.addWidget(self._tag_filter_btn)

        self._clear_tags_btn = QPushButton('Clear')
        self._clear_tags_btn.setEnabled(False)
        self._clear_tags_btn.clicked.connect(self._clear_tag_filter)
        self._filter_bar_layout.addWidget(self._clear_tags_btn)

        # The three widgets above are the fixed header; selected-tag chips are
        # appended after them by _refresh_tag_filter().
        self._filter_header_count = 3

        filter_layout.addWidget(self._filter_bar_scroll)
        filter_container.setMaximumHeight(max(160, load_font_size() * 9 + 40))
        layout.addWidget(filter_container)

        splitter = QSplitter(Qt.Orientation.Vertical)

        grid_container = QWidget()
        self._grid_layout = FlowLayout(grid_container, margin=8, h_spacing=8, v_spacing=8)
        self._grid_scroll = QScrollArea()
        self._grid_scroll.setWidgetResizable(True)
        self._grid_scroll.setWidget(grid_container)
        splitter.addWidget(self._grid_scroll)

        self._summary_scroll = QScrollArea()
        self._summary_scroll.setWidgetResizable(True)
        self._summary_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._summary_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        detail_container = _DetailContainer(self._summary_scroll)
        detail_layout = QVBoxLayout(detail_container)
        detail_layout.setContentsMargins(8, 8, 8, 8)

        self._detail_name = QLabel('Select a character')
        self._detail_name.setStyleSheet('font-weight: bold; color: #e0e0e0;')
        detail_layout.addWidget(self._detail_name)

        self._detail_meta = QLabel('')
        self._detail_meta.setStyleSheet('color: #aaa;')
        detail_layout.addWidget(self._detail_meta)

        rating_row = QHBoxLayout()
        rating_label = QLabel('Rating:')
        rating_label.setStyleSheet('color: #aaa;')
        rating_row.addWidget(rating_label)
        self._detail_rating = RatingWidget()
        self._detail_rating.setEnabled(False)
        self._detail_rating.ratingChanged.connect(self._on_rating_changed)
        rating_row.addWidget(self._detail_rating)
        rating_row.addStretch()
        detail_layout.addLayout(rating_row)

        self._detail_tags = TagWidget(editable=False)
        tags_scroll = QScrollArea()
        tags_scroll.setWidgetResizable(True)
        tags_scroll.setFrameShape(QFrame.Shape.NoFrame)
        tags_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tags_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        tags_scroll.setMaximumHeight(60)
        tags_scroll.setWidget(self._detail_tags)
        detail_layout.addWidget(tags_scroll)

        self._detail_summary = QLabel('')
        self._detail_summary.setWordWrap(True)
        self._detail_summary.setStyleSheet(
            'color: #b0b0b0; font-style: italic; '
            'padding: 6px 4px; background-color: #222; border-radius: 4px;'
        )
        self._summary_scroll.setWidget(self._detail_summary)
        self._summary_scroll.setVisible(False)
        detail_layout.addWidget(self._summary_scroll)

        self._detail_text = QTextEdit()
        self._detail_text.setReadOnly(True)
        self._detail_text.setMaximumHeight(220)
        detail_layout.addWidget(self._detail_text)

        btn_row = QHBoxLayout()
        self._fav_btn = QPushButton('Favorite')
        self._fav_btn.setCheckable(True)
        self._fav_btn.setEnabled(False)
        self._fav_btn.clicked.connect(self._on_toggle_favorite)
        btn_row.addWidget(self._fav_btn)

        self._edit_btn = QPushButton('Edit')
        self._edit_btn.setEnabled(False)
        self._edit_btn.clicked.connect(self._on_edit)
        btn_row.addWidget(self._edit_btn)

        self._collections_btn = QPushButton('Collections')
        self._collections_btn.setEnabled(False)
        self._collections_btn.setToolTip("Choose which collections this card belongs to")
        self._collections_btn.clicked.connect(self._on_assign_collections)
        btn_row.addWidget(self._collections_btn)

        self._delete_btn = QPushButton('Delete')
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self._delete_btn)

        self._export_png_btn = QPushButton('Export as PNG')
        self._export_png_btn.setEnabled(False)
        self._export_png_btn.clicked.connect(self._on_export_png)
        btn_row.addWidget(self._export_png_btn)

        self._export_json_btn = QPushButton('Export as JSON')
        self._export_json_btn.setEnabled(False)
        self._export_json_btn.clicked.connect(self._on_export_json)
        btn_row.addWidget(self._export_json_btn)

        self._open_folder_btn = QPushButton('Open Folder')
        self._open_folder_btn.setEnabled(False)
        self._open_folder_btn.clicked.connect(self._on_open_folder)
        btn_row.addWidget(self._open_folder_btn)

        self._bulk_delete_btn = QPushButton('Bulk Delete')
        self._bulk_delete_btn.setVisible(False)
        self._bulk_delete_btn.clicked.connect(self._on_bulk_delete)
        btn_row.addWidget(self._bulk_delete_btn)

        self._bulk_favorite_btn = QPushButton('Bulk Favorite')
        self._bulk_favorite_btn.setVisible(False)
        self._bulk_favorite_btn.clicked.connect(lambda: self._on_bulk_favorite(True))
        btn_row.addWidget(self._bulk_favorite_btn)

        self._bulk_unfavorite_btn = QPushButton('Bulk Unfavorite')
        self._bulk_unfavorite_btn.setVisible(False)
        self._bulk_unfavorite_btn.clicked.connect(lambda: self._on_bulk_favorite(False))
        btn_row.addWidget(self._bulk_unfavorite_btn)

        self._bulk_export_png_btn = QPushButton('Bulk Export PNG')
        self._bulk_export_png_btn.setVisible(False)
        self._bulk_export_png_btn.clicked.connect(self._on_bulk_export_png)
        btn_row.addWidget(self._bulk_export_png_btn)

        self._bulk_export_json_btn = QPushButton('Bulk Export JSON')
        self._bulk_export_json_btn.setVisible(False)
        self._bulk_export_json_btn.clicked.connect(self._on_bulk_export_json)
        btn_row.addWidget(self._bulk_export_json_btn)

        self._single_btns = [
            self._fav_btn, self._edit_btn, self._collections_btn,
            self._delete_btn,
            self._export_png_btn, self._export_json_btn,
            self._open_folder_btn,
        ]
        self._bulk_btns = [
            self._bulk_delete_btn, self._bulk_favorite_btn,
            self._bulk_unfavorite_btn, self._bulk_export_png_btn,
            self._bulk_export_json_btn,
        ]

        btn_row.addStretch()
        detail_layout.addLayout(btn_row)

        splitter.addWidget(detail_container)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([600, 200])
        layout.addWidget(splitter, 1)

        self._refresh_collection_combo()

    def load_cards(self) -> None:
        self._refresh_collection_combo()
        self._refresh_tag_filter()
        self._apply_filters()
        self.library_changed.emit()

    def _refresh_collection_combo(self) -> None:
        """Rebuild the collection filter dropdown, preserving the selection."""
        current = self._collection_combo.currentData()
        self._collection_combo.blockSignals(True)
        self._collection_combo.clear()
        self._collection_combo.addItem('All Cards', None)
        self._collection_combo.addItem('No Collection', -1)
        for col in self.db.list_collections():
            self._collection_combo.addItem(
                f"{col['name']} ({col['card_count']})", col['id'],
            )
        idx = self._collection_combo.findData(current)
        if idx >= 0:
            self._collection_combo.setCurrentIndex(idx)
        self._collection_combo.blockSignals(False)

    def _refresh_tag_filter(self) -> None:
        # Drop any existing selected-tag chips (everything after the fixed
        # header: label, button, clear button).
        while self._filter_bar_layout.count() > self._filter_header_count:
            item = self._filter_bar_layout.takeAt(self._filter_header_count)
            if item and item.widget():
                item.widget().deleteLater()
        for tag in sorted(self._selected_filter_tags):
            chip = TagChip(tag, removable=True)
            chip.removed.connect(self._remove_selected_tag)
            self._filter_bar_layout.addWidget(chip)
        self._clear_tags_btn.setEnabled(bool(self._selected_filter_tags))

    def _open_tag_filter_dialog(self) -> None:
        from src.ui.widgets.tag_filter_dialog import TagFilterDialog

        dlg = TagFilterDialog(self.db.get_all_tags(), self._selected_filter_tags, self)
        # Read the selection BEFORE scheduling deletion: WA_DeleteOnClose +
        # exec() destroys the C++ widget as soon as the dialog closes, so
        # any post-exec getter would hit a wrapped deleted object.
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        selected = set(dlg.selected_tags())
        dlg.deleteLater()
        if accepted:
            self._selected_filter_tags = selected
            self._refresh_tag_filter()
            self._apply_filters()

    def _remove_selected_tag(self, tag: str) -> None:
        self._selected_filter_tags.discard(tag)
        self._refresh_tag_filter()
        self._apply_filters()

    def _clear_tag_filter(self) -> None:
        self._selected_filter_tags.clear()
        self._refresh_tag_filter()
        self._apply_filters()

    def _apply_filters(self) -> None:
        query = self._search_edit.text().strip()
        tags = list(self._selected_filter_tags) if self._selected_filter_tags else None
        favorites_only = self._favorites_only_cb.isChecked()
        collection_id = self._collection_combo.currentData()
        entries = self.db.search(
            query=query, tags=tags, sort_by=self._sort_by,
            favorites_only=favorites_only,
            collection_id=collection_id,
        )
        self._rebuild_grid(entries)

    def _on_manage_collections(self) -> None:
        from src.ui.widgets.collections_dialog import CollectionsManagerDialog

        dlg = CollectionsManagerDialog(self.db, self)
        changed = exec_dialog_with(dlg, lambda d: d.changed)
        if changed:
            self._refresh_collection_combo()
            # Re-apply unconditionally: deleting the collection currently being
            # filtered by leaves currentData() as None (the combo falls back to
            # "All Cards"), so the old guard skipped the re-filter and the grid
            # kept showing the deleted collection's results.
            self._apply_filters()

    def _on_assign_collections(self) -> None:
        from src.ui.widgets.collections_dialog import CollectionAssignDialog

        if self._selected_id is None:
            return
        entry = self.db.get_by_id(self._selected_id)
        name = entry.get('name', '') if entry else ''
        dlg = CollectionAssignDialog(self.db, self._selected_id, name, self)
        chosen = exec_dialog_with(
            dlg,
            lambda d: d.selected_collection_ids()
            if d.result() == QDialog.DialogCode.Accepted else None,
        )
        if chosen is not None:
            self.db.set_card_collections(self._selected_id, chosen)
            self._refresh_collection_combo()
            self._apply_filters()
            self.status_message.emit('Collections updated', 2000)

    def _on_rating_changed(self, value: int) -> None:
        if self._selected_id is None:
            return
        try:
            self.db.set_rating(self._selected_id, value)
        except Exception as e:
            # An exception escaping a slot aborts the process; a locked DB is
            # an ordinary, recoverable condition.
            logger.exception("Failed to set rating for card %s", self._selected_id)
            QMessageBox.critical(self, 'Error', f"Failed to save rating: {e}")
            self._update_detail()
            return
        msg = f'Rated {value}/5 stars' if value else 'Rating cleared'
        self.status_message.emit(msg, 2000)

    def _clear_grid(self) -> None:
        for thumb in self._thumbnails:
            thumb.hide()
            thumb.setParent(None)
            thumb.deleteLater()
        self._thumbnails.clear()
        # Cancel queued thumbnail loads. The pool queue is global and
        # unbounded, so without this every refresh (import, sync, filter
        # change, bulk favourite) piled up a full set of decodes whose results
        # were then thrown away with the widgets.
        cancel_pending_image_loads(THUMBNAIL_LOADER_OWNER)
        while self._grid_layout.count():
            self._grid_layout.takeAt(0)

    def _rebuild_grid(self, entries: list[dict]) -> None:
        container = self._grid_layout.parentWidget()
        # Preserve the scroll position across rebuilds: clearing the grid
        # collapses the content and clamps the scrollbar to 0, which would
        # lose the user's place on every import/filter change.
        scrollbar = self._grid_scroll.verticalScrollBar()
        scroll_pos = scrollbar.value()
        if container:
            container.setUpdatesEnabled(False)
        self._clear_grid()
        for entry in entries:
            self._add_thumbnail(entry)
        self._all_ids = [e['id'] for e in entries]
        # Drop any selection that no longer exists after the filter/sort.
        self._selection.prune(set(self._all_ids))
        self._update_selection_ui()
        self._grid_layout.activate()
        if container:
            container.setUpdatesEnabled(True)
            container.updateGeometry()
            container.update()

        def _restore() -> None:
            scrollbar.setValue(min(scroll_pos, scrollbar.maximum()))

        if scroll_pos > 0:
            # After layout settles (the content height may not be final yet).
            QTimer.singleShot(0, _restore)

    def _add_thumbnail(self, entry: dict) -> None:
        thumb = CardThumbnail(
            char_id=entry['id'],
            name=entry['name'],
            thumb_path=entry.get('thumbnail_path', ''),
            is_favorite=bool(entry.get('is_favorite')),
            token_count=entry.get('token_count', 0),
        )
        if self._thumb_img_size != 152:
            thumb.set_thumb_size(self._thumb_img_size)
        thumb.selection_requested.connect(self._on_selection_requested)
        thumb.double_clicked.connect(self._show_full_image)
        self._thumbnails.append(thumb)
        self._grid_layout.addWidget(thumb)

    def _refresh_single_thumbnail(self, char_id: int) -> None:
        """Update a single thumbnail in-place from the DB without rebuilding
        the whole grid. Preserves scroll position."""
        entry = self.db.get_by_id(char_id)
        if not entry:
            return
        self._card_cache.pop(char_id, None)
        for thumb in self._thumbnails:
            if thumb.char_id == char_id:
                thumb.update_from_entry(
                    name=entry['name'],
                    thumb_path=entry.get('thumbnail_path', ''),
                    is_favorite=bool(entry.get('is_favorite')),
                    token_count=entry.get('token_count', 0),
                )
                break
        # If this card is currently selected, refresh the detail pane too.
        if self._selected_id == char_id:
            self._on_card_clicked(char_id)

    def _remove_thumbnail_in_place(self, char_id: int) -> None:
        """Remove a single thumbnail from the grid without a full rebuild."""
        self._card_cache.pop(char_id, None)
        for i, thumb in enumerate(self._thumbnails):
            if thumb.char_id == char_id:
                thumb.setParent(None)
                thumb.deleteLater()
                self._thumbnails.pop(i)
                break
        if char_id in self._all_ids:
            self._all_ids.remove(char_id)
        self._selection.prune(set(self._all_ids))
        self._grid_layout.activate()

    def _on_card_clicked(self, char_id: int) -> None:
        for t in self._thumbnails:
            t.set_selected(t.char_id == char_id)
        self._selected_id = char_id
        self.card_selected.emit(char_id)

        entry = self.db.get_by_id(char_id)
        if not entry:
            return

        self._detail_name.setText(entry['name'])

        meta_parts = []
        if entry.get('creator'):
            meta_parts.append(f"Creator: {entry['creator']}")
        if entry.get('spec_version'):
            meta_parts.append(f"Spec: v{entry['spec_version']}")
        meta_parts.append(f"Tokens: {entry.get('token_count', 0):,}")
        self._detail_meta.setText('  |  '.join(meta_parts))

        tags = []
        if entry.get('tags'):
            try:
                tags = json.loads(entry['tags'])
            except (json.JSONDecodeError, TypeError):
                pass
        self._detail_tags.set_tags(tags)

        summary = entry.get('creator_notes', '')
        if summary and summary.strip():
            self._detail_summary.setText(summary.strip())
            self._summary_scroll.setVisible(True)
        else:
            self._detail_summary.setText('')
            self._summary_scroll.setVisible(False)

        source = entry.get('source_path', '')
        card = self._card_cache.get(char_id)
        if card is None and source and Path(source).exists():
            raw = read_card_data(source)
            if raw:
                card = CharacterCard.from_spec_dict(raw, source)
                self._card_cache[char_id] = card

        if card:
            text_parts = []
            if card.description:
                text_parts.append(f"=== Description ===\n{card.description}")
            if card.personality:
                text_parts.append(f"\n=== Personality ===\n{card.personality}")
            if card.scenario:
                text_parts.append(f"\n=== Scenario ===\n{card.scenario}")
            if card.first_mes:
                preview = card.first_mes[:500]
                if len(card.first_mes) > 500:
                    preview += '...'
                text_parts.append(f"\n=== First Message ===\n{preview}")
            if card.creator_notes:
                text_parts.append(f"\n=== Creator Notes ===\n{card.creator_notes[:300]}")
            self._detail_text.setPlainText('\n'.join(text_parts))
        else:
            desc = entry.get('description_preview', '')
            self._detail_text.setPlainText(desc if desc else 'No details available.')

        self._edit_btn.setEnabled(True)
        self._delete_btn.setEnabled(True)
        self._export_png_btn.setEnabled(True)
        self._export_json_btn.setEnabled(True)
        self._open_folder_btn.setEnabled(True)
        self._fav_btn.setEnabled(True)
        self._collections_btn.setEnabled(True)
        self._fav_btn.setChecked(bool(entry.get('is_favorite')))
        self._detail_rating.setEnabled(True)
        try:
            rating_value = int(entry.get('rating') or 0)
        except (TypeError, ValueError):
            rating_value = 0
        self._detail_rating.set_rating(rating_value)

    # ---- Multi-select ----

    def _on_selection_requested(self, char_id: int, ctrl: bool, shift: bool) -> None:
        if ctrl:
            self._selection.toggle(char_id)
        elif shift:
            self._selection.extend_range(char_id, self._all_ids)
        else:
            self._selection.select_single(char_id)
        self._update_selection_ui()

    def _update_selection_ui(self) -> None:
        selected = self._selection.selected_ids
        self._refresh_selection_highlights()
        self.selection_changed.emit(selected)
        count = len(selected)
        if count == 0:
            self._selected_id = None
            self._set_bulk_mode(False)
            self._clear_detail_pane()
        elif count == 1:
            self._selected_id = selected[0]
            self._set_bulk_mode(False)
            self._on_card_clicked(selected[0])
        else:
            self._selected_id = None
            self._set_bulk_mode(True)
            self._detail_name.setText(f"{count} cards selected")
            self._detail_meta.setText('Use bulk actions below.')
            self._detail_tags.set_tags([])
            self._detail_summary.setText('')
            self._summary_scroll.setVisible(False)
            self._detail_text.clear()
            self._detail_rating.set_rating(0)
            self.status_message.emit(f"{count} cards selected", 3000)

    def _refresh_selection_highlights(self) -> None:
        selected = set(self._selection.selected_ids)
        for thumb in self._thumbnails:
            thumb.set_selected(thumb.char_id in selected)

    def _set_bulk_mode(self, active: bool) -> None:
        for btn in self._single_btns:
            btn.setVisible(not active)
        for btn in self._bulk_btns:
            btn.setVisible(active)

    def _clear_detail_pane(self) -> None:
        self._detail_name.setText('Select a character')
        self._detail_meta.setText('')
        self._detail_tags.set_tags([])
        self._detail_summary.setText('')
        self._summary_scroll.setVisible(False)
        self._detail_text.clear()
        for btn in self._single_btns:
            btn.setEnabled(False)
        self._fav_btn.setChecked(False)
        self._detail_rating.set_rating(0)

    def get_selected_ids(self) -> list[int]:
        """Return the current multi-selection (for menu/shortcut delegation)."""
        return self._selection.selected_ids

    def refresh_font_size(self) -> None:
        """Recompute baked font-dependent heights after a theme change."""
        size = load_font_size()
        self._filter_bar_scroll.setMaximumHeight(max(106, size * 9))
        filter_container = self._filter_bar_scroll.parentWidget()
        if filter_container is not None:
            filter_container.setMaximumHeight(max(160, size * 9 + 40))

    def invalidate_card_cache(self) -> None:
        """Drop the per-card CharacterCard cache.

        Must be called after external processes rewrite card files on disk
        (SillyTavern pull/sync) so the detail pane doesn't serve stale
        pre-sync content until restart.
        """
        self._card_cache.clear()

    def get_scroll_position(self) -> int:
        """Return the vertical scroll position of the grid."""
        return self._grid_scroll.verticalScrollBar().value()

    def set_scroll_position(self, position: int) -> None:
        """Restore the vertical scroll position of the grid."""
        self._grid_scroll.verticalScrollBar().setValue(position)

    def set_selected_ids(self, ids: list[int]) -> None:
        """Restore a previously saved selection. Only IDs that exist in the current grid are selected."""
        valid = set(self._all_ids)
        self._selection.set_selection([i for i in ids if i in valid])
        self._update_selection_ui()

    def clear_selection(self) -> None:
        """Clear the multi-selection and refresh the UI."""
        self._selection.clear()
        self._update_selection_ui()

    def select_card(self, char_id: int) -> None:
        """Select a single card in the grid (for cross-tab sync from the Edit tab).

        The thumbnail is scrolled into view via a deferred singleShot, which
        captures both ``parent`` and ``thumb`` by reference. A grid rebuild
        between the scheduling and the callback (``deleteLater`` pending) would
        therefore make the lambda call into a destroyed widget, so the values
        are bound now and the callback verifies the widget is still alive.
        """
        if char_id not in self._all_ids:
            return
        self._selection.clear()
        self._selection.select_single(char_id)
        self._update_selection_ui()
        for thumb in self._thumbnails:
            if thumb.char_id != char_id:
                continue
            parent = thumb.parent()
            while parent and not isinstance(parent, QScrollArea):
                parent = parent.parent()
            if not isinstance(parent, QScrollArea):
                break
            self._ensure_visible_later(parent, thumb)
            break

    @staticmethod
    def _ensure_visible_later(parent: QScrollArea, thumb) -> None:
        """Scroll *thumb* into view on the next event-loop turn, if still alive."""
        from src.ui.widgets.chat_image_loader import _widget_alive

        def _reveal() -> None:
            if not _widget_alive(thumb) or not _widget_alive(parent):
                return
            parent.ensureWidgetVisible(thumb)

        QTimer.singleShot(0, _reveal)

    def _on_bulk_delete(self) -> None:
        selected = self._selection.selected_ids
        if not selected:
            return
        reply = QMessageBox.question(
            self, 'Bulk Delete',
            f"Delete {len(selected)} selected card(s)?\n\n"
            "This removes them from the library and permanently deletes "
            "their card files from disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for char_id in selected:
            try:
                self.db.remove_card(char_id, delete_files=True)
                self._card_cache.pop(char_id, None)
            except Exception:
                logger.exception("Bulk delete failed for card %s", char_id)
        # Remove all deleted thumbnails in-place
        for char_id in list(selected):
            for i, thumb in enumerate(self._thumbnails):
                if thumb.char_id == char_id:
                    thumb.setParent(None)
                    thumb.deleteLater()
                    self._thumbnails.pop(i)
                    break
            if char_id in self._all_ids:
                self._all_ids.remove(char_id)
        self._selection.prune(set(self._all_ids))
        self._grid_layout.activate()
        self.status_message.emit(f"Deleted {len(selected)} card(s).", 5000)
        self._selected_id = None
        self._update_selection_ui()
        self._clear_detail_pane()
        self.library_changed.emit()

    def _on_bulk_favorite(self, favorite: bool) -> None:
        selected = self._selection.selected_ids
        if not selected:
            return
        selected_set = set(selected)
        for char_id in selected:
            try:
                self.db.set_favorite(char_id, favorite)
            except Exception:
                logger.exception("Bulk favorite failed for card %s", char_id)
        for thumb in self._thumbnails:
            if thumb.char_id in selected_set:
                entry = self.db.get_by_id(thumb.char_id)
                if entry:
                    thumb.update_from_entry(
                        name=entry['name'],
                        thumb_path=entry.get('thumbnail_path', ''),
                        is_favorite=bool(entry.get('is_favorite')),
                        token_count=entry.get('token_count', 0),
                    )
        word = 'Favorited' if favorite else 'Unfavorited'
        self.status_message.emit(f"{word} {len(selected)} card(s).", 4000)
        self.library_changed.emit()

    def _on_bulk_export_png(self) -> None:
        selected = self._selection.selected_ids
        if not selected:
            return
        dest_dir = QFileDialog.getExistingDirectory(self, 'Export Selected PNGs to Folder')
        if not dest_dir:
            return
        from src import vault
        from src.database import sanitize_filename
        exported = 0
        errors: list[str] = []
        for char_id in selected:
            entry = self.db.get_by_id(char_id)
            if not entry:
                continue
            source = entry.get('source_path', '')
            if not source or not Path(source).exists():
                errors.append(f"{entry['name']}: source missing")
                continue
            dest_name = f"{sanitize_filename(entry['name'])}_{char_id}{Path(source).suffix}"
            dest = Path(dest_dir) / dest_name
            try:
                vault.copy_out(source, dest)
                exported += 1
            except OSError as e:
                errors.append(f"{entry['name']}: {e}")
        msg = f"Exported {exported} PNG(s)."
        if errors:
            QMessageBox.warning(self, 'Export Results', msg + "\n\nErrors:\n" + '\n'.join(errors))
        self.status_message.emit(msg, 5000)

    def _on_bulk_export_json(self) -> None:
        selected = self._selection.selected_ids
        if not selected:
            return
        dest_dir = QFileDialog.getExistingDirectory(self, 'Export Selected JSON to Folder')
        if not dest_dir:
            return
        from src.database import sanitize_filename
        exported = 0
        errors: list[str] = []
        for char_id in selected:
            entry = self.db.get_by_id(char_id)
            if not entry:
                continue
            source = entry.get('source_path', '')
            if not source or not Path(source).exists():
                errors.append(f"{entry['name']}: source missing")
                continue
            raw = read_card_data(source)
            if raw is None:
                errors.append(f"{entry['name']}: no card data")
                continue
            dest_name = f"{sanitize_filename(entry['name'])}_{char_id}.json"
            dest = Path(dest_dir) / dest_name
            try:
                with open(dest, 'w', encoding='utf-8') as f:
                    json.dump(raw, f, ensure_ascii=False, indent=2)
                exported += 1
            except OSError as e:
                errors.append(f"{entry['name']}: {e}")
        msg = f"Exported {exported} JSON file(s)."
        if errors:
            QMessageBox.warning(self, 'Export Results', msg + "\n\nErrors:\n" + '\n'.join(errors))
        self.status_message.emit(msg, 5000)

    def _on_search(self, text: str) -> None:
        # Debounce: restart the timer on each keystroke; the filter only runs
        # after the user stops typing for 300ms.
        self._search_timer.start()

    def _on_sort_changed(self, label: str) -> None:
        self._sort_by = self._SORT_LABELS.get(label, 'name')
        self._apply_filters()
        self.sort_label_changed.emit(label)

    def _import_card(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Import Character Cards', '',
            'Card Files (*.png *.json);;PNG Files (*.png);;JSON Files (*.json);;All Files (*)',
        )
        if not paths:
            return
        self._import_paths(paths)

    def _import_paths(self, paths: list[str]) -> None:
        """Import a list of file paths into the library asynchronously.

        Shared by the file-dialog import and the drag-and-drop handler.  The
        per-file work (parse, duplicate check, add) runs on a background
        :class:`ImportWorker` thread so the UI stays responsive for large
        batches; a modal :class:`QProgressDialog` reports progress and can be
        cancelled mid-import.
        """
        importable = _filter_importable_paths(paths)
        if not importable:
            return
        if self._worker_is_alive(self._import_worker):
            self.status_message.emit('An import is already in progress.', 3000)
            return
        self._import_force_pending = False
        self._import_cancelled = False
        self._start_import_worker(importable, force_duplicates=False)

    def _start_import_worker(self, paths: list[str], force_duplicates: bool) -> None:
        """Create, wire and start an :class:`ImportWorker` for *paths*."""
        from src.ui.widgets.import_worker import ImportWorker

        self._import_cancelled = False
        self._import_worker = ImportWorker(
            self.db, paths, force_duplicates=force_duplicates, parent=self,
        )
        self._progress_dialog = QProgressDialog('Importing cards...', 'Cancel', 0, len(paths), self)
        self._progress_dialog.setWindowTitle('Import')
        self._progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress_dialog.setMinimumDuration(300)
        # Without these, reaching max auto-resets/hides the dialog before the
        # finished handler closes it (flicker + briefly clickable Cancel).
        self._progress_dialog.setAutoClose(False)
        self._progress_dialog.setAutoReset(False)
        self._progress_dialog.canceled.connect(self._on_import_cancelled)
        self._import_worker.progress.connect(self._on_import_progress)
        self._import_worker.result.connect(self._on_import_result)
        self._import_worker.completed.connect(self._on_import_finished)
        # Cleanup on the built-in signal so cancelled runs are still deleted.
        self._import_worker.finished.connect(self._import_worker.deleteLater)
        self._import_worker.start()

    def _on_import_cancelled(self) -> None:
        """Handle the progress dialog's Cancel button: flag + cancel the worker."""
        self._import_cancelled = True
        if self._import_worker is not None:
            self._import_worker.cancel()

    def _on_import_progress(self, current: int, total: int, name: str) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.setMaximum(total)
            self._progress_dialog.setValue(current)
            self._progress_dialog.setLabelText(f"Importing {current}/{total}: {name}")

    def _on_import_result(self, path: str, char_id: object, error: str) -> None:
        # Per-file results are accumulated in the ImportSummary emitted with
        # ``completed``; this handler is available for live grid updates.
        if char_id is not None:
            logger.debug("Imported %s -> id %s", path, char_id)
        elif error:
            logger.debug("Import skipped/failed for %s: %s", path, error)

    def _on_import_finished(self, summary) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
            self._progress_dialog = None
        msg = summary.message()
        self.status_message.emit(msg, 5000)

        # If duplicates were skipped and the user hasn't already been asked,
        # offer to force-import them in a second pass.  Skip the prompt when
        # the import was cancelled — the user already chose to stop.
        if (
            summary.skipped > 0
            and not self._import_force_pending
            and not self._import_cancelled
            and summary.skipped_paths
        ):
            reply = QMessageBox.question(
                self, 'Duplicates Skipped',
                f"{summary.skipped} duplicate card(s) were skipped.\n"
                f"Import them anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._import_force_pending = True
                self._start_import_worker(
                    summary.skipped_paths, force_duplicates=True,
                )
                return

        self._import_force_pending = False
        self.load_cards()
        if summary.errors:
            QMessageBox.warning(
                self, 'Import Errors',
                msg + "\n\nErrors:\n" + '\n'.join(summary.errors[:20]),
            )
        # Successful imports are already reported via the status bar —
        # a modal popup would just interrupt the workflow.

    @staticmethod
    def _worker_is_alive(worker) -> bool:
        """True when *worker* exists and its C++ object hasn't been deleted.

        Workers are deleteLater()d via their finished signal, so the Python
        wrapper can outlive the Qt object; calling isRunning() on a deleted
        wrapper raises RuntimeError.
        """
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def cleanup_workers(self, timeout_ms: int = 3000) -> bool:
        """Cancel and wait for a running import worker.

        Returns True if the worker finished within the timeout, False if it
        is still running (the caller may force-close anyway).
        """
        if self._worker_is_alive(self._import_worker):
            try:
                self._import_worker.cancel()
                if not self._import_worker.wait(timeout_ms):
                    return False
            except RuntimeError:
                pass
        self._import_worker = None
        return True

    # ---- Drag-and-drop import ----

    def _droppable_paths(self, mime) -> list[str]:
        """Extract importable local file paths from a drop mime data object."""
        if not mime.hasUrls():
            return []
        paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
        return _filter_importable_paths(paths)

    def dragEnterEvent(self, event) -> None:
        if self._droppable_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._droppable_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = self._droppable_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self._import_paths(paths)
        else:
            event.ignore()

    def _on_edit(self) -> None:
        if self._selected_id is not None:
            self.edit_requested.emit(self._selected_id)

    def _on_delete(self) -> None:
        if self._selected_id is None:
            return
        entry = self.db.get_by_id(self._selected_id)
        if not entry:
            return
        reply = QMessageBox.question(
            self, 'Delete Character',
            f"Delete '{entry['name']}'?\n\n"
            "This removes the card from the library and permanently deletes "
            "its card file from disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            deleted_id = self._selected_id
            try:
                self.db.remove_card(deleted_id, delete_files=True)
            except Exception as e:
                # An exception escaping a slot aborts the process; a locked
                # file or a busy database must show an error instead.
                logger.exception("Delete of card %s failed", deleted_id)
                QMessageBox.critical(self, 'Delete Character', f'Could not delete the card: {e}')
                return
            self._remove_thumbnail_in_place(deleted_id)
            self._selected_id = None
            self._update_selection_ui()
            self._clear_detail_pane()
            self.library_changed.emit()

    def _on_export_png(self) -> None:
        if self._selected_id is None:
            return
        entry = self.db.get_by_id(self._selected_id)
        if not entry:
            return
        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            QMessageBox.warning(self, 'Export', 'Source file not found.')
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, 'Export Character Card (PNG)',
            f"{sanitize_filename(entry['name'])}.png",
            'PNG Files (*.png)',
        )
        if not dest:
            return
        try:
            from src import vault
            vault.copy_out(source, dest)
            self.status_message.emit(f"Exported PNG to {Path(dest).name}", 4000)
        except OSError as e:
            logger.exception("PNG export failed")
            QMessageBox.critical(self, 'Export', f"Failed to export PNG: {e}")

    def _on_export_json(self) -> None:
        if self._selected_id is None:
            return
        entry = self.db.get_by_id(self._selected_id)
        if not entry:
            return
        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            QMessageBox.warning(self, 'Export', 'Source file not found.')
            return
        raw = read_card_data(source)
        if raw is None:
            QMessageBox.warning(self, 'Export', 'No character data found in card.')
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, 'Export Character Card (JSON)',
            f"{sanitize_filename(entry['name'])}.json",
            'JSON Files (*.json)',
        )
        if not dest:
            return
        try:
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            self.status_message.emit(f"Exported JSON to {Path(dest).name}", 4000)
        except OSError as e:
            logger.exception("JSON export failed")
            QMessageBox.critical(self, 'Export', f"Failed to export JSON: {e}")

    def _on_open_folder(self) -> None:
        if self._selected_id is None:
            return
        entry = self.db.get_by_id(self._selected_id)
        if not entry:
            return
        source = entry.get('source_path', '')
        from src.path_utils import open_containing_folder
        if not open_containing_folder(source):
            QMessageBox.warning(self, 'Open Folder', 'Source file not found.')
            logger.warning("Could not open folder for card %s (source missing)", self._selected_id)

    def _on_toggle_favorite(self) -> None:
        if self._selected_id is None:
            return
        try:
            new_state = self.db.toggle_favorite(self._selected_id)
            self._fav_btn.setChecked(new_state)
            # Update just the affected thumbnail instead of rebuilding the grid.
            self._refresh_single_thumbnail(self._selected_id)
            self.library_changed.emit()
        except Exception as e:
            QMessageBox.critical(self, 'Error', f"Failed to toggle favorite: {e}")
            logger.exception("Favorite toggle error")

    def _show_full_image(self, char_id: int) -> None:
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

        # Strong ref: the dialog is parented to this tab (so Qt owns the C++
        # side), but without a Python reference the wrapper can be collected,
        # which would defeat the reuse check and stack duplicate viewers.
        dlg = ImageViewer(source, f"Full Image - {entry['name']}", self)
        dlg.show()
        self._full_image_dlg = dlg

    def get_selected_id(self) -> int | None:
        return self._selected_id

    # ---- Public API for menu / shortcut delegation ----

    def import_card(self) -> None:
        """Open the import file dialog (menu entry)."""
        self._import_card()

    def export_png(self) -> None:
        """Export the selected card as PNG (menu entry)."""
        self._on_export_png()

    def focus_search(self) -> None:
        """Focus the search box and select its contents."""
        self._search_edit.setFocus()
        self._search_edit.selectAll()

    def refresh(self) -> None:
        """Reload the card grid."""
        self.load_cards()

    def find_duplicates(self) -> None:
        """Open the duplicate scanner dialog (toolbar / menu entry)."""
        from src.ui.widgets.duplicate_scanner import DuplicateScannerDialog
        dlg = DuplicateScannerDialog(self.db, self)
        exec_dialog(dlg)
        # Card data may have changed; refresh the grid.
        self.load_cards()

    def show_statistics(self) -> None:
        """Open the statistics dashboard dialog (toolbar / menu entry)."""
        from src.ui.widgets.stats_dialog import StatsDialog
        dlg = StatsDialog(self.db, self)
        exec_dialog(dlg)

    def _on_settings_requested(self) -> None:
        self.settings_requested.emit()

    def toggle_favorites_filter(self) -> None:
        """Toggle the Favorites Only checkbox.

        The checkbox's ``toggled`` signal already triggers ``_apply_filters``,
        so no explicit second call here (it would rebuild the grid twice).
        """
        self._favorites_only_cb.toggle()

    def set_sort_by_label(self, label: str) -> None:
        """Set the sort dropdown to *label* (triggers re-filter)."""
        self._sort_combo.setCurrentText(label)

    def zoom_thumbnails(self, delta: int) -> None:
        """Adjust thumbnail size by *delta* pixels, clamped to 80–280.

        The actual reload is debounced via ``_zoom_timer`` so a burst of zoom
        shortcuts only triggers one round of image reloads (at the final size).
        """
        from src.ui.widgets.image_viewer import clamp_thumb_size
        new_size = clamp_thumb_size(self._thumb_img_size + delta)
        if new_size != self._thumb_img_size:
            self._thumb_img_size = new_size
            self._zoom_timer.start()

    def _apply_zoom(self) -> None:
        """Apply the pending thumbnail size to all current thumbnails."""
        for thumb in self._thumbnails:
            thumb.set_thumb_size(self._thumb_img_size)
        self._grid_layout.activate()

    def delete_selected(self) -> None:
        """Delete the currently selected card (menu entry)."""
        self._on_delete()

    def duplicate_card(self) -> None:
        """Duplicate the selected card into the library with a '(copy)' suffix."""
        if self._selected_id is None:
            return
        entry = self.db.get_by_id(self._selected_id)
        if not entry:
            return
        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            QMessageBox.warning(self, 'Duplicate', 'Source file not found.')
            return

        import uuid
        from src.card_models import build_duplicate_card
        from src.database import _get_library_dir, sanitize_filename
        from src.token_counter import count_card_tokens

        raw = read_card_data(source)
        if not raw:
            QMessageBox.warning(self, 'Duplicate', 'Could not read card data.')
            return
        base = CharacterCard.from_spec_dict(raw, source)
        clone = build_duplicate_card(base)
        clone.token_count = count_card_tokens(clone)

        lib_dir = _get_library_dir()
        ext = Path(source).suffix
        # Sanitize: card names may contain characters illegal on Windows,
        # and the copy must stay inside the try (slot exceptions abort the
        # process under PyQt6).
        dest_path = lib_dir / f"{sanitize_filename(clone.name)[:60]}_{uuid.uuid4().hex[:8]}{ext}"
        try:
            from src import vault
            vault.import_external(source, str(dest_path))
            clone.source_path = str(dest_path)
            new_id = self.db.add_card(clone)
            self.status_message.emit(f"Duplicated as '{clone.name}'", 4000)
            self.load_cards()
            self._on_card_clicked(new_id)
        except Exception as e:
            QMessageBox.critical(self, 'Error', f"Failed to duplicate: {e}")
            logger.exception("Duplicate failed")

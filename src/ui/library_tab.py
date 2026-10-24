from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
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
# ``_escape_like`` is the LIKE-metacharacter escaper the database's own
# search uses; the bounded search below must escape identically.
from src.database import (
    SORT_LABELS,
    LibraryDatabase,
    _escape_like,
    collection_filter_clause,
    sanitize_filename,
)
from src.settings_manager import load_font_size
from src.ui.dialog_helper import exec_dialog, exec_dialog_with
from src.ui.widgets.async_image import (
    THUMBNAIL_LOADER_OWNER,
    cancel_pending_image_loads,
)
from src.ui.widgets.card_thumbnail import CardThumbnail
from src.ui.widgets.card_context_menu import show_card_context_menu
from src.ui.widgets.flow_layout import FlowLayout
from src.ui.widgets.multi_select_dropdown import MultiSelectDropdown
from src.ui.widgets.rating_widget import RatingWidget
from src.ui.widgets.tag_widget import TagChip, TagWidget
from src import safe_json

logger = logging.getLogger(__name__)

# File types that can be drag-and-dropped / imported.  Lower-cased suffixes
# (no dot) so the check is suffix-agnostic.
_IMPORTABLE_EXTENSIONS: tuple[str, ...] = ('.png', '.json')

# How many cards the grid builds at once.  Every card costs one thumbnail
# widget (two filesystem stats plus a font-metrics measurement), so an
# unbounded rebuild froze the UI for seconds on a library with thousands of
# cards.  The rest is reachable through the "Load more" button.
GRID_PAGE_SIZE = 100

#: The page size that means "every matching card, no paging at all".
ALL_CARDS = 0

#: The query limit that stands for "every matching card".  Far above any
#: real library, but a real number so the SQL stays a plain LIMIT.
NO_LIMIT = 1_000_000

#: The choices offered by the "cards per page" dropdown.
PAGE_SIZE_CHOICES: tuple[tuple[str, int], ...] = (
    ('25', 25),
    ('50', 50),
    ('100', 100),
    ('200', 200),
    ('500', 500),
    ('1000', 1000),
    ('All', ALL_CARDS),
)

#: The default edge length (px) of a Library grid thumbnail.  "Reset Zoom"
#: (Ctrl+0) restores it.
DEFAULT_THUMB_SIZE = 152


def _configured_page_size() -> int:
    """The number of cards the grid loads per page (0 = all of them).

    The portable config can raise or lower it (``[library] page_size = 250``);
    the value is clamped so a typo cannot make the grid unusable.  Zero is
    the one value that is not clamped: it is the "show everything" choice.
    """
    try:
        from src.portable_config import get
        value = int(get('library.page_size', GRID_PAGE_SIZE))
    except Exception:
        return GRID_PAGE_SIZE
    if value == ALL_CARDS:
        return ALL_CARDS
    return max(10, min(2000, value))


def _filter_importable_paths(paths: list[str]) -> list[str]:
    """Return only the paths whose suffix is an importable file type.

    Pure function so the filter logic can be unit-tested without Qt.
    Case-insensitive on the file extension.
    """
    return [p for p in paths if Path(p).suffix.lower() in _IMPORTABLE_EXTENSIONS]


def _build_import_summary(imported: int, skipped: int) -> str:
    """Build the short status-bar summary for an import operation.

    Pure function so the message formatting can be unit-testable without Qt.
    """
    msg = f"Imported {imported} card(s)."
    if skipped:
        msg += f" Skipped {skipped} duplicate(s)."
    return msg


def notify_card_deleted(card_id: int, name: str) -> None:
    """Announce a deleted card to the plugin/event bus.

    The event module is imported lazily - it is an optional cross-cutting
    module and must not be pulled in at start-up - and every error is
    swallowed: a failing user plugin must never break the deletion.

    Args:
        card_id: The database id of the deleted card.
        name: The name of the deleted card.
    """
    try:
        from src import app_events
        app_events.card_deleted(card_id, name)
    except Exception:
        logger.exception("Could not announce the deleted card")


def _search_page(
    db: LibraryDatabase,
    query: str = '',
    tags: list[str] | None = None,
    sort_by: str = 'name',
    favorites_only: bool = False,
    min_rating: int = 0,
    collection_id: int | Sequence[int] | None = None,
    limit: int = GRID_PAGE_SIZE,
    offset: int = 0,
    only_id: int | None = None,
    full_text: bool = False,
    fuzzy: bool = False,
) -> tuple[list[dict], int]:
    """Return one page of matching cards plus the total number of matches.

    ``LibraryDatabase.search`` has no LIMIT, so every search / filter /
    sort pulled *all* matching rows out of SQLite (``SELECT *``) and then
    built one thumbnail widget per row - the UI froze for seconds on a
    library with a few thousand cards.  This mirrors the search semantics
    but bounds the number of rows that come back.

    *only_id* restricts the result to a single card (used to check whether
    a card passes the current filter without loading the whole page).

    *full_text* also matches the full card body (the description, the other
    prompt fields, the creator notes and the character book) instead of only
    the short preview.  *fuzzy* additionally keeps the cards whose name,
    creator or one of their tags contains every word of the query, in any
    order.  Both are one extra SQL clause - never a query per row.

    The WHERE clause must be kept in sync with ``LibraryDatabase.search``.
    """
    clauses: list[str] = []
    params: list = []
    if only_id is not None:
        clauses.append('id = ?')
        params.append(int(only_id))
    if favorites_only:
        clauses.append('is_favorite = 1')
    if min_rating and min_rating > 0:
        clauses.append('rating >= ?')
        params.append(int(min_rating))
    if collection_id is not None:
        col_clause, col_params = collection_filter_clause(collection_id)
        if col_clause != '1=1':
            clauses.append(col_clause)
            params.extend(col_params)
    # The text clause is built inside the connection block: the full-text
    # variant needs a column that only exists in a library created by a
    # newer build, so the schema has to be checked first.
    text_clauses: list[str] = []
    text_params: list = []
    if query:
        # Escape LIKE metacharacters so a query of "%" doesn't match
        # every row.
        q = f"%{_escape_like(query)}%"
        text_clauses = [
            "(name LIKE ? ESCAPE '\\' OR description_preview LIKE ? ESCAPE '\\' "
            "OR tags LIKE ? ESCAPE '\\' OR creator LIKE ? ESCAPE '\\' "
            "OR creator_notes LIKE ? ESCAPE '\\')"
        ]
        text_params = [q, q, q, q, q]
        if fuzzy:
            # Word-wise match: every word of the query has to appear in the
            # name, the creator or one of the tags.
            text_clauses.append(
                '(st_fuzzy_match(name, ?) = 1 '
                'OR st_fuzzy_match(creator, ?) = 1 '
                'OR st_fuzzy_tags(tags, ?) = 1)'
            )
            text_params.extend([query, query, query])
    for tag in tags or []:
        # Exact tag match (see LibraryDatabase.search for the details).
        clauses.append('st_has_tag(tags, ?) = 1')
        params.append(tag)
    order = db._sort_order(sort_by)
    with db._conn() as conn:
        conn.row_factory = sqlite3.Row
        if full_text and query:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(characters)')}
            if 'body_text' in columns:
                # The full body of the card, not just its preview.
                text_clauses.append("body_text LIKE ? ESCAPE '\\'")
                text_params.append(f"%{_escape_like(query)}%")
        if text_clauses:
            clauses.append('(' + ' OR '.join(text_clauses) + ')')
            params.extend(text_params)
        where = ' AND '.join(clauses) if clauses else '1=1'
        total = conn.execute(
            f'SELECT COUNT(*) FROM characters WHERE {where}', params,
        ).fetchone()[0]
        rows = conn.execute(
            f'SELECT * FROM characters WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?',
            [*params, int(limit), int(offset)],
        ).fetchall()
        return [dict(row) for row in rows], int(total)


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


def _detail_pane_initial_height(line_h: int) -> int:
    """Initial height of the detail pane below the card grid.

    200px plus one text line for each of the pane's three display sections
    (tags, summary, description), plus one more so the summary keeps its
    height now that the tags section above it also grew by a line.
    """
    return 200 + 4 * line_h


def _summary_max_height(pane_height: int, line_h: int) -> int:
    """Cap the summary at ~1/3 of the pane, never less than two text lines."""
    return max(48 + line_h, int(pane_height / 3))


def _filter_bar_height(rows_h: int, cap: int) -> int:
    """Height of the tag-filter bar: its wrapped rows, clamped.

    Never shorter than one row and never taller than *cap*, so the bar hugs
    its chips instead of reserving a fixed block of empty space when no
    tag is selected.
    """
    return min(max(32, int(rows_h)), max(32, int(cap)))


class _DetailContainer(QWidget):
    """Detail pane that caps the summary's height to ~1/3 of its own height."""

    def __init__(self, summary_scroll: QScrollArea, parent: QWidget | None = None):
        super().__init__(parent)
        self._summary_scroll = summary_scroll

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # One extra text line over the old floor so the summary gets the
        # same "one more line" as the tags and description sections.
        line_h = max(16, self.fontMetrics().lineSpacing())
        self._summary_scroll.setMaximumHeight(
            _summary_max_height(self.height(), line_h),
        )


class LibraryTab(QWidget):
    card_selected = pyqtSignal(int)
    edit_requested = pyqtSignal(int)
    library_changed = pyqtSignal()
    status_message = pyqtSignal(str, int)
    sort_label_changed = pyqtSignal(str)
    selection_changed = pyqtSignal(list)
    settings_requested = pyqtSignal()
    # Emitted when the user edits the search box; the shared sidebar mirrors it.
    search_changed = pyqtSignal(str)
    # The favorites filter is shared with the sidebar, so the two lists
    # always show the same cards.
    favorites_filter_changed = pyqtSignal(bool)
    # Emitted with (matched, library_total) whenever the filter result
    # changes, so the status bar can show how many cards the current filter
    # matches out of the whole library.
    filter_stats_changed = pyqtSignal(int, int)

    # Map dropdown display text -> DB sort key (shared with the sidebar's
    # sort combo; see SORT_LABELS / LibraryDatabase._SORT_WHITELIST).
    _SORT_LABELS: dict[str, str] = SORT_LABELS

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
        self._thumb_img_size: int = DEFAULT_THUMB_SIZE
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
        # Paging state for the card grid: only ``_load_limit`` cards are
        # built at once, out of ``_match_count`` matching cards.
        self._page_size = _configured_page_size()
        self._load_limit = self._page_limit()
        self._match_count = 0
        self._filter_args: dict = {}

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

        # Row 1: search + search modes + primary actions.
        toolbar = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText('Search characters...')
        self._search_edit.textChanged.connect(self._on_search)
        toolbar.addWidget(self._search_edit, 1)

        self._full_text_cb = QCheckBox('Full text')
        self._full_text_cb.setObjectName('filterChip')
        self._full_text_cb.setToolTip(
            'Also search the whole card: the description, the other prompt '
            'fields, the creator notes and the character book.'
        )
        self._full_text_cb.toggled.connect(self._apply_filters)
        toolbar.addWidget(self._full_text_cb)

        self._fuzzy_cb = QCheckBox('Fuzzy')
        self._fuzzy_cb.setObjectName('filterChip')
        self._fuzzy_cb.setToolTip(
            'Also match cards whose name, creator or tags contain every word '
            'of the search, in any order.'
        )
        self._fuzzy_cb.toggled.connect(self._apply_filters)
        toolbar.addWidget(self._fuzzy_cb)

        import_btn = QPushButton('Import')
        import_btn.setToolTip('Import a character card (PNG or JSON)')
        import_btn.clicked.connect(self._import_card)
        toolbar.addWidget(import_btn)

        dup_btn = QPushButton('Find Duplicates')
        dup_btn.clicked.connect(self.find_duplicates)
        toolbar.addWidget(dup_btn)

        refresh_btn = QPushButton('Refresh')
        refresh_btn.clicked.connect(self.load_cards)
        toolbar.addWidget(refresh_btn)

        settings_btn = QPushButton('Settings')
        settings_btn.clicked.connect(self._on_settings_requested)
        toolbar.addWidget(settings_btn)

        layout.addLayout(toolbar)

        # Row 2: sort + view filters (kept on its own row so nothing is
        # squashed when the window is narrow).
        filter_row = QHBoxLayout()
        sort_label = QLabel('Sort:')
        sort_label.setObjectName('filterChip')
        filter_row.addWidget(sort_label)
        self._sort_combo = QComboBox()
        for label in self._SORT_LABELS:
            self._sort_combo.addItem(label)
        self._sort_combo.currentTextChanged.connect(self._on_sort_changed)
        filter_row.addWidget(self._sort_combo)

        collection_label = QLabel('Collection:')
        collection_label.setObjectName('filterChip')
        filter_row.addWidget(collection_label)
        self._collection_filter = MultiSelectDropdown(empty_text='All Cards')
        self._collection_filter.setToolTip(
            'Filter by collection - check several to combine them'
        )
        self._collection_filter.selection_changed.connect(self._apply_filters)
        # The two dropdowns in this row look tidiest at the same width;
        # the combo's hint already covers its longest sort label.
        self._collection_filter.setMinimumWidth(
            self._sort_combo.sizeHint().width()
        )
        filter_row.addWidget(self._collection_filter)

        self._build_page_size_combo(filter_row)

        filter_row.addStretch()
        self._favorites_only_cb = QCheckBox('Favorites only')
        self._favorites_only_cb.setObjectName('filterChip')
        self._favorites_only_cb.toggled.connect(self._on_favorites_toggled)
        filter_row.addWidget(self._favorites_only_cb)
        layout.addLayout(filter_row)

        filter_container = QWidget()
        filter_layout = QVBoxLayout(filter_container)
        filter_layout.setContentsMargins(0, 4, 0, 4)
        filter_layout.setSpacing(2)

        # The filter bar is a single wrapping row: the label, the button that
        # opens the tag-selection popup, the clear button, and the currently
        # selected tags (shown as removable chips) all flow together.
        self._filter_bar_scroll = QScrollArea()
        self._filter_bar_scroll.setWidgetResizable(True)
        self._filter_bar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._filter_bar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._filter_bar_widget = QWidget()
        self._filter_bar_layout = FlowLayout(self._filter_bar_widget, margin=2, h_spacing=4, v_spacing=2, wrap_fudge=8)
        self._filter_bar_scroll.setWidget(self._filter_bar_widget)

        filter_label = QLabel('Filter by tags:')
        filter_label.setObjectName('filterChip')
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
        self._filter_container = filter_container
        layout.addWidget(filter_container)

        # Divider between the tag filter bar and the card grid, mirroring
        # the splitter handle line below the grid (both use $border_dim).
        grid_separator = QWidget()
        grid_separator.setObjectName('gridSeparator')
        grid_separator.setFixedHeight(2)
        layout.addWidget(grid_separator)

        # One line of text at the configured font size.  The detail-pane
        # sections grow with it so each one shows about one more line of
        # content at any window height.
        line_h = max(16, self.fontMetrics().lineSpacing())

        splitter = QSplitter(Qt.Orientation.Vertical)

        grid_container = QWidget()
        self._grid_layout = FlowLayout(grid_container, margin=8, h_spacing=8, v_spacing=8)
        self._grid_scroll = QScrollArea()
        self._grid_scroll.setWidgetResizable(True)
        self._grid_scroll.setWidget(grid_container)

        # "Showing X of Y" + "Load more" for libraries that do not fit on
        # a single page of thumbnails.
        self._grid_status_row = QWidget()
        status_layout = QHBoxLayout(self._grid_status_row)
        status_layout.setContentsMargins(2, 0, 2, 0)
        status_layout.setSpacing(6)
        self._grid_status = QLabel('')
        self._grid_status.setStyleSheet('color: #999;')
        status_layout.addWidget(self._grid_status)
        self._load_more_btn = QPushButton('Load More')
        self._load_more_btn.setToolTip('Show the next page of matching cards')
        self._load_more_btn.clicked.connect(self._load_more)
        status_layout.addWidget(self._load_more_btn)
        status_layout.addStretch()
        self._grid_status_row.setVisible(False)

        grid_panel = QWidget()
        grid_panel_layout = QVBoxLayout(grid_panel)
        grid_panel_layout.setContentsMargins(0, 0, 0, 0)
        grid_panel_layout.setSpacing(2)
        grid_panel_layout.addWidget(self._grid_scroll, 1)
        grid_panel_layout.addWidget(self._grid_status_row)
        splitter.addWidget(grid_panel)

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
        # One extra line over the old 60px: roughly one more row of tags,
        # scaled with the configured font size.
        tags_scroll.setMaximumHeight(60 + line_h)
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
        # One extra line over the old 220px cap so the description shows
        # roughly one more line of text.
        self._detail_text.setMaximumHeight(220 + line_h)
        detail_layout.addWidget(self._detail_text)

        btn_row = QHBoxLayout()
        self._fav_btn = QPushButton('Favorite')
        self._fav_btn.setCheckable(True)
        self._fav_btn.setEnabled(False)
        self._fav_btn.clicked.connect(self._on_toggle_favorite)
        btn_row.addWidget(self._fav_btn)

        self._add_to_collection_btn = QPushButton('Add to collection')
        self._add_to_collection_btn.setEnabled(False)
        self._add_to_collection_btn.setToolTip(
            "Add the selected card to a new or existing collection"
        )
        self._add_to_collection_btn.clicked.connect(self._on_assign_collections)
        btn_row.addWidget(self._add_to_collection_btn)

        self._collections_btn = QPushButton('Collections')
        self._collections_btn.setToolTip(
            "Create, rename, or delete collections"
        )
        self._collections_btn.clicked.connect(self._on_manage_collections)
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
            self._fav_btn, self._add_to_collection_btn, self._delete_btn,
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
        # The detail pane starts four text lines taller than before: one
        # extra line for each display section (tags, summary, description)
        # plus one more so the summary keeps its height now that the tags
        # section also grew by a line.  QSplitter keeps this offset at any
        # window height because free space is handed out by the stretch
        # factors above.
        splitter.setSizes([600, _detail_pane_initial_height(line_h)])
        layout.addWidget(splitter, 1)

        self._refresh_collection_combo()
        self._sync_filter_bar_height()
        self._sync_chip_heights()

    def load_cards(self) -> None:
        self._refresh_collection_combo()
        self._refresh_tag_filter()
        self._apply_filters()
        self.library_changed.emit()

    def _build_page_size_combo(self, row) -> None:
        """Add the "cards per page" dropdown to *row*.

        The dropdown decides how many cards are built before the "Load
        more" button appears under the grid.  The choice is stored in the
        portable config, so it survives a restart and travels with the
        configuration.
        """
        label = QLabel('Cards per page:')
        label.setObjectName('filterChip')
        row.addWidget(label)

        combo = QComboBox()
        combo.setToolTip(
            'How many cards are built at once; the rest is reachable '
            'through the "Load More" button below the grid.\n'
            '"All" builds every matching card in one go, which takes a '
            'while on a large library.'
        )
        for text, value in PAGE_SIZE_CHOICES:
            combo.addItem(text, value)
        if self._page_size and self._page_size not in [v for _, v in PAGE_SIZE_CHOICES]:
            # A hand-edited config may name a size that is not on the list.
            combo.addItem(str(self._page_size), self._page_size)
        index = combo.findData(self._page_size)
        if index < 0:
            index = combo.findData(GRID_PAGE_SIZE)
        # Set the selection before connecting, so the initial value does not
        # count as a user change.
        combo.setCurrentIndex(index)
        combo.currentIndexChanged.connect(self._on_page_size_changed)
        self._page_size_combo = combo
        row.addWidget(combo)

    def _on_page_size_changed(self, index: int) -> None:
        """Apply the new page size and rebuild the grid."""
        value = self._page_size_combo.itemData(index)
        if value is None:
            return
        value = int(value)
        if value == self._page_size:
            return
        self._page_size = value
        try:
            from src.portable_config import set_value

            set_value('library.page_size', value)
        except Exception:
            logger.exception("Could not save the library page size")
        self._load_limit = self._page_limit()
        self._refresh_grid()

    def _page_limit(self) -> int:
        """The number of rows the current page may pull.

        The "All" choice has no page limit, so the query is given a limit
        high enough to cover any library.
        """
        if self._page_size == ALL_CARDS:
            return NO_LIMIT
        return self._page_size

    def _refresh_collection_combo(self) -> None:
        """Rebuild the collection filter dropdown, preserving the selection.

        Checked collections that still exist keep their check; a deleted
        one is dropped instead of silently filtering by nothing.
        """
        options: list[tuple[str, int]] = [('No Collection', -1)]
        for col in self.db.list_collections():
            options.append((f"{col['name']} ({col['card_count']})", int(col['id'])))
        self._collection_filter.set_options(options)

    def _refresh_tag_filter(self) -> None:
        # Drop any existing selected-tag chips (everything after the fixed
        # header: label, button, clear button).
        while self._filter_bar_layout.count() > self._filter_header_count:
            item = self._filter_bar_layout.takeAt(self._filter_header_count)
            if item and item.widget():
                chip = item.widget()
                # Detach the chip first: while it is still parented to the
                # filter bar it keeps its old geometry and stays on screen
                # as a ghost until the deferred delete runs.
                chip.setParent(None)
                chip.deleteLater()
        for tag in sorted(self._selected_filter_tags):
            chip = TagChip(tag, removable=True)
            chip.removed.connect(self._remove_selected_tag)
            self._filter_bar_layout.addWidget(chip)
        self._clear_tags_btn.setEnabled(bool(self._selected_filter_tags))
        self._sync_filter_bar_height()

    def _sync_chip_heights(self) -> None:
        """Make every chip-styled control exactly as tall as the buttons.

        Qt sizes a label by its text and a checkbox by its indicator, so
        the chips would otherwise end up a few pixels off from the push
        buttons they are styled to look like.
        """
        height = self._fav_btn.sizeHint().height()
        for chip in self.findChildren(QWidget, 'filterChip'):
            chip.setFixedHeight(height)

    def _sync_filter_bar_height(self) -> None:
        """Size the tag-filter bar to its wrapped rows.

        With no tag selected the bar is a single row tall instead of a fixed
        block of empty space; chips make it grow as they wrap, up to a cap
        (a little shorter than before) where the scroll area takes over.
        """
        width = self._filter_bar_scroll.viewport().width()
        if width <= 0:
            width = max(240, self.width() - 24)
        rows_h = self._filter_bar_layout.heightForWidth(width)
        bar_h = _filter_bar_height(rows_h, max(90, load_font_size() * 7))
        self._filter_bar_scroll.setMinimumHeight(bar_h)
        self._filter_bar_scroll.setMaximumHeight(bar_h)
        self._filter_container.setFixedHeight(bar_h + 8)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # The chips re-wrap as the window narrows, so the bar has to
        # re-measure its rows.
        self._sync_filter_bar_height()

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
        # No check = no collection filter; otherwise match any of the
        # checked collections (the -1 "No Collection" sentinel included).
        selected = self._collection_filter.selected_values()
        self._filter_args = {
            'query': query,
            'tags': tags,
            'favorites_only': favorites_only,
            'collection_id': selected or None,
            'full_text': self._full_text_cb.isChecked(),
            'fuzzy': self._fuzzy_cb.isChecked(),
        }
        # A new filter starts at the first page again.
        self._load_limit = self._page_limit()
        self._refresh_grid()

    def _refresh_grid(self) -> None:
        """Re-run the current filter and rebuild the grid from the result.

        The query itself is bounded: only ``_load_limit`` rows come back
        from the database, so a huge library no longer freezes the UI.
        """
        entries, total = _search_page(
            self.db, sort_by=self._sort_by, limit=self._load_limit, **self._filter_args,
        )
        self._match_count = total
        self._rebuild_grid(entries)

    def _load_more(self) -> None:
        """Show the next page of matching cards."""
        if self._load_limit >= self._match_count:
            return
        self._load_limit = min(
            self._load_limit + self._page_limit(), self._match_count,
        )
        self._refresh_grid()

    def _ensure_loaded(self, char_id: int) -> bool:
        """Grow the loaded window until *char_id* is part of it.

        The grid only holds one page of cards, so a card further down the
        sorted list is not in ``_all_ids`` yet.  Returns True when the card
        is loaded afterwards.
        """
        if char_id in self._all_ids:
            return True
        while self._load_limit < self._match_count:
            self._load_limit = min(
                self._load_limit + self._page_limit(), self._match_count,
            )
            self._refresh_grid()
            if char_id in self._all_ids:
                return True
        return char_id in self._all_ids

    def _card_matches_filter(self, char_id: int) -> bool:
        """True when *char_id* passes the current filter.

        A single-row query, so a card that is filtered out is recognised
        without loading every matching card first.
        """
        rows, _total = _search_page(
            self.db, sort_by=self._sort_by, limit=1, only_id=char_id,
            **self._filter_args,
        )
        return bool(rows)

    def _on_manage_collections(self) -> None:
        from src.ui.widgets.collections_dialog import CollectionsManagerDialog

        dlg = CollectionsManagerDialog(self.db, self)
        changed = exec_dialog_with(dlg, lambda d: d.changed)
        if changed:
            self._refresh_collection_combo()
            # Re-apply unconditionally: deleting a collection that was
            # checked drops its check, so the grid must re-filter even
            # though no selection_changed signal fires.
            self._apply_filters()

    def _on_assign_collections(self, char_id: int | None = None) -> None:
        from src.ui.widgets.collections_dialog import CollectionAssignDialog

        char_id = self._selected_id if char_id is None else char_id
        if char_id is None:
            return
        entry = self.db.get_by_id(char_id)
        name = entry.get('name', '') if entry else ''
        dlg = CollectionAssignDialog(self.db, char_id, name, self)
        chosen = exec_dialog_with(
            dlg,
            lambda d: d.selected_collection_ids()
            if d.result() == QDialog.DialogCode.Accepted else None,
        )
        if chosen is not None:
            self.db.set_card_collections(char_id, chosen)
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
        self._update_grid_status()
        if container:
            container.setUpdatesEnabled(True)
            container.updateGeometry()
            container.update()

        def _restore() -> None:
            # The tab can be torn down before the timer fires; calling into a
            # destroyed scrollbar raises RuntimeError, and an exception
            # inside a slot aborts the whole process.
            from src.ui.widgets.chat_image_loader import _widget_alive

            if not _widget_alive(scrollbar):
                return
            try:
                scrollbar.setValue(min(scroll_pos, scrollbar.maximum()))
            except RuntimeError:
                pass

        if scroll_pos > 0:
            # After layout settles (the content height may not be final yet).
            QTimer.singleShot(0, _restore)

    def _update_grid_status(self) -> None:
        """Publish the filter result and refresh the load-more row.

        Three different numbers are involved here and they used to be
        conflated into one:

        * *matched* - how many cards the current filter matches
          (``_match_count``).  This is what the status bar reports, as
          "200 of 522 shown": how much of the library survives the filter.
        * *loaded* - how many of those cards are currently built as
          thumbnails.  The grid only holds one page at a time, so this is
          smaller than *matched* unless everything fits on one page.  It is
          what the row below the grid reports, next to the "load more"
          button.
        * *library total* - how many cards the library holds at all,
          ignoring the filter.  That is the denominator of the status bar.

        Reporting *loaded* as the numerator made the status bar read
        "100 of 522" for a filter that actually matched all 522 cards,
        because only the first page of thumbnails had been built.
        """
        loaded = len(self._thumbnails)
        matched = self._match_count
        # The status bar shows the filtered count from every tab, so the
        # filter state is published even when the row below the grid is
        # hidden (everything fits on one page).
        self.filter_stats_changed.emit(matched, self._library_total())
        if matched <= loaded:
            # Everything fits on one page: keep the old (clean) look.
            self._grid_status_row.setVisible(False)
            return
        self._grid_status_row.setVisible(True)
        self._grid_status.setText(f'Showing {loaded} of {matched} matching cards')
        self._load_more_btn.setEnabled(self._load_limit < matched)

    def _library_total(self) -> int:
        """The number of cards in the whole library, ignoring the filter."""
        try:
            return int(self.db.get_library_stats().get('count', 0) or 0)
        except Exception:
            # A failed aggregate must not take the grid down with it; the
            # match count is the next best denominator.
            logger.exception("Could not read the library size")
            return self._match_count

    def _add_thumbnail(self, entry: dict) -> None:
        thumb = CardThumbnail(
            char_id=entry['id'],
            name=entry['name'],
            thumb_path=entry.get('thumbnail_path', ''),
            is_favorite=bool(entry.get('is_favorite')),
            token_count=entry.get('token_count', 0),
        )
        if self._thumb_img_size != DEFAULT_THUMB_SIZE:
            thumb.set_thumb_size(self._thumb_img_size)
        thumb.selection_requested.connect(self._on_selection_requested)
        thumb.double_clicked.connect(self._show_full_image)
        thumb.context_menu_requested.connect(self._on_card_context_menu)
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
        removed = False
        for i, thumb in enumerate(self._thumbnails):
            if thumb.char_id == char_id:
                thumb.setParent(None)
                thumb.deleteLater()
                self._thumbnails.pop(i)
                removed = True
                break
        if char_id in self._all_ids:
            self._all_ids.remove(char_id)
        self._selection.prune(set(self._all_ids))
        self._grid_layout.activate()
        if removed:
            # The card is gone from the library, so it no longer matches the
            # filter either; without this the status bar kept reporting the
            # pre-delete count until the next rebuild.
            self._match_count = max(0, self._match_count - 1)
            self._update_grid_status()

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
                tags = safe_json.loads_safe(entry['tags'])
            except (json.JSONDecodeError, TypeError, ValueError):
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

        self._delete_btn.setEnabled(True)
        self._export_png_btn.setEnabled(True)
        self._export_json_btn.setEnabled(True)
        self._open_folder_btn.setEnabled(True)
        self._fav_btn.setEnabled(True)
        self._add_to_collection_btn.setEnabled(True)
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
        self._sync_filter_bar_height()
        self._sync_chip_heights()
        # The rating stars carry their own pixel size, so they have to be
        # told about the new one.
        self._detail_rating.refresh_font_size()

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
        # A saved card may live on a later page: load what is needed to
        # select it instead of silently dropping the selection.
        for char_id in list(ids):
            if char_id in valid:
                continue
            if self._card_matches_filter(char_id) and self._ensure_loaded(char_id):
                valid.add(char_id)
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
            "WARNING: This permanently removes them from the library and "
            "deletes their card files from disk.\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # The names are read before the rows are gone: the event carries the
        # name of the card that was deleted.
        names: dict[int, str] = {}
        for char_id in selected:
            entry = self.db.get_by_id(char_id)
            names[char_id] = entry.get('name', '') if entry else ''
        deleted = 0
        for char_id in selected:
            try:
                self.db.remove_card(char_id, delete_files=True)
                self._card_cache.pop(char_id, None)
            except Exception:
                logger.exception("Bulk delete failed for card %s", char_id)
                continue
            deleted += 1
            notify_card_deleted(char_id, names.get(char_id, ''))
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
        # The deleted cards no longer match the filter, so the counts have to
        # follow; otherwise the status bar keeps the pre-delete total.
        self._match_count = max(0, self._match_count - deleted)
        self._update_grid_status()
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
            except (OSError, vault.VaultError) as e:
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
        self.search_changed.emit(text)

    def set_search_text(self, text: str) -> None:
        """Mirror the shared sidebar's search box (no echo back to it)."""
        if self._search_edit.text() == text:
            return
        self._search_edit.blockSignals(True)
        self._search_edit.setText(text)
        self._search_edit.blockSignals(False)
        self._search_timer.start()

    def _clear_all_filters(self) -> None:
        """Reset every grid filter so a hidden card can be revealed.

        The search box is cleared through the normal channel so the shared
        sidebar's search box clears with it.
        """
        self._selected_filter_tags.clear()
        self._refresh_tag_filter()
        self._favorites_only_cb.blockSignals(True)
        self._favorites_only_cb.setChecked(False)
        self._favorites_only_cb.blockSignals(False)
        self._collection_filter.clear_selection()
        self._search_timer.stop()
        if self._search_edit.text():
            self._search_edit.blockSignals(True)
            self._search_edit.clear()
            self._search_edit.blockSignals(False)
            self.search_changed.emit('')
        self._apply_filters()

    def reveal_card(self, char_id: int | None, clear_filters: bool = False) -> None:
        """Select a card and scroll it into view.

        Used to keep the grid in step with the shared sidebar's selection.

        *clear_filters* decides what happens when the current filters hide
        the card.  It is on for an *explicit* selection (the user asked for
        this card, so the grid has to show it) and off when the grid is
        merely brought into step with the sidebar - switching to the
        Library tab used to wipe the search box, the tag filter and the
        collection filter (and the shared sidebar's search box with them)
        because the selected card happened to be hidden.
        """
        if char_id is None:
            return
        if char_id not in self._all_ids:
            if self._card_matches_filter(char_id):
                # The card matches but sits on a later page: load it.
                self._ensure_loaded(char_id)
            elif clear_filters:
                # Filtered out: clear the filters and try once more.
                self._clear_all_filters()
                self._ensure_loaded(char_id)
            else:
                entry = self.db.get_by_id(char_id)
                name = (entry or {}).get('name') or 'The selected card'
                self.status_message.emit(
                    f'{name} is hidden by the current filters.', 4000,
                )
                return
        self.select_card(char_id)

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

        # A previous dialog must not be left behind as a hidden child.
        self._dispose_progress_dialog()
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

    def _dispose_progress_dialog(self) -> None:
        """Close and delete the import progress dialog.

        The dialog is parented to the tab, so dropping the Python reference
        leaves the C++ widget alive (a hidden child) for the lifetime of the
        tab - one leak per import.
        """
        dialog = self._progress_dialog
        self._progress_dialog = None
        if dialog is None:
            return
        try:
            dialog.close()
            dialog.deleteLater()
        except RuntimeError:
            # The C++ object is already gone.
            pass

    def _on_import_finished(self, summary) -> None:
        self._dispose_progress_dialog()
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

    def _on_edit(self, char_id: int | None = None) -> None:
        char_id = self._selected_id if char_id is None else char_id
        if char_id is not None:
            self.edit_requested.emit(char_id)

    def _on_delete(self, char_id: int | None = None) -> None:
        char_id = self._selected_id if char_id is None else char_id
        if char_id is None:
            return
        entry = self.db.get_by_id(char_id)
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
            deleted_id = char_id
            deleted_name = entry.get('name', '')
            try:
                self.db.remove_card(deleted_id, delete_files=True)
            except Exception as e:
                # An exception escaping a slot aborts the process; a locked
                # file or a busy database must show an error instead.
                logger.exception("Delete of card %s failed", deleted_id)
                QMessageBox.critical(self, 'Delete Character', f'Could not delete the card: {e}')
                return
            notify_card_deleted(deleted_id, deleted_name)
            self._remove_thumbnail_in_place(deleted_id)
            # The deleted card may not have been the selected one (the
            # sidebar's menu can target any card): drop it from the selection
            # model and let the selection UI settle on what is left.
            self._selection.prune(set(self._all_ids))
            self._update_selection_ui()
            self.library_changed.emit()

    def _on_export_png(self, char_id: int | None = None) -> None:
        char_id = self._selected_id if char_id is None else char_id
        if char_id is None:
            return
        entry = self.db.get_by_id(char_id)
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
        from src import vault
        try:
            vault.copy_out(source, dest)
            self.status_message.emit(f"Exported PNG to {Path(dest).name}", 4000)
        except (OSError, vault.VaultError) as e:
            logger.exception("PNG export failed")
            QMessageBox.critical(self, 'Export', f"Failed to export PNG: {e}")

    def _on_export_json(self, char_id: int | None = None) -> None:
        char_id = self._selected_id if char_id is None else char_id
        if char_id is None:
            return
        entry = self.db.get_by_id(char_id)
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

    def _on_open_folder(self, char_id: int | None = None) -> None:
        char_id = self._selected_id if char_id is None else char_id
        if char_id is None:
            return
        entry = self.db.get_by_id(char_id)
        if not entry:
            return
        source = entry.get('source_path', '')
        from src.path_utils import open_containing_folder
        if not open_containing_folder(source):
            QMessageBox.warning(self, 'Open Folder', 'Source file not found.')
            logger.warning("Could not open folder for card %s (source missing)", char_id)

    def _on_toggle_favorite(self, char_id: int | None = None) -> None:
        char_id = self._selected_id if char_id is None else char_id
        if char_id is None:
            return
        try:
            new_state = self.db.toggle_favorite(char_id)
            if char_id == self._selected_id:
                self._fav_btn.setChecked(new_state)
            # Update just the affected thumbnail instead of rebuilding the grid.
            self._refresh_single_thumbnail(char_id)
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

    def _on_card_context_menu(self, char_id: int, global_pos) -> None:
        """Right-click on a card thumbnail: show the shared card menu.

        A right-click inside a multi-selection keeps the selection and makes
        Delete/Favorite apply to all of it; everything else targets the
        right-clicked card.  Outside a multi-selection the card is simply
        selected first, so the whole menu applies to it.
        """
        selected = self._selection.selected_ids
        multi = char_id in selected and len(selected) > 1
        if not multi:
            self._on_selection_requested(char_id, False, False)
        entry = self.db.get_by_id(char_id)
        if not entry:
            return
        if multi:
            handlers = {
                'edit': lambda: self._on_edit(char_id),
                'collections': lambda: self._on_assign_collections(char_id),
                'full_image': lambda: self._show_full_image(char_id),
                'open_folder': lambda: self._on_open_folder(char_id),
                'export_png': lambda: self._on_export_png(char_id),
                'export_json': lambda: self._on_export_json(char_id),
                'duplicate': lambda: self.duplicate_card(char_id),
                'favorite_add': lambda: self._on_bulk_favorite(True),
                'favorite_remove': lambda: self._on_bulk_favorite(False),
                'delete': self._on_bulk_delete,
            }
            show_card_context_menu(
                self, global_pos, handlers, multi_count=len(selected),
            )
        else:
            handlers = {
                'edit': lambda: self._on_edit(char_id),
                'favorite': lambda: self._on_toggle_favorite(char_id),
                'collections': lambda: self._on_assign_collections(char_id),
                'full_image': lambda: self._show_full_image(char_id),
                'open_folder': lambda: self._on_open_folder(char_id),
                'export_png': lambda: self._on_export_png(char_id),
                'export_json': lambda: self._on_export_json(char_id),
                'duplicate': lambda: self.duplicate_card(char_id),
                'delete': lambda: self._on_delete(char_id),
            }
            show_card_context_menu(
                self, global_pos, handlers,
                favorite_on=bool(entry.get('is_favorite')),
            )

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
        # Card data may have changed; refresh the grid. Deletions from the
        # scanner must also invalidate the parsed-card cache (the menu path
        # in MainWindow does the same).
        self.invalidate_card_cache()
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

    def _on_favorites_toggled(self, checked: bool) -> None:
        """The checkbox re-filters the grid and tells the shared sidebar."""
        self._apply_filters()
        self.favorites_filter_changed.emit(checked)

    def set_favorites_only(self, checked: bool) -> None:
        """Mirror the sidebar's "Favorites only" filter (no echo back)."""
        if self._favorites_only_cb.isChecked() == checked:
            return
        self._favorites_only_cb.setChecked(checked)

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

    def reset_thumbnail_size(self) -> None:
        """Restore the default thumbnail size (View > Reset Zoom, Ctrl+0)."""
        if self._thumb_img_size != DEFAULT_THUMB_SIZE:
            self._thumb_img_size = DEFAULT_THUMB_SIZE
            self._zoom_timer.start()

    def _apply_zoom(self) -> None:
        """Apply the pending thumbnail size to all current thumbnails."""
        for thumb in self._thumbnails:
            thumb.set_thumb_size(self._thumb_img_size)
        self._grid_layout.activate()

    def delete_selected(self) -> None:
        """Delete the currently selected card (menu entry)."""
        self._on_delete()

    def duplicate_card(self, char_id: int | None = None) -> None:
        """Duplicate the selected card into the library with a '(copy)' suffix."""
        char_id = self._selected_id if char_id is None else char_id
        if char_id is None:
            return
        entry = self.db.get_by_id(char_id)
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

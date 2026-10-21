"""A keyboard-driven command palette.

The palette is a small dialog with a filter box and a list of the
application's commands.  Typing filters the list, the arrow keys move the
selection, Enter runs the highlighted command and Escape closes the
palette without running anything.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QRect, Qt
from PyQt6.QtGui import QAction, QPainter, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

# The shortcut of a row is stored next to the user role (which carries the
# action id) so the list can render it in a second column.
SHORTCUT_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def fuzzy_score(query: str, label: str) -> int:
    """Score how well *label* matches *query* (higher is better).

    The match itself is a plain subsequence test, but the *order* of the
    results matters: an exact prefix should come first, then a match at a
    word boundary, then a match anywhere.  Without a score the first
    registry entry won, so "sort" showed "Sort by Date Added" before
    "Sort by Rating".
    """
    if not query.strip():
        return 0
    needle = query.casefold().replace(' ', '')
    haystack = label.casefold()
    position = 0
    gaps = 0
    for char in needle:
        found = haystack.find(char, position)
        if found < 0:
            return -1
        if found != position:
            gaps += found - position
        position = found + 1
    score = 100
    if haystack.startswith(needle):
        score += 200
    elif needle in haystack:
        score += 80
    if ' ' + needle in haystack:
        score += 40
    # Prefer short labels: "View > Sort > Rating" over "View > Sort > Random".
    score -= len(label) // 4
    score -= gaps
    return score


def fuzzy_match(query: str, label: str) -> bool:
    """True when every character of *query* appears in *label*, in order.

    The match is case-insensitive and ignores whitespace in the query, so
    ``"vsr"`` finds ``"View > Sort > Rating"``.  An empty query matches
    everything.
    """
    if not query:
        return True
    haystack = label.casefold()
    position = 0
    for char in query.casefold():
        if char.isspace():
            continue
        found = haystack.find(char, position)
        if found < 0:
            return False
        position = found + 1
    return True


def filter_entries(
    entries: list[tuple[str, str, str]], query: str
) -> list[tuple[str, str, str]]:
    """Return the entries whose label matches *query*, best match first."""
    if not query.strip():
        return list(entries)
    scored: list[tuple[int, int, tuple[str, str, str]]] = []
    for index, entry in enumerate(entries):
        score = fuzzy_score(query, entry[1])
        if score < 0:
            continue
        scored.append((-score, index, entry))
    scored.sort()
    return [entry for _score, _index, entry in scored]


class _ShortcutDelegate(QStyledItemDelegate):
    """Paints the keyboard shortcut in a right-aligned second column."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        shortcut = index.data(SHORTCUT_ROLE) or ''
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # initStyleOption() may reset the geometry - keep the item rect.
        opt.rect = QRect(option.rect)
        label = opt.text
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        # Let the style draw the row (background, selection, frame) but not
        # the text: the label and the shortcut are drawn separately so the
        # shortcut can live in its own column.
        opt.text = ''
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        painter.save()
        color = opt.palette.color(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text,
        )
        painter.setPen(color)
        metrics = opt.fontMetrics
        margin = 10
        item_rect = opt.rect
        shortcut_width = metrics.horizontalAdvance(shortcut) + margin * 2 if shortcut else 0
        shortcut_rect = QRect(
            item_rect.right() - shortcut_width + 1,
            item_rect.top(),
            max(0, shortcut_width - 1),
            item_rect.height(),
        )
        label_rect = QRect(item_rect)
        label_rect.setLeft(item_rect.left() + 6)
        label_rect.setRight(shortcut_rect.left() - 4)
        painter.drawText(
            label_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            label,
        )
        if shortcut:
            painter.setPen(
                opt.palette.color(
                    QPalette.ColorRole.HighlightedText if selected
                    else QPalette.ColorRole.PlaceholderText,
                ),
            )
            painter.drawText(
                shortcut_rect,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                shortcut,
            )
        painter.restore()

    def sizeHint(self, option, index) -> object:
        size = super().sizeHint(option, index)
        shortcut = index.data(SHORTCUT_ROLE) or ''
        if shortcut:
            metrics = option.fontMetrics
            size.setWidth(size.width() + metrics.horizontalAdvance(shortcut) + 20)
        return size


class CommandPalette(QDialog):
    """A filterable list of the application's commands.

    *entries* is a list of ``(action_id, label, shortcut_text)`` tuples and
    *actions* maps an action id to the :class:`QAction` that runs it.
    """

    def __init__(
        self,
        entries: list[tuple[str, str, str]],
        actions: dict[str, QAction],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._entries = list(entries)
        self._actions = dict(actions)
        self._visible_entries: list[tuple[str, str, str]] = []

        self.setWindowTitle('Command Palette')
        # Window-modal: the rest of the app is blocked while the palette is
        # open, but the event loop keeps running (so the command the user
        # picks can open its own dialog).
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(self._stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._search = QLineEdit()
        self._search.setPlaceholderText('Type a command...')
        self._search.setClearButtonEnabled(True)
        self._search.setAccessibleName('Command palette search')
        self._search.setAccessibleDescription(
            'Type to filter the commands, use the arrow keys to choose one '
            'and Enter to run it.',
        )
        self._search.textChanged.connect(self._apply_filter)
        self._search.installEventFilter(self)
        layout.addWidget(self._search)

        self._list = QListWidget()
        self._list.setItemDelegate(_ShortcutDelegate(self._list))
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setAccessibleName('Available commands')
        self._list.setWordWrap(False)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.itemActivated.connect(self._on_item_activated)
        layout.addWidget(self._list)

        self.resize(520, 380)
        self._apply_filter('')

    # ---- theming ----

    @staticmethod
    def _stylesheet() -> str:
        """Build the palette stylesheet from the active theme.

        The palette draws itself (rounded, translucent background), so it
        cannot use the application stylesheet directly - but it must still
        follow the theme, or it stays dark in the light theme.
        """
        from src.ui.styles import current_palette

        colours = current_palette()
        background = colours['bg']
        panel = colours['bg_deep']
        border = colours['border_strong']
        text = colours['fg']
        selection = colours['select']
        accent = colours['accent']
        return f"""
            QDialog {{
                background-color: {background};
                border: 1px solid {border};
                border-radius: 6px;
            }}
            QLineEdit {{
                background-color: {panel};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 6px 8px;
                color: {text};
                selection-background-color: {accent};
            }}
            QLineEdit:focus {{
                border: 1px solid {accent};
            }}
            QListWidget {{
                background-color: transparent;
                border: none;
                color: {text};
                outline: none;
            }}
            QListWidget::item {{
                padding: 5px 6px;
                border-radius: 3px;
            }}
            QListWidget::item:selected {{
                background-color: {selection};
                color: {colours['fg_bright']};
            }}
        """

    # ---- filtering ----

    def _apply_filter(self, query: str) -> None:
        """Show only the entries matching *query*."""
        self._visible_entries = filter_entries(self._entries, query)
        self._list.clear()
        for action_id, label, shortcut in self._visible_entries:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, action_id)
            item.setData(SHORTCUT_ROLE, shortcut)
            item.setToolTip(f'{label}  ({shortcut})' if shortcut else label)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)
        self._list.setVisible(bool(self._visible_entries))

    @property
    def visible_entries(self) -> list[tuple[str, str, str]]:
        """The entries currently offered (after filtering)."""
        return list(self._visible_entries)

    def current_action_id(self) -> str | None:
        """The action id of the highlighted row (None when there is none)."""
        item = self._list.currentItem()
        if item is None:
            return None
        action_id = item.data(Qt.ItemDataRole.UserRole)
        return str(action_id) if action_id is not None else None

    # ---- interaction ----

    def _move_selection(self, delta: int) -> None:
        if not self._list.count():
            return
        row = self._list.currentRow()
        row = max(0, min(self._list.count() - 1, row + delta))
        self._list.setCurrentRow(row)

    def _select_row(self, row: int) -> None:
        """Move the selection to *row* (clamped to the list)."""
        if not self._list.count():
            return
        self._list.setCurrentRow(max(0, min(self._list.count() - 1, row)))

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        self.trigger_selected()

    def trigger_selected(self) -> None:
        """Run the highlighted command and close the palette."""
        action_id = self.current_action_id()
        if not action_id:
            return
        action = self._actions.get(action_id)
        if action is None:
            return
        # Close first: the command may open another (modal) window and the
        # palette must not stay on top of it.
        self.close()
        if not action.isEnabled():
            return
        action.trigger()

    def eventFilter(self, obj, event) -> bool:
        if obj is self._search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                self._move_selection(1)
                return True
            if key == Qt.Key.Key_Up:
                self._move_selection(-1)
                return True
            if key == Qt.Key.Key_PageDown:
                self._move_selection(8)
                return True
            if key == Qt.Key.Key_PageUp:
                self._move_selection(-8)
                return True
            if key == Qt.Key.Key_Home:
                self._select_row(0)
                return True
            if key == Qt.Key.Key_End:
                self._select_row(self._list.count() - 1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.trigger_selected()
                return True
            if key == Qt.Key.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(obj, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._search.clear()
        self._search.setFocus()
        self._search.selectAll()
        self._center_over_parent()

    def _center_over_parent(self) -> None:
        """Centre the palette over the window it belongs to."""
        parent = self.parentWidget()
        if parent is None:
            return
        geometry = self.frameGeometry()
        geometry.moveCenter(parent.frameGeometry().center())
        self.setGeometry(geometry)

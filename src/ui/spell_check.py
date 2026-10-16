"""Spell-check integration for text widgets.

``SpellCheckMixin`` adds red spell-check underlines (via
``QTextEdit.ExtraSelection``) and a context menu with suggested corrections,
"Ignore word" and "Add word to dictionary" to the plain-text editors.
``SpellCheckTextEdit`` / ``SpellCheckPlainTextEdit`` are drop-in replacements
for ``QTextEdit`` / ``QPlainTextEdit``; ``ChatInputEdit`` and friends inherit
from them so every editable field gets the same behaviour.
"""
from __future__ import annotations

from weakref import WeakSet

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QColor, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import QMenu, QPlainTextEdit, QTextEdit, QWidget

from src.spellcheck import get_checker

_RECHECK_MS = 300
_RETRY_MS = 500
_MISSPELLED_COLOR = QColor('red')

# Every live spell-checked widget, so a settings toggle can refresh them all.
_widgets: 'WeakSet[QWidget]' = WeakSet()


def _misspelled_format() -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
    fmt.setUnderlineColor(_MISSPELLED_COLOR)
    return fmt


def apply_enabled_to_all() -> None:
    """Re-apply the global enable flag to every open spell-checked widget."""
    from src.settings_manager import load_spellcheck_enabled

    enabled = load_spellcheck_enabled()
    for widget in list(_widgets):
        setter = getattr(widget, 'setSpellCheckEnabled', None)
        if setter is not None:
            setter(enabled)


class SpellCheckMixin:
    """Spell-check behaviour shared by the QTextEdit/QPlainTextEdit subclasses.

    Cooperative with Qt widget ``__init__``: subclasses may override
    ``keyPressEvent`` and call ``super()`` normally.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._spell_enabled = True
        self._spell_timer = QTimer(self)
        self._spell_timer.setSingleShot(True)
        self._spell_timer.setInterval(_RECHECK_MS)
        self._spell_timer.timeout.connect(self._spell_recheck)
        self.textChanged.connect(self._spell_schedule)
        _widgets.add(self)
        from src.settings_manager import load_spellcheck_enabled

        self._spell_enabled = load_spellcheck_enabled()
        self._spell_schedule()

    # -- public API -----------------------------------------------------------

    def setSpellCheckEnabled(self, enabled: bool) -> None:
        self._spell_enabled = bool(enabled)
        self._spell_recheck()

    # -- rechecking -----------------------------------------------------------

    def _spell_schedule(self) -> None:
        self._spell_timer.start()

    def _spell_recheck(self) -> None:
        checker = get_checker()
        checker.ensure_loading()
        if not self._spell_enabled or self.isReadOnly():
            self.setExtraSelections([])
            return
        if not checker.is_ready():
            # Dictionary still building: come back when it is done.
            self._spell_timer.start(_RETRY_MS)
            return
        if not checker.available:
            self.setExtraSelections([])
            return
        selections = []
        for start, end, _word in checker.misspellings(self.toPlainText()):
            selection = QTextEdit.ExtraSelection()
            selection.cursor = self._spell_span_cursor(start, end)
            selection.format = _misspelled_format()
            selections.append(selection)
        self.setExtraSelections(selections)

    def _spell_span_cursor(self, start: int, end: int) -> QTextCursor:
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        return cursor

    def _spell_misspelling_at(self, pos) -> tuple[int, int, str] | None:
        """Misspelled word spanning the widget position *pos*, if any."""
        cursor = self.cursorForPosition(pos)
        target = cursor.position()
        for start, end, word in get_checker().misspellings(self.toPlainText()):
            if start <= target <= end:
                return start, end, word
        return None

    # -- context menu -----------------------------------------------------------

    def contextMenuEvent(self, event) -> None:
        menu = self.createStandardContextMenu()
        span = None
        if self._spell_enabled and not self.isReadOnly():
            checker = get_checker()
            if checker.available:
                span = self._spell_misspelling_at(event.pos())
        if span is not None:
            self._spell_extend_menu(menu, span)
        menu.exec(event.globalPos())
        menu.deleteLater()

    def _spell_extend_menu(self, menu: QMenu, span: tuple[int, int, str]) -> None:
        start, end, word = span
        checker = get_checker()
        first = menu.actions()[0] if menu.actions() else None
        for suggestion in checker.suggestions(word):
            action = QAction(suggestion, menu)
            action.triggered.connect(
                lambda _checked=False, s=suggestion, st=start, en=end: self._spell_replace(st, en, s),
            )
            menu.insertAction(first, action)
        ignore = QAction(f'Ignore "{word}"', menu)
        ignore.triggered.connect(lambda _checked=False, w=word: self._spell_ignore(w))
        add = QAction(f'Add "{word}" to dictionary', menu)
        add.triggered.connect(lambda _checked=False, w=word: self._spell_add(w))
        menu.insertAction(first, add)
        menu.insertAction(first, ignore)
        if first is not None:
            menu.insertSeparator(first)

    def _spell_replace(self, start: int, end: int, text: str) -> None:
        self._spell_span_cursor(start, end).insertText(text)

    def _spell_ignore(self, word: str) -> None:
        get_checker().ignore_word(word)
        self._spell_recheck()

    def _spell_add(self, word: str) -> None:
        get_checker().add_word(word)
        self._spell_recheck()


class SpellCheckTextEdit(SpellCheckMixin, QTextEdit):
    """Drop-in ``QTextEdit`` with spell-check underlines and suggestions."""


class SpellCheckPlainTextEdit(SpellCheckMixin, QPlainTextEdit):
    """Drop-in ``QPlainTextEdit`` with spell-check underlines and suggestions."""

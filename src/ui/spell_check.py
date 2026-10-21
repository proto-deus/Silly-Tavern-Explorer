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

#: A cap on the number of underlines.  Every selection makes Qt re-lay-out
#: the whole document, so a field full of typos must not cost thousands of
#: them.
_MAX_SPELL_SELECTIONS = 500

# Every live spell-checked widget, so a settings toggle can refresh them all.
_widgets: 'WeakSet[QWidget]' = WeakSet()


def _common_prefix_len(old: str, new: str) -> int:
    """The length of the common prefix of *old* and *new*."""
    limit = min(len(old), len(new))
    index = 0
    while index < limit and old[index] == new[index]:
        index += 1
    return index


def _common_suffix_len(old: str, new: str, prefix: int) -> int:
    """The length of the common suffix of *old* and *new*.

    *prefix* is the length of the common prefix; the two must not overlap.
    """
    limit = min(len(old), len(new)) - prefix
    index = 0
    while (
        index < limit
        and old[len(old) - 1 - index] == new[len(new) - 1 - index]
    ):
        index += 1
    return index


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == '_'


def _word_start(text: str, index: int) -> int:
    """The start of the word containing *index*."""
    index = max(0, min(index, len(text)))
    while index > 0 and _is_word_char(text[index - 1]):
        index -= 1
    return index


def _word_end(text: str, index: int) -> int:
    """The end of the word containing *index*."""
    index = max(0, min(index, len(text)))
    while index < len(text) and _is_word_char(text[index]):
        index += 1
    return index


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
        # The text the cached misspellings were computed from, and the
        # misspellings themselves.  They let a re-check look at the region
        # that actually changed instead of the whole document.
        self._spell_text: str = ''
        self._spell_results: list[tuple[int, int, str]] = []
        _widgets.add(self)
        from src.settings_manager import load_spellcheck_enabled

        self._spell_enabled = load_spellcheck_enabled()
        self._spell_schedule()

    # -- public API -----------------------------------------------------------

    def setSpellCheckEnabled(self, enabled: bool) -> None:
        self._spell_enabled = bool(enabled)
        self._spell_text = ''
        self._spell_results = []
        self._spell_recheck()

    # -- rechecking -----------------------------------------------------------

    def _spell_schedule(self) -> None:
        # ``start(msec)`` would set the timer's interval permanently, so the
        # interval is restored explicitly instead.
        self._spell_timer.setInterval(_RECHECK_MS)
        self._spell_timer.start()

    def _spell_recheck(self, force: bool = False) -> None:
        checker = get_checker()
        checker.ensure_loading()
        if force:
            # The dictionary changed (a word was ignored or added): the
            # cached results are stale.
            self._spell_text = ''
            self._spell_results = []
        if not self._spell_enabled or self.isReadOnly():
            self.setExtraSelections([])
            self._spell_text = self.toPlainText()
            self._spell_results = []
            return
        if not checker.is_ready():
            # Dictionary still building: come back when it is done.
            self._spell_timer.setInterval(_RETRY_MS)
            self._spell_timer.start()
            return
        # The dictionary is ready: back to the normal debounce.
        self._spell_timer.setInterval(_RECHECK_MS)
        if not checker.available:
            self.setExtraSelections([])
            return
        text = self.toPlainText()
        if text == self._spell_text:
            # Nothing changed: re-applying the same selections would force
            # Qt to re-lay-out the whole document for nothing.
            return
        results = self._misspellings(checker, text)
        self._spell_text = text
        self._spell_results = results
        selections = []
        for start, end, _word in results[:_MAX_SPELL_SELECTIONS]:
            selection = QTextEdit.ExtraSelection()
            selection.cursor = self._spell_span_cursor(start, end)
            selection.format = _misspelled_format()
            selections.append(selection)
        self.setExtraSelections(selections)

    def _misspellings(self, checker, text: str) -> list[tuple[int, int, str]]:
        """The misspellings in *text*, re-checking only what changed.

        A full re-check of a long field on every pause in typing is what
        made the editor feel sluggish: the whole document was scanned, and
        ``setExtraSelections`` made Qt re-lay it out.  Only the region that
        changed is re-scanned here; the rest of the cached results keep
        their offsets (the text before and after the region is unchanged).
        """
        previous = self._spell_results
        old_text = self._spell_text
        if not old_text or not previous:
            return checker.misspellings(text)
        prefix = _common_prefix_len(old_text, text)
        suffix = _common_suffix_len(old_text, text, prefix)
        region_start = prefix
        region_end = len(text) - suffix
        if region_start >= region_end:
            # Pure insertion or deletion: check the whole thing, the
            # boundaries are not worth the trouble.
            return checker.misspellings(text)
        # Widen the region to whole words, so a word that was split by the
        # edit is checked as a whole.
        region_start = _word_start(text, region_start)
        region_end = _word_end(text, region_end)
        local = [
            (start + region_start, end + region_start, word)
            for start, end, word in checker.misspellings(text[region_start:region_end])
        ]
        merged = [
            item for item in previous
            if item[1] <= region_start or item[0] >= region_end
        ]
        merged.extend(local)
        merged.sort(key=lambda item: item[0])
        return merged

    def _spell_span_cursor(self, start: int, end: int) -> QTextCursor:
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        return cursor

    def _spell_misspelling_at(self, pos) -> tuple[int, int, str] | None:
        """Misspelled word spanning the widget position *pos*, if any."""
        cursor = self.cursorForPosition(pos)
        target = cursor.position()
        for start, end, word in self._spell_results:
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
        self._spell_recheck(force=True)

    def _spell_add(self, word: str) -> None:
        get_checker().add_word(word)
        self._spell_recheck(force=True)


class SpellCheckTextEdit(SpellCheckMixin, QTextEdit):
    """Drop-in ``QTextEdit`` with spell-check underlines and suggestions."""


class SpellCheckPlainTextEdit(SpellCheckMixin, QPlainTextEdit):
    """Drop-in ``QPlainTextEdit`` with spell-check underlines and suggestions."""

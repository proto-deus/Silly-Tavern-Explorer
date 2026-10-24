from __future__ import annotations

from collections.abc import Iterable, Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QMenu, QToolButton, QWidget


class _CheckMenu(QMenu):
    """A menu that stays open while its checkable actions are toggled.

    QMenu hides itself as soon as an action is activated, which makes
    ticking several boxes a one-click-per-popup chore.  Checkable items are
    toggled in place instead, so the popup only closes when the user clicks
    outside it (or a non-checkable item).
    """

    def mouseReleaseEvent(self, event) -> None:
        action = self.actionAt(event.pos())
        if action is not None and action.isCheckable() and action.isEnabled():
            action.toggle()
            return
        super().mouseReleaseEvent(event)

    def keyReleaseEvent(self, event) -> None:
        # QMenu activates the highlighted action on Space/Enter/Return from
        # the key *release*; toggle instead so the popup stays open for the
        # keyboard as well.
        action = self.activeAction()
        if (
            action is not None
            and action.isCheckable()
            and action.isEnabled()
            and event.key()
            in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            action.toggle()
            return
        super().keyReleaseEvent(event)


class MultiSelectDropdown(QToolButton):
    """A drop-down button whose popup items are checkboxes.

    Any number of items can be checked at once and the caption summarises
    the selection.  With nothing checked no filter is active - callers treat
    an empty selection as "match everything".

    ``selection_changed`` is only emitted for user edits; the programmatic
    ``set_options`` / ``set_selected`` / ``clear_selection`` never re-trigger
    a reload on their own.
    """

    selection_changed = pyqtSignal()

    def __init__(self, empty_text: str = 'All', parent: QWidget | None = None):
        super().__init__(parent)
        self._empty_text = empty_text
        self._updating = False
        self._actions: dict = {}
        self._menu = _CheckMenu(self)
        self.setMenu(self._menu)
        # The whole button opens the popup - it is a filter, not a command.
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolTip('Check one or more to filter; none checked shows all')
        self._update_caption()

    # ---- contents ----

    def set_options(self, items: Sequence[tuple[str, object]]) -> None:
        """Replace the popup items, keeping the checks that still exist.

        *items* are ``(label, value)`` pairs; values must be hashable.
        """
        selected = set(self.selected_values())
        self._updating = True
        self._menu.clear()
        self._actions.clear()
        for label, value in items:
            action = self._menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(value in selected)
            action.toggled.connect(self._on_action_toggled)
            self._actions[value] = action
        self._updating = False
        self._update_caption()

    def selected_values(self) -> list:
        """The checked values, in the order their options were added."""
        return [v for v, action in self._actions.items() if action.isChecked()]

    def set_selected(self, values: Iterable) -> None:
        """Check exactly *values* (no ``selection_changed`` is emitted)."""
        wanted = set(values)
        self._updating = True
        for value, action in self._actions.items():
            action.setChecked(value in wanted)
        self._updating = False
        self._update_caption()

    def clear_selection(self) -> None:
        """Uncheck everything (no ``selection_changed`` is emitted)."""
        self.set_selected(())

    # ---- internals ----

    def _on_action_toggled(self, _checked: bool = False) -> None:
        if self._updating:
            return
        self._update_caption()
        self.selection_changed.emit()

    def _update_caption(self) -> None:
        labels = [
            action.text()
            for action in self._actions.values()
            if action.isChecked()
        ]
        if not labels:
            self.setText(self._empty_text)
        elif len(labels) == 1:
            self.setText(labels[0])
        else:
            self.setText(f'{labels[0]} (+{len(labels) - 1})')

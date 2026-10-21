"""Focus helpers for window-level keyboard shortcuts.

Several ST Explorer actions are bound to *bare* keys (``Del``, ``F``) on the
main window, so they are active no matter which widget has the keyboard focus.
That is fine while the Library grid has the focus, but a bare key is also
meaningful inside combo boxes, list views and text fields: ``Del`` inside a
collection filter used to delete the selected card from the library.

``focus_blocks_shortcut`` centralises the decision "may a bare-key window
shortcut fire right now?" so every such action behaves the same way.
"""

from __future__ import annotations

from typing import Any

# The blocking widget classes are:
#
# * text entry - the key is typed into the field;
# * item views - the key navigates or edits the selection;
# * combo boxes / spin boxes / sliders - the key changes the value;
# * check boxes and radio buttons - they are filter controls the user just
#   clicked into;
# * a key-sequence editor - the key is captured as a shortcut.
#
# Plain push buttons are deliberately *not* included: they do not consume a
# bare letter, so a window shortcut should still work while one has focus.


def _blocking_types() -> tuple[type, ...]:
    """Resolve the blocking widget classes (Qt is imported lazily)."""
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QAbstractSpinBox,
        QComboBox,
        QKeySequenceEdit,
        QLineEdit,
        QPlainTextEdit,
        QSlider,
        QTextEdit,
    )

    return (
        QAbstractItemView,
        QAbstractSpinBox,
        QComboBox,
        QKeySequenceEdit,
        QLineEdit,
        QPlainTextEdit,
        QSlider,
        QTextEdit,
    )


def _is_toggle_button(widget: Any) -> bool:
    """True for check boxes and radio buttons (but not push buttons).

    A toggle is a filter control the user just clicked into, so a bare
    key belongs to it.  A plain push button ignores letters, so the window
    shortcut should still work while one has the focus.
    """
    from PyQt6.QtWidgets import QAbstractButton, QPushButton

    if isinstance(widget, QPushButton):
        return False
    return isinstance(widget, QAbstractButton)


def focus_blocks_shortcut(widget: Any) -> bool:
    """True when *widget* should keep a bare-key window shortcut for itself.

    The check walks up the parent chain, so a ``QLineEdit`` inside a
    ``QComboBox`` (the editable variant) still counts as blocking.  ``None``
    (nothing focused) never blocks.
    """
    if widget is None:
        return False
    blocking = _blocking_types()
    current = widget
    # A bounded walk: a pathological parent chain must not hang the UI.
    for _ in range(16):
        if isinstance(current, blocking) or _is_toggle_button(current):
            return True
        parent = current.parentWidget()
        if parent is None or parent is current:
            break
        current = parent
    return False


def application_focus_blocks_shortcut() -> bool:
    """True when the widget that currently has focus blocks bare-key shortcuts."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return False
    return focus_blocks_shortcut(app.focusWidget())

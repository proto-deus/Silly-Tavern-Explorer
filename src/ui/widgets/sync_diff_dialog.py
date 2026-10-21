"""Read-only view of the field-level differences between two card copies.

The sync dialog classifies a card pair as "changed" from a content hash,
which drives the push/pull plan but says nothing about *what* differs.
This view shows the answer of :mod:`src.sync_diff`: for every compared
card, the fields that changed, the ST Explorer value and the SillyTavern
value.

The view is deliberately read-only - it explains a sync, it does not
perform one.  Both the card sync and the lorebook sync use it, because
:func:`src.sync_diff.diff_lorebooks` produces the same
:class:`~src.sync_diff.CardDiff` shape.
"""
from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.sync_diff import summarize_diffs

logger = logging.getLogger(__name__)

# A whole card description would make the table unreadable (and the item
# model huge), so long values are cut off with a marker.
MAX_VALUE_CHARS = 400


def shorten_value(value: str, limit: int = MAX_VALUE_CHARS) -> str:
    """Shorten *value* for display, marking how much was cut off.

    Pure function so the truncation is unit-testable without Qt.
    """
    text = value or ''
    if len(text) <= limit:
        return text
    return f'{text[:limit]}... (+{len(text) - limit} chars)'


class SyncDiffDialog(QDialog):
    """Dialog listing the per-field differences of the compared cards.

    Args:
        diffs: The :class:`~src.sync_diff.CardDiff` objects to show.
        title: The window title.
        parent: The parent widget.
    """

    def __init__(self, diffs, title: str = 'Changes', parent: QWidget | None = None):
        super().__init__(parent)
        self._diffs = list(diffs or [])
        self.setWindowTitle(title)
        self.setMinimumSize(900, 480)

        layout = QVBoxLayout(self)

        self._summary_label = QLabel(summarize_diffs(self._diffs))
        self._summary_label.setStyleSheet('color: #ccc;')
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(['Field', 'ST Explorer', 'SillyTavern'])
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setAlternatingRowColors(True)
        layout.addWidget(self._tree, 1)

        self._populate()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def summary(self) -> str:
        """The one-line summary of the shown differences."""
        return summarize_diffs(self._diffs)

    def _populate(self) -> None:
        """Fill the tree with one top-level item per compared card."""
        self._tree.clear()
        for diff in self._diffs:
            name = diff.card_name or (
                f'Card {diff.card_id}' if diff.card_id else 'Card'
            )
            top = QTreeWidgetItem([name, '', ''])
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            for field in diff.changed_fields:
                top.addChild(QTreeWidgetItem([
                    field.field,
                    shorten_value(field.local),
                    shorten_value(field.remote),
                ]))
            if diff.local_only:
                top.addChild(QTreeWidgetItem([
                    '(only in ST Explorer)', '(present)', '(missing)',
                ]))
            if diff.remote_only:
                top.addChild(QTreeWidgetItem([
                    '(only in SillyTavern)', '(missing)', '(present)',
                ]))
            self._tree.addTopLevelItem(top)
            top.setExpanded(True)


__all__ = ['MAX_VALUE_CHARS', 'SyncDiffDialog', 'shorten_value']

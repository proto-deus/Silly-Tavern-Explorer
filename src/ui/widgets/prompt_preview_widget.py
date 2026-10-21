"""A widget that renders a :class:`src.prompt_preview.PromptPreview`.

The widget is the "what will actually be sent" view of a chat request:

* a summary line with the token total against the context window,
* a context-pressure meter that turns amber above 70 % and red above 100 %,
* a tree of the prompt's blocks (label, role, token cost) - selecting one
  shows its text,
* the exact payload that would be posted, selectable as a whole.

It is used by the Test tab's Context Inspector, which shows it on its
"Prompt" tab.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

#: Row role used to carry the block text of a tree item.
BLOCK_TEXT_ROLE = int(Qt.ItemDataRole.UserRole) + 1

#: The pseudo-row that shows the whole payload.
PAYLOAD_LABEL = 'Payload (exactly what is sent)'

_NORMAL_COLOR = '#999'
_WARN_COLOR = '#e0a030'
_OVER_COLOR = '#ff6666'


def format_preview_summary(preview) -> str:
    """One-line summary of the preview's token usage."""
    limit = int(getattr(preview, 'context_limit', 0) or 0)
    used = int(getattr(preview, 'total_tokens', 0) or 0)
    if limit:
        text = f'{used:,} / {limit:,} tokens'
        if used > limit:
            text += ' - over the context window!'
        return text
    return f'{used:,} tokens'


def format_preview_text(preview) -> str:
    """Plain-text rendering of the preview (used for the Copy button)."""
    lines: list[str] = []
    for block in getattr(preview, 'blocks', []) or []:
        lines.append(f'== {block.label} [{block.role}] ({block.tokens:,} tokens) ==')
        if block.text:
            lines.append(block.text)
        lines.append('')
    lines.append('== Payload (exactly what is sent) ==')
    for message in getattr(preview, 'messages', []) or []:
        role = message.get('role', 'user')
        content = message.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(
                str(part.get('text', ''))
                for part in content
                if isinstance(part, dict) and part.get('type') == 'text'
            )
        lines.append(f'-- {role} --')
        lines.append(str(content))
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


class PromptPreviewWidget(QWidget):
    """Shows a prompt preview: blocks, token costs and the wire payload."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._summary = QLabel('')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._meter = QProgressBar()
        self._meter.setRange(0, 100)
        self._meter.setTextVisible(True)
        layout.addWidget(self._meter)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(['Block', 'Role', 'Tokens'])
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.currentItemChanged.connect(self._on_block_selected)
        layout.addWidget(self._tree, 1)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self._text, 1)

    # ---- content ----

    def set_preview(self, preview) -> None:
        """Show *preview* (a :class:`src.prompt_preview.PromptPreview`)."""
        limit = max(1, int(getattr(preview, 'context_limit', 1) or 1))
        used = int(getattr(preview, 'total_tokens', 0) or 0)
        self._summary.setText(format_preview_summary(preview))
        over = used > limit
        self._summary.setStyleSheet(
            f'color: {_OVER_COLOR if over else _NORMAL_COLOR};',
        )
        self._meter.setValue(min(100, int(used * 100 / limit)))
        self._meter.setFormat(f'{used:,} / {limit:,} tokens (%p%)')

        self._tree.clear()
        payload_item = QTreeWidgetItem([
            PAYLOAD_LABEL, 'payload', f'{used:,}',
        ])
        payload_item.setData(0, BLOCK_TEXT_ROLE, self._payload_text(preview))
        payload_item.setToolTip(0, PAYLOAD_LABEL)
        self._tree.addTopLevelItem(payload_item)

        for block in getattr(preview, 'blocks', []) or []:
            item = QTreeWidgetItem([
                block.label, block.role, f'{block.tokens:,}',
            ])
            item.setData(0, BLOCK_TEXT_ROLE, block.text or '')
            item.setToolTip(0, block.text or '')
            self._tree.addTopLevelItem(item)
        for col in range(3):
            self._tree.resizeColumnToContents(col)

        if self._tree.topLevelItemCount():
            self._tree.setCurrentItem(self._tree.topLevelItem(0))

    @staticmethod
    def _payload_text(preview) -> str:
        """The exact payload, rendered as text."""
        lines: list[str] = []
        for message in getattr(preview, 'messages', []) or []:
            role = message.get('role', 'user')
            content = message.get('content', '')
            if isinstance(content, list):
                content = '\n'.join(
                    str(part.get('text', ''))
                    for part in content
                    if isinstance(part, dict) and part.get('type') == 'text'
                )
            lines.append(f'--- {role} ---')
            lines.append(str(content))
            lines.append('')
        return '\n'.join(lines).rstrip()

    def _on_block_selected(self, current, _previous) -> None:
        if current is None:
            self._text.clear()
            return
        self._text.setPlainText(current.data(0, BLOCK_TEXT_ROLE) or '')

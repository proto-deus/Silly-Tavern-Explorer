from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.chat_sessions import normalize_author_note


class AuthorNoteEditor(QWidget):
    """Edit a chat's Author's Note (text, depth, injection role).

    Mirrors SillyTavern's Author's Note: a per-chat instruction injected as a
    message *depth* turns from the end of the conversation (4 by default),
    with the chosen role.  ``note()`` returns the normalised
    ``{'text', 'depth', 'role'}`` dict the session persists.
    """

    _ROLES = ('system', 'user', 'assistant')

    changed = pyqtSignal()

    def __init__(self, note: dict | None = None, parent: QWidget | None = None):
        super().__init__(parent)

        current = normalize_author_note(note)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(
            "Injected as a message near the end of the chat history "
            "(depth = how many messages stay below it)."
        ))

        self._text = QTextEdit()
        self._text.setAcceptRichText(False)
        self._text.setPlaceholderText(
            "e.g. Keep the pacing slow and the tone melancholic."
        )
        self._text.setPlainText(current['text'])
        layout.addWidget(self._text, 1)

        form = QFormLayout()
        self._depth = QSpinBox()
        self._depth.setRange(0, 99)
        self._depth.setValue(int(current['depth']))
        self._depth.setToolTip(
            'How many messages from the end of the history the note is '
            'placed before (0 = after everything, 4 = SillyTavern default).'
        )
        form.addRow('Depth:', self._depth)

        self._role = QComboBox()
        self._role.addItems(list(self._ROLES))
        self._role.setCurrentText(current['role'])
        self._role.setToolTip('API role the injected note carries.')
        form.addRow('Role:', self._role)
        layout.addLayout(form)

        self._text.textChanged.connect(self.changed)
        self._depth.valueChanged.connect(self.changed)
        self._role.currentTextChanged.connect(self.changed)

    def note(self) -> dict:
        """The edited note as a normalised ``{text, depth, role}`` dict."""
        return normalize_author_note({
            'text': self._text.toPlainText().strip(),
            'depth': self._depth.value(),
            'role': self._role.currentText(),
        })
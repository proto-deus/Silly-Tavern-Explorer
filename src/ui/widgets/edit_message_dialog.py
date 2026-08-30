from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)


class TextEditDialog(QDialog):
    """A simple multiline text editor dialog used for messages and chat memory."""

    def __init__(
        self,
        text: str = '',
        title: str = 'Edit',
        placeholder: str = '',
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(520, 360)

        layout = QVBoxLayout(self)
        self._edit = QPlainTextEdit(text or '')
        self._edit.setPlaceholderText(placeholder)
        layout.addWidget(self._edit, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def text(self) -> str:
        return self._edit.toPlainText()

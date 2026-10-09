from __future__ import annotations

import uuid

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class PersonaDialog(QDialog):
    """Manage named user personas and pick the one active for the chat.

    SillyTavern keeps multiple personas (name + description per user) with a
    per-chat selection; this dialog provides the same for the Test tab.
    ``personas()`` returns the edited list and ``selected_id()`` the active
    persona's id.
    """

    def __init__(
        self,
        personas: list[dict] | None = None,
        active_id: str = '',
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle('User Personas')
        self.setMinimumSize(560, 380)

        self._personas: list[dict] = [
            {
                'id': str(p.get('id') or uuid.uuid4().hex),
                'name': str(p.get('name') or 'Persona'),
                'text': str(p.get('text') or ''),
            }
            for p in (personas or [])
        ]
        if not self._personas:
            self._personas.append(
                {'id': uuid.uuid4().hex, 'name': 'Default', 'text': ''},
            )
        self._loading = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            'The selected persona is injected as the [User persona] block '
            '({{user}}) for this chat.'
        ))

        row = QHBoxLayout()
        left = QVBoxLayout()
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_row_changed)
        left.addWidget(self._list, 1)
        btn_row = QHBoxLayout()
        new_btn = QPushButton('New')
        new_btn.clicked.connect(self._on_new)
        btn_row.addWidget(new_btn)
        delete_btn = QPushButton('Delete')
        delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(delete_btn)
        btn_row.addStretch()
        left.addLayout(btn_row)
        row.addLayout(left, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel('Name:'))
        self._name = QLineEdit()
        self._name.textEdited.connect(self._sync_current)
        right.addWidget(self._name)
        right.addWidget(QLabel('Description (who {{user}} is):'))
        self._text = QTextEdit()
        self._text.setAcceptRichText(False)
        self._text.setPlaceholderText('Appearance, personality, role...')
        self._text.textChanged.connect(self._sync_current)
        right.addWidget(self._text, 1)
        row.addLayout(right, 2)
        layout.addLayout(row, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        select = 0
        for i, p in enumerate(self._personas):
            self._list.addItem(QListWidgetItem(p['name']))
            if active_id and p['id'] == active_id:
                select = i
        self._list.setCurrentRow(select)

    # ---- editing ----

    def _on_row_changed(self, row: int) -> None:
        if self._loading:
            return
        if 0 <= row < len(self._personas):
            p = self._personas[row]
            self._loading = True
            self._name.setText(p['name'])
            self._text.setPlainText(p['text'])
            self._loading = False

    def _sync_current(self, *_args) -> None:
        if self._loading:
            return
        row = self._list.currentRow()
        if 0 <= row < len(self._personas):
            self._personas[row]['name'] = self._name.text().strip() or 'Persona'
            self._personas[row]['text'] = self._text.toPlainText()
            item = self._list.item(row)
            if item is not None:
                item.setText(self._personas[row]['name'])

    def _on_new(self) -> None:
        self._sync_current()
        self._personas.append(
            {'id': uuid.uuid4().hex, 'name': f'Persona {len(self._personas) + 1}', 'text': ''},
        )
        self._list.addItem(QListWidgetItem(self._personas[-1]['name']))
        self._list.setCurrentRow(len(self._personas) - 1)

    def _on_delete(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._personas):
            return
        if len(self._personas) <= 1:
            QMessageBox.information(
                self, 'Personas', 'At least one persona must remain.',
            )
            return
        if QMessageBox.question(
            self, 'Delete Persona', f'Delete persona "{self._personas[row]["name"]}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        del self._personas[row]
        self._list.takeItem(row)
        self._list.setCurrentRow(min(row, len(self._personas) - 1))

    # ---- results ----

    def personas(self) -> list[dict]:
        """The edited persona list (includes the current field edits)."""
        self._sync_current()
        return [dict(p) for p in self._personas]

    def selected_id(self) -> str:
        """Id of the highlighted persona (the one to activate)."""
        row = self._list.currentRow()
        if 0 <= row < len(self._personas):
            return self._personas[row]['id']
        return self._personas[0]['id'] if self._personas else ''

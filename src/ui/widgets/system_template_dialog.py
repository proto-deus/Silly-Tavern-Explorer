"""Dialog for managing system prompt templates (the chat's main prompts)."""

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
    QVBoxLayout,
    QWidget,
)

from src.prompt_templates import (
    CUSTOM_TEMPLATE_ID,
    SystemPromptTemplate,
    all_templates,
    save_user_templates,
)
from src.ui.spell_check import SpellCheckPlainTextEdit


class SystemTemplateDialog(QDialog):
    """Manage the system prompt templates offered by the Test tab.

    Lists the "Custom" prompt (edited in Settings > Prompts > Chat), the
    built-in SillyTavern-style presets, and user-defined templates.  Built-ins
    are read-only - Duplicate creates an editable copy.  User templates are
    persisted on accept.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('System Prompts')
        self.setMinimumSize(720, 460)

        self._templates: list[SystemPromptTemplate] = all_templates()
        self._loading = False

        layout = QVBoxLayout(self)
        hint = QLabel(
            'The selected system prompt is the chat\'s main prompt (first '
            'system block). The character\'s own system prompt is appended '
            'after it; this prompt\'s post-history instructions follow the '
            'card\'s. Macros like {{user}} / {{char}} are substituted at '
            'send time.'
        )
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #aaa;')
        layout.addWidget(hint)

        row = QHBoxLayout()
        left = QVBoxLayout()
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_row_changed)
        left.addWidget(self._list, 1)
        btn_row = QHBoxLayout()
        self._new_btn = QPushButton('New')
        self._new_btn.clicked.connect(self._on_new)
        btn_row.addWidget(self._new_btn)
        self._dup_btn = QPushButton('Duplicate')
        self._dup_btn.setToolTip(
            'Create an editable copy of the selected template.'
        )
        self._dup_btn.clicked.connect(self._on_duplicate)
        btn_row.addWidget(self._dup_btn)
        self._delete_btn = QPushButton('Delete')
        self._delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self._delete_btn)
        btn_row.addStretch()
        left.addLayout(btn_row)
        row.addLayout(left, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel('Name:'))
        self._name = QLineEdit()
        self._name.textEdited.connect(self._sync_current)
        right.addWidget(self._name)
        right.addWidget(QLabel('System prompt:'))
        self._content = SpellCheckPlainTextEdit()
        self._content.setPlaceholderText(
            "e.g. You're {{char}} in this fictional roleplay with {{user}}."
        )
        self._content.textChanged.connect(self._sync_current)
        right.addWidget(self._content, 1)
        right.addWidget(QLabel('Post-history instructions (optional):'))
        self._post_history = SpellCheckPlainTextEdit()
        self._post_history.setPlaceholderText(
            'Sent after the card\'s own post-history instructions.'
        )
        self._post_history.setMaximumHeight(80)
        self._post_history.textChanged.connect(self._sync_current)
        right.addWidget(self._post_history)
        self._note = QLabel('')
        self._note.setWordWrap(True)
        self._note.setStyleSheet('color: #888888;')
        right.addWidget(self._note)
        row.addLayout(right, 2)
        layout.addLayout(row, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for tpl in self._templates:
            self._list.addItem(QListWidgetItem(self._label_for(tpl)))
        self._list.setCurrentRow(0)

    # ---- list helpers ----

    @staticmethod
    def _label_for(tpl: SystemPromptTemplate) -> str:
        if tpl.builtin and tpl.id != CUSTOM_TEMPLATE_ID:
            return f'{tpl.name}  (built-in)'
        return tpl.name

    @staticmethod
    def _is_editable(tpl: SystemPromptTemplate) -> bool:
        return not tpl.builtin

    def _on_row_changed(self, row: int) -> None:
        if self._loading:
            return
        if not (0 <= row < len(self._templates)):
            return
        tpl = self._templates[row]
        self._loading = True
        self._name.setText(tpl.name)
        self._content.setPlainText(tpl.content)
        self._post_history.setPlainText(tpl.post_history)
        self._loading = False

        editable = self._is_editable(tpl)
        self._name.setReadOnly(not editable)
        self._content.setReadOnly(not editable)
        self._post_history.setReadOnly(not editable)
        self._delete_btn.setEnabled(editable)
        if tpl.id == CUSTOM_TEMPLATE_ID:
            self._note.setText(
                'The "Custom" system prompt is edited in Settings > Prompts '
                '("Chat"), shared with the other AI prompts.'
            )
        elif tpl.builtin:
            self._note.setText(
                'Built-in system prompts are read-only. Use Duplicate to '
                'create an editable copy.'
            )
        else:
            self._note.setText('')

    def _sync_current(self, *_args) -> None:
        if self._loading:
            return
        row = self._list.currentRow()
        if not (0 <= row < len(self._templates)):
            return
        tpl = self._templates[row]
        if not self._is_editable(tpl):
            return
        self._templates[row] = SystemPromptTemplate(
            id=tpl.id,
            name=self._name.text().strip() or 'Template',
            content=self._content.toPlainText(),
            post_history=self._post_history.toPlainText(),
            builtin=False,
        )
        self._loading = True
        self._list.item(row).setText(self._label_for(self._templates[row]))
        self._loading = False

    def _add_user_template(self, tpl: SystemPromptTemplate, select: bool = True) -> None:
        self._templates.append(tpl)
        self._list.addItem(QListWidgetItem(self._label_for(tpl)))
        if select:
            self._list.setCurrentRow(len(self._templates) - 1)

    def _on_new(self) -> None:
        self._add_user_template(SystemPromptTemplate(
            id=f'user:{uuid.uuid4().hex}',
            name='New Template',
            content='',
            post_history='',
            builtin=False,
        ))

    def _on_duplicate(self) -> None:
        row = self._list.currentRow()
        if not (0 <= row < len(self._templates)):
            return
        src = self._templates[row]
        name = src.name
        if name.endswith(' (built-in)'):
            name = name[: -len(' (built-in)')]
        self._add_user_template(SystemPromptTemplate(
            id=f'user:{uuid.uuid4().hex}',
            name=f'Copy of {name}',
            content=src.content,
            post_history=src.post_history,
            builtin=False,
        ))

    def _on_delete(self) -> None:
        row = self._list.currentRow()
        if not (0 <= row < len(self._templates)):
            return
        tpl = self._templates[row]
        if not self._is_editable(tpl):
            return
        answer = QMessageBox.question(
            self, 'Delete Template',
            f'Delete the template "{tpl.name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._templates.pop(row)
        self._list.takeItem(row)
        if self._templates:
            self._list.setCurrentRow(min(row, len(self._templates) - 1))

    def _save(self) -> None:
        save_user_templates([t for t in self._templates if not t.builtin])
        self.accept()

"""Dialog for managing context templates (chat-context framing presets)."""

from __future__ import annotations

import uuid

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
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

from src.context_templates import (
    DEFAULT_CONTEXT_TEMPLATE_ID,
    NAMES_BEHAVIOR_CHOICES,
    ContextTemplate,
    all_templates,
    save_user_templates,
)

_NAMES_BEHAVIOR_LABELS = (
    ('None', 'none'),
    ("Character's name", 'char'),
    ("User's name", 'user'),
    ('Both', 'both'),
)


class ContextTemplateDialog(QDialog):
    """Manage the context templates offered by the Test tab.

    Lists the "Default" template plus the built-in presets and user-defined
    templates.  Built-ins are read-only - Duplicate creates an editable copy.
    User templates are persisted on accept.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Context Templates')
        self.setMinimumSize(860, 560)

        self._templates: list[ContextTemplate] = all_templates()
        self._loading = False

        layout = QVBoxLayout(self)
        hint = QLabel(
            'Context templates control how the chat context is framed and '
            'named: system block labels (e.g. "[Description]" vs "### '
            'Description"), speaker names on history messages, example-'
            'dialogue formatting, and the wrapping of the trailing blocks. '
            'Format fields use {title} and {body}; {{user}} / {{char}} in '
            'example prefixes are substituted at send time.'
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
        self._name.textChanged.connect(self._sync_current)
        right.addWidget(self._name)

        blocks = QGroupBox('System blocks')
        blocks_form = QFormLayout(blocks)
        self._block_format = self._line('[{title}]\\n{body}')
        blocks_form.addRow('Block format:', self._block_format)
        self._main_format = self._line('{body}')
        blocks_form.addRow('System prompt format:', self._main_format)
        self._block_separator = self._line('\\n\\n')
        blocks_form.addRow('Block separator:', self._block_separator)
        right.addWidget(blocks)

        names = QGroupBox('Names')
        names_form = QFormLayout(names)
        self._names_behavior = QComboBox()
        for label, _value in _NAMES_BEHAVIOR_LABELS:
            self._names_behavior.addItem(label)
        self._names_behavior.currentIndexChanged.connect(self._sync_current)
        self._names_behavior.setToolTip(
            'Prepend the speaker name to chat history messages '
            '(SillyTavern\'s "names behavior"); useful for roleplay and '
            'group chats.'
        )
        names_form.addRow('Add names to messages:', self._names_behavior)
        self._name_format = self._line('{name}: ')
        names_form.addRow('Name format:', self._name_format)
        right.addWidget(names)

        examples = QGroupBox('Example dialogue')
        examples_form = QFormLayout(examples)
        self._example_user_prefix = self._line('{{user}}: ')
        examples_form.addRow('User line prefix:', self._example_user_prefix)
        self._example_char_prefix = self._line('{{char}}: ')
        examples_form.addRow('Character line prefix:', self._example_char_prefix)
        self._example_separator = self._line('<START>')
        examples_form.addRow('Block separator:', self._example_separator)
        right.addWidget(examples)

        trailing = QGroupBox('Trailing blocks')
        trailing_form = QFormLayout(trailing)
        self._post_history_format = self._line('{body}')
        trailing_form.addRow('Post-history format:', self._post_history_format)
        self._jailbreak_format = self._line('{body}')
        trailing_form.addRow('Jailbreak format:', self._jailbreak_format)
        self._conversation_start_header = self._line('[Conversation start]')
        trailing_form.addRow('Conversation start header:', self._conversation_start_header)
        right.addWidget(trailing)

        self._note = QLabel('')
        self._note.setWordWrap(True)
        self._note.setStyleSheet('color: #888888;')
        right.addWidget(self._note)
        right.addStretch()
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

    def _line(self, default: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(default)
        edit.textChanged.connect(self._sync_current)
        return edit

    # ---- list helpers ----

    @staticmethod
    def _label_for(tpl: ContextTemplate) -> str:
        if tpl.builtin and tpl.id != DEFAULT_CONTEXT_TEMPLATE_ID:
            return f'{tpl.name}  (built-in)'
        return tpl.name

    @staticmethod
    def _is_editable(tpl: ContextTemplate) -> bool:
        return not tpl.builtin

    def _on_row_changed(self, row: int) -> None:
        if self._loading:
            return
        if not (0 <= row < len(self._templates)):
            return
        tpl = self._templates[row]
        self._loading = True
        self._name.setText(tpl.name)
        self._block_format.setText(tpl.block_format)
        self._main_format.setText(tpl.main_format)
        self._block_separator.setText(tpl.block_separator.replace('\n', '\\n'))
        self._names_behavior.setCurrentIndex(
            max(0, NAMES_BEHAVIOR_CHOICES.index(tpl.names_behavior))
            if tpl.names_behavior in NAMES_BEHAVIOR_CHOICES else 0,
        )
        self._name_format.setText(tpl.name_format)
        self._example_user_prefix.setText(tpl.example_user_prefix)
        self._example_char_prefix.setText(tpl.example_char_prefix)
        self._example_separator.setText(tpl.example_separator)
        self._post_history_format.setText(tpl.post_history_format)
        self._jailbreak_format.setText(tpl.jailbreak_format)
        self._conversation_start_header.setText(tpl.conversation_start_header)
        self._loading = False

        editable = self._is_editable(tpl)
        for widget in (
            self._name, self._block_format, self._main_format,
            self._block_separator, self._name_format,
            self._example_user_prefix, self._example_char_prefix,
            self._example_separator, self._post_history_format,
            self._jailbreak_format, self._conversation_start_header,
        ):
            widget.setReadOnly(not editable)
        self._names_behavior.setEnabled(editable)
        self._delete_btn.setEnabled(editable)
        if tpl.id == DEFAULT_CONTEXT_TEMPLATE_ID:
            self._note.setText(
                'The "Default" template keeps the classic output: [Title] '
                'blocks, no speaker names, and {{user}}: / {{char}}: example '
                'lines. It is always available and cannot be edited.'
            )
        elif tpl.builtin:
            self._note.setText(
                'Built-in context templates are read-only. Use Duplicate to '
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
        self._templates[row] = ContextTemplate(
            id=tpl.id,
            name=self._name.text().strip() or 'Context Template',
            builtin=False,
            block_format=self._block_format.text() or '[{title}]\n{body}',
            main_format=self._main_format.text() or '{body}',
            block_separator=self._unescape(self._block_separator.text()) or '\n\n',
            names_behavior=NAMES_BEHAVIOR_CHOICES[
                self._names_behavior.currentIndex()
            ] if 0 <= self._names_behavior.currentIndex() < len(NAMES_BEHAVIOR_CHOICES) else 'none',
            name_format=self._name_format.text() or '{name}: ',
            example_user_prefix=self._example_user_prefix.text(),
            example_char_prefix=self._example_char_prefix.text(),
            example_separator=self._example_separator.text() or '<START>',
            post_history_format=self._post_history_format.text() or '{body}',
            jailbreak_format=self._jailbreak_format.text() or '{body}',
            conversation_start_header=(
                self._conversation_start_header.text() or '[Conversation start]'
            ),
        )
        self._loading = True
        self._list.item(row).setText(self._label_for(self._templates[row]))
        self._loading = False

    @staticmethod
    def _unescape(text: str) -> str:
        return (text or '').replace('\\n', '\n').replace('\\t', '\t')

    def _add_user_template(self, tpl: ContextTemplate, select: bool = True) -> None:
        self._templates.append(tpl)
        self._list.addItem(QListWidgetItem(self._label_for(tpl)))
        if select:
            self._list.setCurrentRow(len(self._templates) - 1)

    def _on_new(self) -> None:
        self._add_user_template(ContextTemplate(
            id=f'user:{uuid.uuid4().hex}',
            name='New Context Template',
            builtin=False,
        ))

    def _on_duplicate(self) -> None:
        row = self._list.currentRow()
        if not (0 <= row < len(self._templates)):
            return
        src = self._templates[row]
        data = src.to_dict()
        data['id'] = f'user:{uuid.uuid4().hex}'
        data['name'] = f'Copy of {src.name}'
        self._add_user_template(ContextTemplate.from_dict(data))

    def _on_delete(self) -> None:
        row = self._list.currentRow()
        if not (0 <= row < len(self._templates)):
            return
        tpl = self._templates[row]
        if not self._is_editable(tpl):
            return
        answer = QMessageBox.question(
            self, 'Delete Context Template',
            f'Delete the context template "{tpl.name}"?',
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
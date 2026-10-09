from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.card_models import BookEntry, parse_character_book


def parse_secondary_keys(text: str) -> list[str]:
    """Parse a comma-separated secondary-keys line into a clean list.

    Pure function so parsing can be unit-tested without Qt.
    """
    text = (text or '').strip()
    if not text:
        return []
    return [k.strip() for k in text.split(',') if k.strip()]


def compose_entry_extensions(
    original_extensions: dict | None,
    edited_extensions: dict | None,
    secondary_keys: list[str] | None = None,
) -> dict:
    """Merge the entry editor's advanced-section inputs into one dict.

    *edited_extensions* is authoritative when given (the dialog always
    seeds it from *original_extensions* minus ``keysecondary``); otherwise
    the original dict is carried over verbatim.  When *secondary_keys* is
    not ``None`` it replaces any stored ``keysecondary`` list — an empty
    list removes the field entirely.  Pure function so composition can be
    unit-tested without Qt.
    """
    source = edited_extensions if edited_extensions is not None else original_extensions
    ext = dict(source) if isinstance(source, dict) else {}
    if secondary_keys is not None:
        ext.pop('keysecondary', None)
        clean = [k.strip() for k in secondary_keys if isinstance(k, str) and k.strip()]
        if clean:
            ext['keysecondary'] = clean
    return ext


def merge_edited_entry(
    original: BookEntry,
    *,
    name: str,
    keys: list[str],
    content: str,
    position: str,
    insertion_order: int,
    depth: int,
    enabled: bool,
    case_sensitive: bool,
    match_whole_words: bool,
    extensions: dict | None = None,
) -> BookEntry:
    """Build the saved :class:`BookEntry` from editor fields over *original*.

    By default the unmodeled ``extensions`` dict (ST-native fields such as
    ``uid``, ``probability``, ``selective``, ``sticky``, ...) is carried
    verbatim from *original*; passing *extensions* explicitly overrides it
    (an empty dict clears it).  Pure function so preservation can be
    unit-tested without Qt.
    """
    if extensions is not None:
        final_extensions = dict(extensions)
    else:
        final_extensions = dict(original.extensions) if original.extensions else {}
    return BookEntry(
        name=name,
        keys=keys,
        content=content,
        extensions=final_extensions,
        enabled=enabled,
        insertion_order=insertion_order,
        case_sensitive=case_sensitive,
        match_whole_words=match_whole_words,
        depth=depth,
        position=position,
    )


class _BookEntryEditDialog(QDialog):
    """Dialog for editing a single lorebook entry."""

    def __init__(self, entry: BookEntry, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Edit Entry')
        self.setMinimumWidth(450)
        self.setMinimumHeight(400)
        self._entry = entry

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._name_edit = QLineEdit(entry.name)
        form.addRow('Name:', self._name_edit)

        self._keys_edit = QLineEdit(', '.join(entry.keys))
        self._keys_edit.setPlaceholderText('Comma-separated keys...')
        form.addRow('Keys:', self._keys_edit)

        form.addRow(QLabel('Content:'))
        self._content_edit = QPlainTextEdit(entry.content)
        form.addRow(self._content_edit)

        self._position_edit = QLineEdit(entry.position)
        self._position_edit.setPlaceholderText('before_char / after_char')
        form.addRow('Position:', self._position_edit)

        self._order_spin = QSpinBox()
        # Wide range: lorebooks' own Add derives orders as len(entries)*100
        # (>= 1100 for 11+ entries) and imported ST books can carry order
        # values above 1000; a narrower range silently rewrote them to 1000
        # on save, changing prompt-insertion priority.
        self._order_spin.setRange(-1000000, 1000000)
        self._order_spin.setValue(entry.insertion_order)
        form.addRow('Insertion Order:', self._order_spin)

        self._depth_spin = QSpinBox()
        self._depth_spin.setRange(0, 100)
        self._depth_spin.setValue(entry.depth)
        form.addRow('Depth:', self._depth_spin)

        self._enabled_cb = QCheckBox('Enabled')
        self._enabled_cb.setChecked(entry.enabled)
        form.addRow(self._enabled_cb)

        self._case_cb = QCheckBox('Case Sensitive')
        self._case_cb.setChecked(entry.case_sensitive)
        form.addRow(self._case_cb)

        self._whole_cb = QCheckBox('Match Whole Words')
        self._whole_cb.setChecked(entry.match_whole_words)
        form.addRow(self._whole_cb)

        layout.addLayout(form)

        # Advanced: secondary trigger keys + unmodeled extension fields
        # (uid, probability, selective, sticky, ...).  keysecondary is
        # managed by its own field and hidden from the generic editor.
        advanced = QWidget()
        adv_form = QFormLayout(advanced)
        adv_form.setContentsMargins(0, 0, 0, 0)

        self._secondary_edit = QLineEdit(
            ', '.join(str(k) for k in entry.extensions.get('keysecondary') or []),
        )
        self._secondary_edit.setPlaceholderText('Comma-separated secondary keys (optional)')
        adv_form.addRow('Secondary Keys:', self._secondary_edit)

        self._extensions = {
            k: v for k, v in (entry.extensions or {}).items() if k != 'keysecondary'
        }
        ext_row = QHBoxLayout()
        self._ext_count_label = QLabel(self._extensions_label())
        ext_row.addWidget(self._ext_count_label)
        ext_row.addStretch()
        edit_ext_btn = QPushButton('Edit Fields...')
        edit_ext_btn.clicked.connect(self._on_edit_extensions)
        ext_row.addWidget(edit_ext_btn)
        adv_form.addRow('Extensions:', ext_row)

        from src.ui.widgets.collapsible_section import CollapsibleSection
        self._advanced_section = CollapsibleSection(
            'Advanced', advanced, show_token_count=False,
        )
        self._advanced_section.set_expanded(False)
        layout.addWidget(self._advanced_section)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _extensions_label(self) -> str:
        n = len(self._extensions)
        return f'{n} field{"s" if n != 1 else ""}' if n else 'none'

    def _on_edit_extensions(self) -> None:
        from src.ui.widgets.extensions_editor import ExtensionsEditor
        result = ExtensionsEditor.edit(
            dict(self._extensions), self, reserved_keys=set(),
        )
        if result is not None:
            self._extensions = dict(result)
            self._ext_count_label.setText(self._extensions_label())

    def get_entry(self) -> BookEntry:
        keys_text = self._keys_edit.text().strip()
        keys = [k.strip() for k in keys_text.split(',') if k.strip()] if keys_text else []
        # The Advanced section is authoritative for extensions: the edited
        # fields plus whatever secondary keys are currently in the line edit.
        composed_extensions = compose_entry_extensions(
            self._entry.extensions,
            self._extensions,
            parse_secondary_keys(self._secondary_edit.text()),
        )
        # merge_edited_entry preserves any keys not touched by this dialog.
        return merge_edited_entry(
            self._entry,
            name=self._name_edit.text().strip(),
            keys=keys,
            content=self._content_edit.toPlainText(),
            position=self._position_edit.text().strip() or 'before_char',
            insertion_order=self._order_spin.value(),
            depth=self._depth_spin.value(),
            enabled=self._enabled_cb.isChecked(),
            case_sensitive=self._case_cb.isChecked(),
            match_whole_words=self._whole_cb.isChecked(),
            extensions=composed_extensions,
        )


class CharacterBookEditor(QDialog):
    """Dialog for editing a character book (lorebook)."""

    def __init__(self, book_dict: dict | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Character Book Editor')
        self.setMinimumWidth(500)
        self.setMinimumHeight(500)

        self._book = parse_character_book(book_dict)

        layout = QVBoxLayout(self)

        book_form = QFormLayout()
        self._book_name_edit = QLineEdit(self._book.name)
        book_form.addRow('Book Name:', self._book_name_edit)

        self._book_desc_edit = QLineEdit(self._book.description)
        book_form.addRow('Description:', self._book_desc_edit)

        depth_row = QHBoxLayout()
        self._scan_depth_spin = QSpinBox()
        self._scan_depth_spin.setRange(0, 9999)
        self._scan_depth_spin.setSpecialValueText('None')
        if self._book.scan_depth is not None:
            self._scan_depth_spin.setValue(self._book.scan_depth)
        else:
            self._scan_depth_spin.setValue(0)
        depth_row.addWidget(self._scan_depth_spin)
        book_form.addRow('Scan Depth:', depth_row)

        budget_row = QHBoxLayout()
        self._token_budget_spin = QSpinBox()
        self._token_budget_spin.setRange(0, 999999)
        self._token_budget_spin.setSpecialValueText('None')
        if self._book.token_budget is not None:
            self._token_budget_spin.setValue(self._book.token_budget)
        else:
            self._token_budget_spin.setValue(0)
        budget_row.addWidget(self._token_budget_spin)
        book_form.addRow('Token Budget:', budget_row)

        self._recursive_cb = QCheckBox('Recursive Scanning')
        self._recursive_cb.setChecked(self._book.recursive_scanning)
        book_form.addRow(self._recursive_cb)

        book_ext_row = QHBoxLayout()
        self._book_ext_label = QLabel(self._book_extensions_label())
        book_ext_row.addWidget(self._book_ext_label)
        book_ext_row.addStretch()
        book_ext_btn = QPushButton('Edit Fields...')
        book_ext_btn.clicked.connect(self._on_edit_book_extensions)
        book_ext_row.addWidget(book_ext_btn)
        book_form.addRow('Extensions:', book_ext_row)

        layout.addLayout(book_form)

        layout.addWidget(QLabel('Entries:'))
        self._entry_list = QListWidget()
        self._entry_list.setMaximumHeight(200)
        self._entry_list.setStyleSheet('')
        self._entry_list.itemDoubleClicked.connect(self._edit_entry)
        layout.addWidget(self._entry_list)

        btn_row = QHBoxLayout()
        add_btn = QPushButton('Add')
        add_btn.clicked.connect(self._add_entry)
        btn_row.addWidget(add_btn)
        edit_btn = QPushButton('Edit')
        edit_btn.clicked.connect(self._edit_entry)
        btn_row.addWidget(edit_btn)
        remove_btn = QPushButton('Remove')
        remove_btn.clicked.connect(self._remove_entry)
        btn_row.addWidget(remove_btn)
        up_btn = QPushButton('Move Up')
        up_btn.clicked.connect(self._move_up)
        btn_row.addWidget(up_btn)
        down_btn = QPushButton('Move Down')
        down_btn.clicked.connect(self._move_down)
        btn_row.addWidget(down_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_entry_list()

    def _book_extensions_label(self) -> str:
        n = len(self._book.extensions or {})
        return f'{n} field{"s" if n != 1 else ""}' if n else 'none'

    def _on_edit_book_extensions(self) -> None:
        from src.ui.widgets.extensions_editor import ExtensionsEditor
        result = ExtensionsEditor.edit(
            dict(self._book.extensions or {}), self, reserved_keys=set(),
        )
        if result is not None:
            self._book.extensions = dict(result)
            self._book_ext_label.setText(self._book_extensions_label())

    def _refresh_entry_list(self) -> None:
        self._entry_list.clear()
        for i, entry in enumerate(self._book.entries):
            label = entry.name or f'Entry {i + 1}'
            key_count = len(entry.keys)
            display = f'{label}  ({key_count} key{"s" if key_count != 1 else ""})'
            if not entry.enabled:
                display += '  [disabled]'
            item = QListWidgetItem(display)
            self._entry_list.addItem(item)

    def _add_entry(self) -> None:
        entry = BookEntry()
        dlg = _BookEntryEditDialog(entry, self)
        try:
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._book.entries.append(dlg.get_entry())
                self._refresh_entry_list()
                self._entry_list.setCurrentRow(len(self._book.entries) - 1)
        finally:
            dlg.setParent(None)
            dlg.deleteLater()

    def _edit_entry(self, *args) -> None:
        row = self._entry_list.currentRow()
        if row < 0 or row >= len(self._book.entries):
            return
        dlg = _BookEntryEditDialog(self._book.entries[row], self)
        try:
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._book.entries[row] = dlg.get_entry()
                self._refresh_entry_list()
        finally:
            dlg.setParent(None)
            dlg.deleteLater()

    def _remove_entry(self) -> None:
        row = self._entry_list.currentRow()
        if 0 <= row < len(self._book.entries):
            self._book.entries.pop(row)
            self._refresh_entry_list()

    def _move_up(self) -> None:
        row = self._entry_list.currentRow()
        if row > 0 and row < len(self._book.entries):
            self._book.entries[row], self._book.entries[row - 1] = (
                self._book.entries[row - 1], self._book.entries[row]
            )
            self._refresh_entry_list()
            self._entry_list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self._entry_list.currentRow()
        if 0 <= row < len(self._book.entries) - 1:
            self._book.entries[row], self._book.entries[row + 1] = (
                self._book.entries[row + 1], self._book.entries[row]
            )
            self._refresh_entry_list()
            self._entry_list.setCurrentRow(row + 1)

    def get_book_dict(self) -> dict:
        """Return the edited character book as a serializable dict."""
        self._book.name = self._book_name_edit.text().strip()
        self._book.description = self._book_desc_edit.text().strip()
        sd = self._scan_depth_spin.value()
        self._book.scan_depth = sd if sd > 0 else None
        tb = self._token_budget_spin.value()
        self._book.token_budget = tb if tb > 0 else None
        self._book.recursive_scanning = self._recursive_cb.isChecked()
        return self._book.to_dict()

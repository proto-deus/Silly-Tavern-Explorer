from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ai_client import AIClient, preset_from_saved
from src.card_models import BookEntry, CharacterBook, CharacterCard
from src.card_parser import read_card_data
from src.database import LibraryDatabase
from src import lorebook_store
from src.settings_manager import (
    load_api_settings,
    load_lorebook_use_card_context,
    save_lorebook_use_card_context,
)
from src.token_counter import count_tokens
from src.ui.widgets.character_book_editor import _BookEntryEditDialog

logger = logging.getLogger(__name__)


class _LorebookWorker(QThread):
    """Streams a lorebook generation request; ``mode`` picks the prompt pair."""

    completed = pyqtSignal(str)
    chunk = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, client: AIClient, mode: str, params: dict, parent=None):
        super().__init__(parent)
        self.client = client
        self.mode = mode
        self.params = params
        self._cancel = False

    def cancel(self):
        self._cancel = True
        self.client.cancel()

    def run(self):
        try:
            if self._cancel:
                return
            if self.mode == 'book':
                system, user = _build_book_prompts(self.params)
            else:
                system, user = _build_entry_prompts(self.params)
            parts: list[str] = []
            for piece in self.client.generate(system, user, stream=True):
                if self._cancel:
                    return
                parts.append(piece)
                self.chunk.emit(piece)
            if not self._cancel:
                self.completed.emit(''.join(parts))
        except Exception as e:
            if not self._cancel:
                self.error.emit(f"{e}\n{traceback.format_exc()}")


def _build_book_prompts(params: dict) -> tuple[str, str]:
    from src.ai_prompts import build_lorebook_prompts
    return build_lorebook_prompts(
        params.get('concept', ''),
        card=params.get('card'),
        count=params.get('count', ''),
        existing_summary=params.get('existing_summary', ''),
        length=params.get('length', ''),
        extra=params.get('extra', ''),
    )


def _build_entry_prompts(params: dict) -> tuple[str, str]:
    from src.ai_prompts import build_lorebook_entry_prompts
    return build_lorebook_entry_prompts(
        params.get('entry_name', ''),
        params.get('keys') or [],
        card=params.get('card'),
        book_description=params.get('book_description', ''),
        other_entries=params.get('other_entries', ''),
        length=params.get('length', ''),
        extra=params.get('extra', ''),
    )


def summarize_book_for_ai(book: CharacterBook, max_content_chars: int = 160) -> str:
    """Compact bullet summary of a book's entries for prompt context."""
    lines: list[str] = []
    for i, e in enumerate(book.entries, 1):
        keys = ', '.join(e.keys)
        content = (e.content or '').strip()
        if len(content) > max_content_chars:
            content = content[:max_content_chars].rstrip() + '...'
        key_part = f" [{keys}]" if keys else ''
        lines.append(f"{i}. {e.name or '(untitled)'}{key_part}: {content}")
    return '\n'.join(lines)


def format_book_stats(book: CharacterBook) -> str:
    """'N entries · M tokens' label text (pure, unit-testable)."""
    tokens = sum(count_tokens(e.content or '') for e in book.entries)
    n = len(book.entries)
    return f'{n} entr{"y" if n == 1 else "ies"} · {tokens:,} tokens'


def format_card_context_label(name: str | None, use_context: bool) -> str:
    """Header label for the selected-card context (pure, unit-testable).

    Empty when no card is selected; the name gains a "(not used)" suffix
    when a card is selected but the context toggle is off.
    """
    if not name:
        return ''
    if use_context:
        return f'Card context: {name}'
    return f'Card context: {name} (not used)'


class LorebooksTab(QWidget):
    """Create, edit, import/export, and AI-generate standalone lorebooks."""

    status_message = pyqtSignal(str, int)
    settings_requested = pyqtSignal()
    lorebooks_changed = pyqtSignal()

    AUTOSAVE_MS = 600

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._api_preset = preset_from_saved(load_api_settings())
        self._worker: _LorebookWorker | None = None
        self._book: CharacterBook | None = None
        self._filename: str | None = None
        self._loading = False
        self._pending_mode: str | None = None
        self._pending_text: str | None = None
        self._pending_filename: str | None = None
        self._selected_card_id: int | None = None
        self._use_card_context = load_lorebook_use_card_context()

        self._build_ui()

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(self.AUTOSAVE_MS)
        self._autosave_timer.timeout.connect(self._persist_now)

        self.refresh_books(select_first=True)

    # ---- UI construction ----

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        layout.addWidget(self._build_left_panel())

        right = QVBoxLayout()
        header_row = QHBoxLayout()
        title = QLabel('Lorebooks')
        title.setStyleSheet('font-weight: bold; color: #e0e0e0;')
        header_row.addWidget(title)
        header_row.addStretch()
        self._use_context_cb = QCheckBox('Use card context')
        self._use_context_cb.setToolTip(
            'When checked, the character selected in the sidebar is sent to '
            'the AI as context so generated lore stays consistent with it.\n'
            'Uncheck to generate without any card context.'
        )
        self._use_context_cb.setChecked(self._use_card_context)
        self._use_context_cb.toggled.connect(self._on_use_context_toggled)
        header_row.addWidget(self._use_context_cb)
        self._card_label = QLabel('')
        self._card_label.setStyleSheet('color: #6cb6ff;')
        self._card_label.setToolTip(
            'The character selected in the sidebar is used as context for AI '
            'generation so lore stays consistent with it.'
        )
        header_row.addWidget(self._card_label)
        settings_btn = QPushButton('Settings')
        settings_btn.clicked.connect(lambda: self.settings_requested.emit())
        header_row.addWidget(settings_btn)
        right.addLayout(header_row)
        right.addWidget(self._build_book_group())
        right.addWidget(self._build_entries_group())
        right.addWidget(self._build_ai_group(), 1)
        right_container = QWidget()
        right_container.setLayout(right)
        layout.addWidget(right_container, 1)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(260)
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)

        self._search = QLineEdit()
        self._search.setPlaceholderText('Search lorebooks...')
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter_books)
        v.addWidget(self._search)

        self._book_list = QListWidget()
        self._book_list.currentRowChanged.connect(self._on_book_row_changed)
        v.addWidget(self._book_list, 1)

        row1 = QHBoxLayout()
        new_btn = QPushButton('New')
        new_btn.clicked.connect(self._on_new_book)
        row1.addWidget(new_btn)
        self._dup_btn = QPushButton('Duplicate')
        self._dup_btn.clicked.connect(self._on_duplicate_book)
        row1.addWidget(self._dup_btn)
        self._del_btn = QPushButton('Delete')
        self._del_btn.clicked.connect(self._on_delete_book)
        row1.addWidget(self._del_btn)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        import_btn = QPushButton('Import...')
        import_btn.setToolTip(
            'Import lorebook JSON files. Supports ST Explorer character-book '
            'JSON and SillyTavern world-info JSON.'
        )
        import_btn.clicked.connect(self._on_import)
        row2.addWidget(import_btn)
        self._export_btn = QPushButton('Export...')
        self._export_btn.clicked.connect(self._on_export)
        row2.addWidget(self._export_btn)
        v.addLayout(row2)

        hint = QLabel('Changes save automatically.')
        hint.setStyleSheet('color: #777; ')
        v.addWidget(hint)
        return panel

    def _build_book_group(self) -> QGroupBox:
        group = QGroupBox('Book')
        form = QFormLayout(group)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText('Lorebook name')
        form.addRow('Name:', self._name_edit)
        self._desc_edit = QLineEdit()
        self._desc_edit.setPlaceholderText('What this book covers')
        form.addRow('Description:', self._desc_edit)

        depth_row = QHBoxLayout()
        self._scan_depth_spin = QSpinBox()
        self._scan_depth_spin.setRange(0, 9999)
        self._scan_depth_spin.setSpecialValueText('None')
        depth_row.addWidget(self._scan_depth_spin)
        budget_label = QLabel('Token Budget:')
        budget_label.setStyleSheet('color: #aaa;')
        depth_row.addWidget(budget_label)
        self._token_budget_spin = QSpinBox()
        self._token_budget_spin.setRange(0, 999999)
        self._token_budget_spin.setSpecialValueText('None')
        depth_row.addWidget(self._token_budget_spin)
        depth_row.addStretch()
        form.addRow('Scan Depth:', depth_row)

        self._recursive_cb = QCheckBox('Recursive Scanning (entries can trigger other entries)')
        form.addRow('', self._recursive_cb)

        stats_row = QHBoxLayout()
        self._stats_label = QLabel('')
        self._stats_label.setStyleSheet('color: #777;')
        stats_row.addWidget(self._stats_label)
        stats_row.addStretch()
        self._book_ext_btn = QPushButton('Extensions...')
        self._book_ext_btn.setToolTip(
            'Edit advanced extension fields stored on this book '
            '(preserved from SillyTavern world-info imports).'
        )
        self._book_ext_btn.clicked.connect(self._on_book_extensions)
        stats_row.addWidget(self._book_ext_btn)
        form.addRow('', stats_row)

        for w in (self._name_edit, self._desc_edit):
            w.textEdited.connect(self._on_field_edited)
        self._scan_depth_spin.valueChanged.connect(self._on_field_edited)
        self._token_budget_spin.valueChanged.connect(self._on_field_edited)
        self._recursive_cb.stateChanged.connect(self._on_field_edited)
        return group

    def _build_entries_group(self) -> QGroupBox:
        group = QGroupBox('Entries')
        v = QVBoxLayout(group)

        self._entry_list = QListWidget()
        self._entry_list.setMinimumHeight(120)
        self._entry_list.itemDoubleClicked.connect(lambda *_: self._on_edit_entry())
        v.addWidget(self._entry_list)

        btns = QHBoxLayout()
        add_btn = QPushButton('Add')
        add_btn.clicked.connect(self._on_add_entry)
        btns.addWidget(add_btn)
        edit_btn = QPushButton('Edit')
        edit_btn.clicked.connect(self._on_edit_entry)
        btns.addWidget(edit_btn)
        self._remove_btn = QPushButton('Remove')
        self._remove_btn.clicked.connect(self._on_remove_entry)
        btns.addWidget(self._remove_btn)
        up_btn = QPushButton('↑')
        up_btn.setFixedWidth(28)
        up_btn.clicked.connect(lambda: self._move_entry(-1))
        btns.addWidget(up_btn)
        down_btn = QPushButton('↓')
        down_btn.setFixedWidth(28)
        down_btn.clicked.connect(lambda: self._move_entry(1))
        btns.addWidget(down_btn)
        self._gen_entry_btn = QPushButton('Generate Content')
        self._gen_entry_btn.setToolTip(
            'Fill the selected entry\'s content with AI, based on its keys, '
            'the book, and the selected character.'
        )
        self._gen_entry_btn.clicked.connect(self._on_generate_entry_content)
        btns.addWidget(self._gen_entry_btn)
        btns.addStretch()
        v.addLayout(btns)
        return group

    def _build_ai_group(self) -> QGroupBox:
        group = QGroupBox('AI Generation')
        v = QVBoxLayout(group)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel('Concept / additional instructions:'))
        self._ai_concept = QLineEdit()
        self._ai_concept.setPlaceholderText(
            'e.g., A fantasy kingdom with political intrigue and ancient ruins'
        )
        row1.addWidget(self._ai_concept, 1)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel('Entries:'))
        self._ai_count = QSpinBox()
        self._ai_count.setRange(1, 30)
        self._ai_count.setValue(8)
        row2.addWidget(self._ai_count)
        row2.addWidget(QLabel('Words per entry (optional):'))
        self._ai_length = QLineEdit()
        self._ai_length.setMaximumWidth(90)
        self._ai_length.setPlaceholderText('e.g., 80')
        row2.addWidget(self._ai_length)
        row2.addStretch()
        v.addLayout(row2)

        row3 = QHBoxLayout()
        self._gen_full_btn = QPushButton('Generate Full Book')
        self._gen_full_btn.setToolTip(
            'Generate a complete set of entries from the concept. Applying the '
            'result REPLACES this book\'s entries.'
        )
        self._gen_full_btn.clicked.connect(lambda: self._on_generate_book(replace=True))
        row3.addWidget(self._gen_full_btn)
        self._gen_more_btn = QPushButton('Generate More Entries')
        self._gen_more_btn.setToolTip(
            'Generate additional entries without touching existing ones. '
            'Existing entries are shown to the AI to avoid duplication.'
        )
        self._gen_more_btn.clicked.connect(lambda: self._on_generate_book(replace=False))
        row3.addWidget(self._gen_more_btn)
        self._cancel_gen_btn = QPushButton('Cancel')
        self._cancel_gen_btn.setEnabled(False)
        self._cancel_gen_btn.clicked.connect(self._on_cancel_generation)
        row3.addWidget(self._cancel_gen_btn)
        row3.addStretch()
        v.addLayout(row3)

        self._ai_result = QTextEdit()
        self._ai_result.setReadOnly(True)
        self._ai_result.setMinimumHeight(110)
        self._ai_result.setPlaceholderText('Generated results will appear here.')
        v.addWidget(self._ai_result, 1)

        row4 = QHBoxLayout()
        self._apply_btn = QPushButton('Apply Result')
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply_result)
        row4.addWidget(self._apply_btn)
        clear_btn = QPushButton('Clear Result')
        clear_btn.setEnabled(False)
        clear_btn.clicked.connect(self._on_clear_result)
        row4.addWidget(clear_btn)
        self._clear_result_btn = clear_btn
        row4.addStretch()
        v.addLayout(row4)

        self._ai_gen_btns = [self._gen_full_btn, self._gen_more_btn, self._gen_entry_btn]
        return group

    # ---- book list ----

    def refresh_books(self, select_first: bool = False) -> None:
        """Rebuild the lorebook list, keeping the current selection if possible."""
        del select_first  # kept for API compatibility; first row is the fallback anyway
        keep = self._filename
        self._loading = True
        self._book_list.blockSignals(True)
        try:
            self._book_list.clear()
            restore_row = -1
            for i, meta in enumerate(lorebook_store.list_lorebooks()):
                item = QListWidgetItem(f"{meta['name']}  ({meta['entries']})")
                item.setData(Qt.ItemDataRole.UserRole, meta['filename'])
                self._book_list.addItem(item)
                if meta['filename'] == keep:
                    restore_row = i
            target_row = restore_row if restore_row >= 0 else (
                0 if self._book_list.count() else -1
            )
            if target_row >= 0:
                self._book_list.setCurrentRow(target_row)
        finally:
            self._book_list.blockSignals(False)
            self._loading = False
        if target_row >= 0:
            # Signals were blocked above, so drive the load explicitly.
            self._on_book_row_changed(target_row)
        else:
            self._clear_editor()
        self._sync_button_states()

    def _filter_books(self, text: str) -> None:
        query = text.strip().lower()
        for i in range(self._book_list.count()):
            item = self._book_list.item(i)
            item.setHidden(bool(query) and query not in item.text().lower())

    def _on_book_row_changed(self, row: int) -> None:
        if self._loading:
            return
        self._flush_autosave()
        item = self._book_list.item(row)
        if item is None:
            self._clear_editor()
            return
        filename = item.data(Qt.ItemDataRole.UserRole)
        book = lorebook_store.load_lorebook(filename)
        if book is None:
            # Do not keep editing the *previous* book under the new row: a
            # later autosave would write it to the newly selected filename.
            self._clear_editor()
            self.status_message.emit(f'Could not load lorebook: {filename}', 5000)
            return
        self._filename = filename
        self._book = book
        self._populate_editor()

    def _sync_button_states(self) -> None:
        has_book = self._book is not None and self._filename is not None
        for w in (self._dup_btn, self._del_btn, self._export_btn,
                  self._gen_full_btn, self._gen_more_btn, self._book_ext_btn):
            w.setEnabled(has_book)
        self._remove_btn.setEnabled(has_book and bool(self._entry_list.count()))

    # ---- editor population / persistence ----

    def _clear_editor(self) -> None:
        self._loading = True
        try:
            self._book = None
            self._filename = None
            self._name_edit.clear()
            self._desc_edit.clear()
            self._scan_depth_spin.setValue(0)
            self._token_budget_spin.setValue(0)
            self._recursive_cb.setChecked(False)
            self._stats_label.setText('')
            self._entry_list.clear()
        finally:
            self._loading = False
        self._sync_button_states()

    def _populate_editor(self) -> None:
        if self._book is None:
            self._clear_editor()
            return
        self._loading = True
        try:
            self._name_edit.setText(self._book.name)
            self._desc_edit.setText(self._book.description)
            self._scan_depth_spin.setValue(self._book.scan_depth or 0)
            self._token_budget_spin.setValue(self._book.token_budget or 0)
            self._recursive_cb.setChecked(self._book.recursive_scanning)
            self._refresh_entry_list()
        finally:
            self._loading = False

    def _refresh_entry_list(self) -> None:
        self._entry_list.clear()
        if self._book is None:
            return
        for i, entry in enumerate(self._book.entries):
            name = entry.name or f'Entry {i + 1}'
            keys = ', '.join(entry.keys)
            label = f'{name}' + (f'  ·  {keys}' if keys else '')
            if not entry.enabled:
                label = '[disabled] ' + label
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._entry_list.addItem(item)
        self._stats_label.setText(format_book_stats(self._book))
        self._sync_button_states()

    def _collect_editor(self) -> CharacterBook | None:
        """Build a CharacterBook from the editor widgets (or None if empty)."""
        if self._book is None:
            return None
        book = self._book
        book.name = self._name_edit.text().strip()
        book.description = self._desc_edit.text().strip()
        sd = self._scan_depth_spin.value()
        book.scan_depth = sd if sd > 0 else None
        tb = self._token_budget_spin.value()
        book.token_budget = tb if tb > 0 else None
        book.recursive_scanning = self._recursive_cb.isChecked()
        return book

    def _on_field_edited(self, *args) -> None:
        if self._loading or self._book is None:
            return
        self._collect_editor()
        self._autosave_timer.start()

    def _on_book_extensions(self) -> None:
        """Edit the book-level extensions dict (autosaves on accept)."""
        if self._book is None:
            return
        from src.ui.widgets.extensions_editor import ExtensionsEditor
        result = ExtensionsEditor.edit(
            dict(self._book.extensions or {}), self, reserved_keys=set(),
        )
        if result is not None:
            self._book.extensions = dict(result)
            self._on_field_edited()
            self.status_message.emit('Book extensions updated.', 3000)

    def _flush_autosave(self) -> None:
        if self._autosave_timer.isActive():
            self._autosave_timer.stop()
            self._persist_now()

    def _persist_now(self) -> None:
        if self._loading or self._book is None or self._filename is None:
            return
        self._collect_editor()
        try:
            lorebook_store.save_lorebook(self._filename, self._book)
            self._update_list_item_label()
            self.lorebooks_changed.emit()
        except Exception as e:
            # Includes serialization failures: this runs in a timer slot, and
            # an escaping exception would abort the process.
            logger.exception("Failed to autosave lorebook")
            self.status_message.emit(f'Autosave failed: {e}', 5000)

    def _update_list_item_label(self) -> None:
        if self._book is None or self._filename is None:
            return
        # Match by filename rather than the current row: on a row switch the
        # autosave flush runs while the new row is already current, and
        # updating "the current row" then overwrites the wrong label.
        for row in range(self._book_list.count()):
            item = self._book_list.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == self._filename:
                item.setText(f"{self._book.name or '(untitled)'}  ({len(self._book.entries)})")
                return

    # ---- book actions ----

    def _current_row(self) -> int:
        return self._book_list.currentRow()

    def _select_filename(self, filename: str) -> bool:
        for i in range(self._book_list.count()):
            if self._book_list.item(i).data(Qt.ItemDataRole.UserRole) == filename:
                self._book_list.setCurrentRow(i)
                return True
        return False

    def _on_new_book(self) -> None:
        self._flush_autosave()
        book = CharacterBook(name='New Lorebook', description='')
        filename = lorebook_store.unique_filename(book.name)
        lorebook_store.save_lorebook(filename, book)
        self.refresh_books()
        self._select_filename(filename)
        self.status_message.emit(f'Created "{book.name}".', 3000)
        self.lorebooks_changed.emit()

    def _on_duplicate_book(self) -> None:
        if self._book is None or self._filename is None:
            return
        self._flush_autosave()
        clone = CharacterBook.from_dict(self._book.to_dict())
        clone.name = (clone.name or 'Untitled') + ' (copy)'
        filename = lorebook_store.unique_filename(clone.name)
        lorebook_store.save_lorebook(filename, clone)
        self.refresh_books()
        self._select_filename(filename)
        self.status_message.emit(f'Duplicated to "{clone.name}".', 3000)
        self.lorebooks_changed.emit()

    def _on_delete_book(self) -> None:
        if self._book is None or self._filename is None:
            return
        name = self._book.name or self._filename
        if QMessageBox.question(
            self, 'Delete Lorebook',
            f'Delete "{name}"?\n\nThis cannot be undone.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._autosave_timer.stop()
        lorebook_store.delete_lorebook(self._filename)
        self._filename = None
        self._book = None
        self.refresh_books()
        self.status_message.emit(f'Deleted "{name}".', 3000)
        self.lorebooks_changed.emit()

    # ---- import / export ----

    def _on_import(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Import Lorebooks', '', 'JSON (*.json);;All files (*)',
        )
        if not paths:
            return
        imported = 0
        skipped: list[str] = []
        for p in paths:
            path = Path(p)
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
                skipped.append(f'{path.name}: {e}')
                continue
            book = lorebook_store.parse_book_json(raw)
            if not book.entries:
                skipped.append(f'{path.name}: no usable entries found')
                continue
            if not book.name:
                book.name = path.stem
            filename = lorebook_store.unique_filename(book.name)
            lorebook_store.save_lorebook(filename, book)
            imported += 1
        self.refresh_books()
        self.lorebooks_changed.emit()
        msg = f'Imported {imported} lorebook(s).'
        if skipped:
            msg += '\n\nSkipped:\n' + '\n'.join(skipped[:10])
            QMessageBox.warning(self, 'Import Lorebooks', msg)
        else:
            self.status_message.emit(msg, 5000)

    def _on_export(self) -> None:
        if self._book is None or self._filename is None:
            return
        self._flush_autosave()
        menu_default = (self._book.name or Path(self._filename).stem).strip() or 'lorebook'
        choice = QMessageBox.question(
            self, 'Export Format',
            'Export as:\n\n'
            'Yes — SillyTavern world-info JSON (importable by SillyTavern)\n'
            'No — V2 character-book JSON (ST Explorer native)',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return
        default_name = menu_default.replace(' ', '_') + '.json'
        dest, _ = QFileDialog.getSaveFileName(
            self, 'Export Lorebook', default_name, 'JSON (*.json)',
        )
        if not dest:
            return
        if not dest.lower().endswith('.json'):
            dest += '.json'
        data = (
            lorebook_store.book_to_st_world_info(self._book)
            if choice == QMessageBox.StandardButton.Yes
            else self._book.to_dict()
        )
        try:
            Path(dest).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8',
            )
            self.status_message.emit(f'Exported lorebook to {dest}', 5000)
        except OSError as e:
            QMessageBox.critical(self, 'Export', f'Failed to export: {e}')

    # ---- entry actions ----

    def _selected_entry_index(self) -> int:
        item = self._entry_list.currentItem()
        if item is None:
            return -1
        return int(item.data(Qt.ItemDataRole.UserRole))

    def _on_add_entry(self) -> None:
        if self._book is None:
            return
        dlg = _BookEntryEditDialog(BookEntry(insertion_order=len(self._book.entries) * 100), self)
        try:
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._book.entries.append(dlg.get_entry())
                self._refresh_entry_list()
                self._persist_now()
        finally:
            # Parented dialogs are hidden, not destroyed, after exec(): one
            # subtree leaked per Add/Edit otherwise.
            dlg.setParent(None)
            dlg.deleteLater()

    def _on_edit_entry(self, *args) -> None:
        if self._book is None:
            return
        idx = self._selected_entry_index()
        if not 0 <= idx < len(self._book.entries):
            return
        dlg = _BookEntryEditDialog(self._book.entries[idx], self)
        try:
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._book.entries[idx] = dlg.get_entry()
                self._refresh_entry_list()
                self._persist_now()
        finally:
            dlg.setParent(None)
            dlg.deleteLater()

    def _on_remove_entry(self) -> None:
        if self._book is None:
            return
        idx = self._selected_entry_index()
        if not 0 <= idx < len(self._book.entries):
            return
        entry = self._book.entries[idx]
        # Entry removal persists immediately (no undo), so confirm it first.
        if QMessageBox.question(
            self, 'Remove Entry',
            f'Remove "{entry.name or f"entry {idx + 1}"}"?\n\n'
            'This is saved immediately and cannot be undone.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._book.entries.pop(idx)
        self._refresh_entry_list()
        next_idx = min(idx, max(0, len(self._book.entries) - 1))
        if self._entry_list.count():
            self._entry_list.setCurrentRow(next_idx)
        self._persist_now()

    def _move_entry(self, delta: int) -> None:
        if self._book is None:
            return
        idx = self._selected_entry_index()
        new_idx = idx + delta
        if not 0 <= idx < len(self._book.entries) or not 0 <= new_idx < len(self._book.entries):
            return
        entries = self._book.entries
        entries[idx], entries[new_idx] = entries[new_idx], entries[idx]
        self._refresh_entry_list()
        self._entry_list.setCurrentRow(new_idx)
        self._persist_now()

    # ---- AI generation ----

    def set_selected_card(self, char_id: int | None) -> None:
        """Track the shared-sidebar selection as optional generation context."""
        self._selected_card_id = char_id
        self._update_card_context_ui()

    def _update_card_context_ui(self) -> None:
        """Sync the toggle's enabled state and the header label."""
        has_card = self._selected_card_id is not None
        name = ''
        if has_card:
            entry = self.db.get_by_id(self._selected_card_id)
            if entry:
                name = entry.get('name', '')
            else:
                # Card vanished (deleted); nothing to offer as context.
                has_card = False
                self._selected_card_id = None
        self._use_context_cb.setEnabled(has_card)
        self._card_label.setText(
            format_card_context_label(name or None, self._use_card_context),
        )

    def _on_use_context_toggled(self, checked: bool) -> None:
        """Persist the card-context preference and refresh the label."""
        self._use_card_context = bool(checked)
        save_lorebook_use_card_context(self._use_card_context)
        self._update_card_context_ui()

    def _get_selected_card(self) -> CharacterCard | None:
        if not self._use_card_context:
            return None
        if self._selected_card_id is None:
            return None
        entry = self.db.get_by_id(self._selected_card_id)
        if not entry:
            return None
        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            return None
        raw = read_card_data(source)
        if not raw:
            return None
        return CharacterCard.from_spec_dict(raw, source)

    def reload_preset(self) -> None:
        """Reload the cached API preset after a settings change."""
        self._api_preset = preset_from_saved(load_api_settings())

    @staticmethod
    def _worker_is_alive(worker) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def cleanup_workers(self, timeout_ms: int = 3000) -> bool:
        worker = self._worker
        if self._worker_is_alive(worker):
            try:
                worker.cancel()
                if not worker.wait(timeout_ms):
                    return False
            except RuntimeError:
                pass
        return True

    def _require_book(self) -> bool:
        if self._book is None or self._filename is None:
            QMessageBox.information(
                self, 'AI Generation', 'Create or select a lorebook first.',
            )
            return False
        return True

    def _start_worker(self, mode: str, params: dict) -> None:
        if self._worker_is_alive(self._worker):
            return
        self._pending_mode = None
        self._pending_text = None
        self._pending_filename = None
        self._apply_btn.setEnabled(False)
        self._clear_result_btn.setEnabled(False)
        self._ai_result.setPlainText('Generating...')
        for b in self._ai_gen_btns:
            b.setEnabled(False)
        self._cancel_gen_btn.setEnabled(True)
        client = AIClient(self._api_preset)
        self._worker = _LorebookWorker(client, mode, params, self)
        self._result_delivered = False
        self._worker.chunk.connect(self._on_chunk)
        self._worker.completed.connect(self._on_generated)
        self._worker.error.connect(self._on_generate_error)
        # A cancelled run returns from run() without emitting anything, so the
        # finished signal is what unsticks the "Generating..." UI.
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _common_params(self) -> dict:
        self._flush_autosave()
        return {
            'card': self._get_selected_card(),
            'length': self._ai_length.text().strip(),
            'extra': self._ai_concept.text().strip(),
        }

    def _on_generate_book(self, replace: bool) -> None:
        if not self._require_book() or self._worker_is_alive(self._worker):
            return
        params = self._common_params()
        concept = params.pop('extra') or (self._book.description or self._book.name or '')
        params['concept'] = concept
        params['extra'] = ''
        params['count'] = str(self._ai_count.value())
        params['existing_summary'] = '' if replace else summarize_book_for_ai(self._book)
        self._pending_target_replace = replace
        self._start_worker('book', params)

    def _on_generate_entry_content(self) -> None:
        if not self._require_book() or self._worker_is_alive(self._worker):
            return
        idx = self._selected_entry_index()
        if not 0 <= idx < len(self._book.entries):
            QMessageBox.information(self, 'Generate Content', 'Select an entry first.')
            return
        entry = self._book.entries[idx]
        others = [e for j, e in enumerate(self._book.entries) if j != idx]
        pseudo = CharacterBook(entries=others)
        params = self._common_params()
        params.update({
            'entry_name': entry.name,
            'keys': list(entry.keys),
            'book_description': self._book.description,
            'other_entries': summarize_book_for_ai(pseudo),
        })
        self._start_worker('entry', params)

    @pyqtSlot(str)
    def _on_chunk(self, text: str) -> None:
        self._ai_result.moveCursor(QTextCursor.MoveOperation.End)
        self._ai_result.insertPlainText(text)
        self._ai_result.ensureCursorVisible()

    @pyqtSlot(str)
    def _on_generated(self, text: str) -> None:
        mode = self._worker.mode if self._worker is not None else None
        self._result_delivered = True
        self._finish_generation()
        self._pending_text = text
        self._pending_mode = mode
        self._pending_filename = self._filename
        self._apply_btn.setEnabled(True)
        self._clear_result_btn.setEnabled(True)

    @pyqtSlot(str)
    def _on_generate_error(self, error: str) -> None:
        self._result_delivered = True
        self._finish_generation()
        short = error.splitlines()[0] if error else 'Unknown error'
        self._ai_result.setPlainText(f'Error: {error}')
        QMessageBox.critical(self, 'Generation Error', short)

    def _on_worker_finished(self) -> None:
        """Unstick the UI when the worker ended without a result (cancel)."""
        if not getattr(self, '_result_delivered', True):
            self._result_delivered = True
            self._finish_generation()
            self._ai_result.setPlainText('Generation cancelled.')

    def _finish_generation(self) -> None:
        self._worker = None
        for b in self._ai_gen_btns:
            b.setEnabled(self._book is not None)
        self._cancel_gen_btn.setEnabled(False)

    def _on_cancel_generation(self) -> None:
        if self._worker_is_alive(self._worker):
            self._worker.cancel()

    def _on_clear_result(self) -> None:
        self._pending_mode = None
        self._pending_text = None
        self._pending_filename = None
        self._ai_result.clear()
        self._apply_btn.setEnabled(False)
        self._clear_result_btn.setEnabled(False)

    def _on_apply_result(self) -> None:
        if not self._pending_text or self._pending_mode is None:
            return
        if self._book is None or self._filename is None:
            QMessageBox.information(self, 'Apply Result', 'Select a lorebook first.')
            return
        if self._pending_filename != self._filename:
            QMessageBox.warning(
                self, 'Selection Changed',
                'This result was generated for a different lorebook.\n'
                'Select that lorebook again to apply it.',
            )
            return
        self._flush_autosave()
        if self._pending_mode == 'entry':
            applied = self._apply_entry_content()
        else:
            applied = self._apply_book_entries()
        if applied:
            # Only clear on success: a declined overwrite or unparsable
            # output must keep the generated text so the user can retry
            # without paying for another generation.
            self._on_clear_result()

    def _apply_entry_content(self) -> bool:
        idx = self._selected_entry_index()
        if not 0 <= idx < len(self._book.entries):
            QMessageBox.information(self, 'Apply Result', 'Select an entry first.')
            return False
        entry = self._book.entries[idx]
        if entry.content and entry.content.strip():
            if QMessageBox.question(
                self, 'Overwrite Entry',
                f'Replace the content of "{entry.name or "this entry"}"? Continue?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return False
        entry.content = lorebook_store.clean_generated_text(self._pending_text)
        self._refresh_entry_list()
        self._persist_now()
        self.status_message.emit('Entry content updated.', 3000)
        return True

    def _apply_book_entries(self) -> bool:
        replace = getattr(self, '_pending_target_replace', False)
        generated = lorebook_store.parse_generated_book(self._pending_text)
        if generated is None:
            QMessageBox.warning(
                self, 'Apply Result',
                'Could not parse any entries from the generated output.\n'
                'Try generating again or adjust the prompt.',
            )
            return False
        if replace:
            if QMessageBox.question(
                self, 'Replace Entries',
                f'This will REPLACE all {len(self._book.entries)} existing '
                f'entr{"y" if len(self._book.entries) == 1 else "ies"} with the '
                f'{len(generated.entries)} generated ones. Continue?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return False
            self._book.entries = generated.entries
            if generated.name:
                self._name_edit.setText(generated.name)
                self._book.name = generated.name
            if generated.description:
                self._desc_edit.setText(generated.description)
                self._book.description = generated.description
        else:
            self._book.entries.extend(generated.entries)
        self._populate_editor()
        self._persist_now()
        verb = 'Replaced' if replace else 'Appended'
        self.status_message.emit(
            f'{verb} {len(generated.entries)} entr'
            f'{"y" if len(generated.entries) == 1 else "ies"}.', 4000,
        )
        return True

    def flush(self) -> None:
        """Force-write any pending debounced edits (called on app shutdown)."""
        self._flush_autosave()

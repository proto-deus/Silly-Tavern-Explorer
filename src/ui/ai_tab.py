from __future__ import annotations

import logging
import traceback
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ai_client import AIClient, APIPreset, _extract_json, _parse_tags, preset_from_saved
from src import ai_prompts
from src.card_models import CharacterCard
from src.card_parser import read_card_data
from src.database import LibraryDatabase
from src.settings_manager import load_api_settings
from src.tag_ops import merge_tags
from src.token_counter import count_card_tokens, count_tokens

logger = logging.getLogger(__name__)


class _GenerateWorker(QThread):
    # Named ``completed`` so the built-in QThread.finished stays available
    # for lifetime management (deleteLater).
    completed = pyqtSignal(object)
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
            if self.mode == 'tags':
                card = self.params['card']
                system, user = ai_prompts.build_tags_prompts(
                    card,
                    extra=self.params.get('extra', ''),
                    length=self.params.get('length', ''),
                )
                full = self._stream_generate(system, user)
                if not self._cancel and full is not None:
                    tags = _parse_tags(full)
                    self.completed.emit(tags)
            elif self.mode == 'missing_tags':
                card = self.params['card']
                system, user = ai_prompts.build_missing_tags_prompts(
                    card,
                    extra=self.params.get('extra', ''),
                    length=self.params.get('length', ''),
                )
                full = self._stream_generate(system, user)
                if not self._cancel and full is not None:
                    tags = _parse_tags(full)
                    self.completed.emit(tags)
            elif self.mode == 'summary':
                card = self.params['card']
                system, user = ai_prompts.build_summary_prompts(
                    card,
                    extra=self.params.get('extra', ''),
                    length=self.params.get('length', ''),
                )
                full = self._stream_generate(system, user)
                if not self._cancel and full is not None:
                    self.completed.emit(full)
            elif self.mode == 'alt_greetings':
                card = self.params['card']
                system, user = ai_prompts.build_alt_greetings_prompts(
                    card,
                    extra=self.params.get('extra', ''),
                    length=self.params.get('length', ''),
                )
                full = self._stream_generate(system, user)
                if not self._cancel and full is not None:
                    self.completed.emit(full)
            elif self.mode == 'fill':
                card = self.params['card']
                fields = self.params['fields']
                system, user = ai_prompts.build_fill_prompts(
                    card, fields,
                    extra=self.params.get('extra', ''),
                    field_lengths=self.params.get('field_lengths', None),
                )
                full = self._stream_generate(system, user)
                if not self._cancel and full is not None:
                    raw = _extract_json(full, expected_keys=set(fields))
                    if raw is None:
                        raise ValueError("AI did not return valid JSON for fill result")
                    self.completed.emit({'fields': fields, 'result': raw, 'card': card})
        except Exception as e:
            if not self._cancel:
                self.error.emit(f"{e}\n{traceback.format_exc()}")

    def _stream_generate(self, system: str, user: str) -> str | None:
        """Stream a generation, accumulating the full text.

        Returns the accumulated full text, or ``None`` if cancelled mid-stream.
        """
        parts: list[str] = []
        for piece in self.client.generate(system, user, stream=True):
            if self._cancel:
                return None
            parts.append(piece)
        return ''.join(parts)


class AITab(QWidget):
    settings_requested = pyqtSignal()
    library_changed = pyqtSignal()
    card_updated = pyqtSignal(int)

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._api_preset = self._load_saved_preset()
        self._worker: _GenerateWorker | None = None
        self._gen_source_id: int | None = None
        self._last_tags: list[str] | None = None
        self._last_summary: str | None = None
        self._last_alt_greeting: str | None = None
        self._last_fill_result: dict | None = None
        self._last_fill_fields: list[str] | None = None
        self._selected_id: int | None = None
        self._active_mode: str | None = None
        self._tags_mode: str = 'replace'

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # Header: title + current selection + settings.
        header_row = QHBoxLayout()
        title = QLabel('Generate')
        title.setStyleSheet('font-size: 16px; font-weight: bold; color: #e0e0e0;')
        header_row.addWidget(title)
        self._selected_label = QLabel('No character selected')
        self._selected_label.setStyleSheet('font-size: 13px; color: #6cb6ff;')
        header_row.addWidget(self._selected_label)
        header_row.addStretch()
        settings_btn = QPushButton('Settings')
        settings_btn.clicked.connect(self._on_settings_requested)
        header_row.addWidget(settings_btn)
        main_layout.addLayout(header_row)

        # --- Generate Tags section ---
        tags_group = QGroupBox('Generate Tags')
        tags_layout = QVBoxLayout(tags_group)
        tags_len_row = QHBoxLayout()
        tags_len_row.addWidget(QLabel('Target number of tags (optional):'))
        self._tags_length = QLineEdit()
        self._tags_length.setMaximumWidth(100)
        self._tags_length.setPlaceholderText('e.g., 10')
        tags_len_row.addWidget(self._tags_length)
        tags_len_row.addStretch()
        tags_layout.addLayout(tags_len_row)
        tags_layout.addWidget(QLabel('Additional Instructions (optional):'))
        self._tags_extra_edit = QLineEdit()
        self._tags_extra_edit.setPlaceholderText('e.g., Focus on genre and mood tags')
        tags_layout.addWidget(self._tags_extra_edit)
        tags_btn_row = QHBoxLayout()
        self._tags_gen_btn = QPushButton('Generate Tags')
        self._tags_gen_btn.clicked.connect(self._on_generate_tags)
        tags_btn_row.addWidget(self._tags_gen_btn)
        self._missing_tags_btn = QPushButton('Generate Missing Tags')
        self._missing_tags_btn.clicked.connect(self._on_generate_missing_tags)
        tags_btn_row.addWidget(self._missing_tags_btn)
        self._tags_save_btn = QPushButton('Save to Card')
        self._tags_save_btn.setEnabled(False)
        self._tags_save_btn.clicked.connect(self._on_save_tags)
        tags_btn_row.addWidget(self._tags_save_btn)
        self._tags_clear_btn = QPushButton('Clear')
        self._tags_clear_btn.setEnabled(False)
        self._tags_clear_btn.clicked.connect(self._on_clear_tags)
        tags_btn_row.addWidget(self._tags_clear_btn)
        tags_btn_row.addStretch()
        tags_layout.addLayout(tags_btn_row)
        self._tags_result_box = self._make_result_box(60)
        tags_layout.addWidget(self._tags_result_box)
        main_layout.addWidget(tags_group)

        # --- Generate Summary section ---
        summary_group = QGroupBox('Generate Summary')
        summary_layout = QVBoxLayout(summary_group)
        summary_len_row = QHBoxLayout()
        summary_len_row.addWidget(QLabel('Target word count (optional):'))
        self._summary_length = QLineEdit()
        self._summary_length.setMaximumWidth(100)
        self._summary_length.setPlaceholderText('e.g., 150')
        summary_len_row.addWidget(self._summary_length)
        summary_len_row.addStretch()
        summary_layout.addLayout(summary_len_row)
        summary_layout.addWidget(QLabel('Additional Instructions (optional):'))
        self._summary_extra_edit = QLineEdit()
        self._summary_extra_edit.setPlaceholderText('e.g., Emphasize their backstory')
        summary_layout.addWidget(self._summary_extra_edit)
        summary_btn_row = QHBoxLayout()
        self._summary_gen_btn = QPushButton('Generate Summary')
        self._summary_gen_btn.clicked.connect(self._on_generate_summary)
        summary_btn_row.addWidget(self._summary_gen_btn)
        self._summary_save_btn = QPushButton('Save to Card')
        self._summary_save_btn.setEnabled(False)
        self._summary_save_btn.clicked.connect(self._on_save_summary)
        summary_btn_row.addWidget(self._summary_save_btn)
        self._summary_clear_btn = QPushButton('Clear')
        self._summary_clear_btn.setEnabled(False)
        self._summary_clear_btn.clicked.connect(self._on_clear_summary)
        summary_btn_row.addWidget(self._summary_clear_btn)
        summary_btn_row.addStretch()
        summary_layout.addLayout(summary_btn_row)
        self._summary_result_box = self._make_result_box(90)
        summary_layout.addWidget(self._summary_result_box)
        main_layout.addWidget(summary_group)

        # --- Generate Alternate Greetings section ---
        alt_greet_group = QGroupBox('Generate Alternate Greetings')
        alt_greet_layout = QVBoxLayout(alt_greet_group)
        alt_greet_len_row = QHBoxLayout()
        alt_greet_len_row.addWidget(QLabel('Target word count (optional):'))
        self._alt_greet_length = QLineEdit()
        self._alt_greet_length.setMaximumWidth(100)
        self._alt_greet_length.setPlaceholderText('e.g., 80')
        alt_greet_len_row.addWidget(self._alt_greet_length)
        alt_greet_len_row.addStretch()
        alt_greet_layout.addLayout(alt_greet_len_row)
        alt_greet_layout.addWidget(QLabel('Describe the alternate greeting:'))
        self._alt_greet_prompt = QLineEdit()
        self._alt_greet_prompt.setPlaceholderText('e.g., A formal variant, or a light-hearted joke')
        alt_greet_layout.addWidget(self._alt_greet_prompt)
        alt_greet_btn_row = QHBoxLayout()
        self._alt_greet_gen_btn = QPushButton('Generate')
        self._alt_greet_gen_btn.clicked.connect(self._on_generate_alt_greeting)
        alt_greet_btn_row.addWidget(self._alt_greet_gen_btn)
        self._alt_greet_save_btn = QPushButton('Save to Card')
        self._alt_greet_save_btn.setEnabled(False)
        self._alt_greet_save_btn.clicked.connect(self._on_save_alt_greeting)
        alt_greet_btn_row.addWidget(self._alt_greet_save_btn)
        self._alt_greet_clear_btn = QPushButton('Clear')
        self._alt_greet_clear_btn.setEnabled(False)
        self._alt_greet_clear_btn.clicked.connect(self._on_clear_alt_greeting)
        alt_greet_btn_row.addWidget(self._alt_greet_clear_btn)
        alt_greet_btn_row.addStretch()
        alt_greet_layout.addLayout(alt_greet_btn_row)
        self._alt_greet_result_box = self._make_result_box(90)
        alt_greet_layout.addWidget(self._alt_greet_result_box)
        main_layout.addWidget(alt_greet_group)

        # --- Fill Missing Fields section ---
        fill_group = QGroupBox('Fill Missing Fields')
        fill_layout = QVBoxLayout(fill_group)
        self._fill_token_label = QLabel('Existing content: 0 tokens')
        self._fill_token_label.setStyleSheet('color: #aaa;')
        fill_layout.addWidget(self._fill_token_label)
        self._fill_checks: dict[str, QCheckBox] = {}
        self._fill_lengths: dict[str, QLineEdit] = {}
        fill_fields = [
            ('description', 'Description'),
            ('personality', 'Personality'),
            ('scenario', 'Scenario'),
            ('first_mes', 'First Message'),
            ('mes_example', 'Example Messages'),
            ('creator_notes', 'Creator Notes'),
            ('system_prompt', 'System Prompt'),
            ('post_history_instructions', 'Post-History Instructions'),
            ('tags', 'Tags'),
        ]
        _length_hints = {
            'description': 'e.g., 300',
            'personality': 'e.g., 100',
            'scenario': 'e.g., 100',
            'first_mes': 'e.g., 150',
            'mes_example': 'e.g., 200',
            'creator_notes': 'e.g., 50',
            'system_prompt': 'e.g., 100',
            'post_history_instructions': 'e.g., 50',
            'tags': 'e.g., 10',
        }
        fill_grid = QGridLayout()
        fill_grid.addWidget(QLabel('Field'), 0, 0)
        fill_grid.addWidget(QLabel('Target #'), 0, 1)
        for row, (key, label) in enumerate(fill_fields, start=1):
            cb = QCheckBox(label)
            self._fill_checks[key] = cb
            fill_grid.addWidget(cb, row, 0)
            length_edit = QLineEdit()
            length_edit.setMaximumWidth(80)
            length_edit.setPlaceholderText(_length_hints.get(key, ''))
            self._fill_lengths[key] = length_edit
            fill_grid.addWidget(length_edit, row, 1)
            cb.toggled.connect(length_edit.setEnabled)
            length_edit.setEnabled(cb.isChecked())
        fill_layout.addLayout(fill_grid)
        fill_extra_row = QHBoxLayout()
        fill_extra_row.addWidget(QLabel('Additional Instructions:'))
        self._fill_extra_edit = QLineEdit()
        self._fill_extra_edit.setPlaceholderText('e.g., Keep descriptions short, use a medieval tone')
        fill_extra_row.addWidget(self._fill_extra_edit)
        fill_layout.addLayout(fill_extra_row)
        fill_btn_row = QHBoxLayout()
        self._fill_gen_btn = QPushButton('Fill Missing Fields')
        self._fill_gen_btn.clicked.connect(self._on_generate_fill)
        fill_btn_row.addWidget(self._fill_gen_btn)
        self._fill_save_btn = QPushButton('Save to Card')
        self._fill_save_btn.setEnabled(False)
        self._fill_save_btn.clicked.connect(self._on_save_fill)
        fill_btn_row.addWidget(self._fill_save_btn)
        self._fill_clear_btn = QPushButton('Clear')
        self._fill_clear_btn.setEnabled(False)
        self._fill_clear_btn.clicked.connect(self._on_clear_fill)
        fill_btn_row.addWidget(self._fill_clear_btn)
        fill_btn_row.addStretch()
        fill_layout.addLayout(fill_btn_row)
        self._fill_result_box = self._make_result_box(140)
        fill_layout.addWidget(self._fill_result_box)
        main_layout.addWidget(fill_group)

        # --- Bottom actions ---
        bottom_row = QHBoxLayout()
        self._cancel_btn = QPushButton('Cancel')
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        bottom_row.addWidget(self._cancel_btn)
        bottom_row.addStretch()
        self._batch_btn = QPushButton('Batch Generate...')
        self._batch_btn.clicked.connect(self._on_batch)
        bottom_row.addWidget(self._batch_btn)
        self._create_btn = QPushButton('Generate New Character...')
        self._create_btn.clicked.connect(self._on_create_character)
        bottom_row.addWidget(self._create_btn)
        main_layout.addLayout(bottom_row)

        self._gen_btns = [self._tags_gen_btn, self._missing_tags_btn, self._summary_gen_btn, self._alt_greet_gen_btn, self._fill_gen_btn]

    @staticmethod
    def _make_result_box(min_height: int) -> QTextEdit:
        box = QTextEdit()
        box.setReadOnly(True)
        box.setMinimumHeight(min_height)
        box.setPlaceholderText('Results will appear here.')
        return box

    # ---- selection ----

    def select_card(self, char_id: int) -> None:
        """Set the currently-selected card (driven by the shared sidebar)."""
        changed = char_id != self._selected_id
        self._selected_id = char_id
        entry = self.db.get_by_id(char_id)
        if entry:
            self._selected_label.setText(f'Selected: {entry["name"]}')
        else:
            self._selected_label.setText('No character selected')
        if changed:
            self._clear_all_results()
        self._on_fill_card_changed()

    # ---- preset ----

    def _load_saved_preset(self) -> APIPreset:
        return preset_from_saved(load_api_settings())

    def reload_preset(self) -> None:
        """Reload the cached API preset after a settings change."""
        self._api_preset = self._load_saved_preset()

    def current_preset(self) -> APIPreset:
        return self._api_preset

    def cleanup_workers(self, timeout_ms: int = 3000) -> bool:
        """Cancel and wait for any running workers. Returns True if all
        workers finished within the timeout, False if still running."""
        worker = self._worker
        if self._worker_is_alive(worker):
            try:
                worker.cancel()
                if not worker.wait(timeout_ms):
                    return False
            except RuntimeError:
                pass
        return True

    # ---- handlers ----

    def _on_settings_requested(self) -> None:
        self.settings_requested.emit()

    def _on_fill_card_changed(self) -> None:
        """Update fill checkboxes and token count when the selected card changes."""
        card = self._get_selected_card()
        if card is None:
            for cb in self._fill_checks.values():
                cb.setChecked(False)
                cb.setEnabled(False)
            self._fill_token_label.setText('Existing content: 0 tokens')
            self._fill_gen_btn.setEnabled(False)
            return

        self._fill_gen_btn.setEnabled(True)
        field_map = {
            'description': card.description,
            'personality': card.personality,
            'scenario': card.scenario,
            'first_mes': card.first_mes,
            'mes_example': card.mes_example,
            'creator_notes': card.creator_notes,
            'system_prompt': card.system_prompt,
            'post_history_instructions': card.post_history_instructions,
            'tags': card.tags,
        }
        for key, cb in self._fill_checks.items():
            value = field_map.get(key)
            has_content = False
            if isinstance(value, list):
                has_content = bool(value)
            elif isinstance(value, str):
                has_content = bool(value and value.strip())
            cb.setEnabled(True)
            cb.setChecked(not has_content)
            self._fill_lengths[key].setEnabled(not has_content)

        token_count = count_tokens(
            '\n'.join(filter(None, [
                card.name, card.description, card.personality,
                card.scenario, card.first_mes, card.mes_example,
                card.creator_notes, card.system_prompt,
                card.post_history_instructions,
            ]))
        )
        self._fill_token_label.setText(f'Existing content: {token_count:,} tokens')

    def _get_selected_card(self) -> CharacterCard | None:
        if self._selected_id is None:
            return None
        entry = self.db.get_by_id(self._selected_id)
        if not entry:
            return None
        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            return None
        raw = read_card_data(source)
        if not raw:
            return None
        card = CharacterCard.from_spec_dict(raw, source)
        card.token_count = count_card_tokens(card)
        return card

    def _require_card(self) -> CharacterCard | None:
        card = self._get_selected_card()
        if card is None:
            QMessageBox.warning(self, 'Error', 'Please select a character in the sidebar.')
        return card

    def _on_generate_tags(self) -> None:
        card = self._require_card()
        if not card:
            return
        self._active_mode = 'tags'
        self._tags_mode = 'replace'
        self._start_generation('tags', {
            'card': card,
            'extra': self._tags_extra_edit.text().strip(),
            'length': self._tags_length.text().strip(),
        })

    def _on_generate_missing_tags(self) -> None:
        card = self._require_card()
        if not card:
            return
        self._active_mode = 'missing_tags'
        self._tags_mode = 'missing'
        self._start_generation('missing_tags', {
            'card': card,
            'extra': self._tags_extra_edit.text().strip(),
            'length': self._tags_length.text().strip(),
        })

    def _on_generate_summary(self) -> None:
        card = self._require_card()
        if not card:
            return
        self._active_mode = 'summary'
        self._start_generation('summary', {
            'card': card,
            'extra': self._summary_extra_edit.text().strip(),
            'length': self._summary_length.text().strip(),
        })

    def _on_generate_alt_greeting(self) -> None:
        card = self._require_card()
        if not card:
            return
        self._active_mode = 'alt_greetings'
        self._start_generation('alt_greetings', {
            'card': card,
            'extra': self._alt_greet_prompt.text().strip(),
            'length': self._alt_greet_length.text().strip(),
        })

    def _on_generate_fill(self) -> None:
        card = self._require_card()
        if not card:
            return
        selected_fields = [k for k, cb in self._fill_checks.items() if cb.isChecked()]
        if not selected_fields:
            QMessageBox.warning(self, 'Error', 'Select at least one field to generate.')
            return
        field_lengths: dict[str, str] = {}
        for f in selected_fields:
            len_val = self._fill_lengths[f].text().strip()
            if len_val:
                field_lengths[f] = len_val
        self._active_mode = 'fill'
        self._start_generation('fill', {
            'card': card,
            'fields': selected_fields,
            'extra': self._fill_extra_edit.text().strip(),
            'field_lengths': field_lengths,
        })

    def _on_batch(self) -> None:
        from src.ui.widgets.batch_generate_dialog import BatchGenerateDialog
        dlg = BatchGenerateDialog(self.db, self._api_preset, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.library_changed.connect(self.library_changed.emit)
        dlg.exec()

    def _on_create_character(self) -> None:
        from src.ui.widgets.character_wizard import CharacterCreationWizard
        wiz = CharacterCreationWizard(self.db, self._api_preset, self)
        wiz.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        wiz.library_changed.connect(self.library_changed.emit)
        wiz.exec()

    # ---- generation worker wiring ----

    def _set_generating(self, active: bool) -> None:
        for btn in self._gen_btns:
            btn.setEnabled(not active)
        self._cancel_btn.setEnabled(active)

    def _active_result_box(self) -> QTextEdit:
        if self._active_mode in ('tags', 'missing_tags'):
            return self._tags_result_box
        if self._active_mode == 'summary':
            return self._summary_result_box
        if self._active_mode == 'alt_greetings':
            return self._alt_greet_result_box
        return self._fill_result_box

    @staticmethod
    def _worker_is_alive(worker) -> bool:
        """True when *worker* exists and its C++ object hasn't been deleted."""
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def _start_generation(self, mode: str, params: dict) -> None:
        if self._worker_is_alive(self._worker):
            return
        client = AIClient(self._api_preset)

        # Clear the previous result for this section so a stale Save/Clear
        # can't operate on outdated data while the new one streams in.
        if mode == 'tags' or mode == 'missing_tags':
            self._last_tags = None
            self._tags_save_btn.setEnabled(False)
            self._tags_clear_btn.setEnabled(False)
            self._tags_result_box.setPlainText('Generating...')
        elif mode == 'summary':
            self._last_summary = None
            self._summary_save_btn.setEnabled(False)
            self._summary_clear_btn.setEnabled(False)
            self._summary_result_box.setPlainText('Generating...')
        elif mode == 'alt_greetings':
            self._last_alt_greeting = None
            self._alt_greet_save_btn.setEnabled(False)
            self._alt_greet_clear_btn.setEnabled(False)
            self._alt_greet_result_box.setPlainText('Generating...')
        elif mode == 'fill':
            self._last_fill_result = None
            self._fill_save_btn.setEnabled(False)
            self._fill_clear_btn.setEnabled(False)
            self._fill_result_box.setPlainText('Generating...')

        self._set_generating(True)

        # Remember which card the generation was started for: results may
        # arrive after the user selects a different card, and saving them
        # to the newly selected card would corrupt it.
        self._gen_source_id = self._selected_id
        self._worker = _GenerateWorker(client, mode, params, self)
        self._worker.completed.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        # Cleanup on the built-in signal so cancelled runs are still deleted.
        self._worker.finished.connect(self._clear_worker)
        self._worker.start()

    def _on_cancel(self) -> None:
        if not self._worker_is_alive(self._worker):
            self._set_generating(False)
            return
        # Cooperative cancel; poll from the GUI thread instead of wait() so
        # the UI never blocks. The generating state stays active until the
        # worker has actually stopped, so a follow-up click can't silently
        # no-op against a winding-down worker ("Generating..." stuck state).
        self._worker.cancel()
        if getattr(self, '_cancel_poll', None) is None:
            self._cancel_poll = QTimer(self)
            self._cancel_poll.setInterval(100)
            self._cancel_poll.timeout.connect(self._poll_cancel_finished)
        self._cancel_poll.start()

    def _poll_cancel_finished(self) -> None:
        if self._worker_is_alive(self._worker):
            return
        poll = getattr(self, '_cancel_poll', None)
        if poll is not None:
            poll.stop()
            poll.deleteLater()
            self._cancel_poll = None
        self._set_generating(False)

    def _clear_worker(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _result_matches_selection(self, action: str) -> bool:
        """Return True if the stored result belongs to the selected card."""
        if self._gen_source_id is not None and self._selected_id != self._gen_source_id:
            QMessageBox.warning(
                self, 'Selection Changed',
                f'This result was generated for a different character.\n'
                f'Select that character again to {action}.',
            )
            return False
        return True

    @pyqtSlot(object)
    def _on_finished(self, result) -> None:
        self._set_generating(False)
        try:
            self._display_result(result)
        except Exception as e:
            box = self._active_result_box()
            box.setPlainText(f"Error displaying result: {e}")

    def _display_result(self, result) -> None:
        if isinstance(result, list):
            self._last_tags = result
            empty_msg = '(no missing tags)' if self._tags_mode == 'missing' else '(no tags)'
            self._tags_result_box.setPlainText(', '.join(result) if result else empty_msg)
            self._tags_save_btn.setEnabled(True)
            self._tags_clear_btn.setEnabled(True)

        elif isinstance(result, str):
            if self._active_mode == 'alt_greetings':
                self._last_alt_greeting = result
                self._alt_greet_result_box.setPlainText(result)
                self._alt_greet_save_btn.setEnabled(True)
                self._alt_greet_clear_btn.setEnabled(True)
            else:
                self._last_summary = result
                self._summary_result_box.setPlainText(result)
                self._summary_save_btn.setEnabled(True)
                self._summary_clear_btn.setEnabled(True)

        elif isinstance(result, dict) and 'fields' in result:
            fields = result['fields']
            data = result['result']
            self._last_fill_result = data
            self._last_fill_fields = list(fields)
            lines = [f"Generated {len(fields)} field(s):\n"]
            for field_name in fields:
                value = data.get(field_name, '(not returned)')
                if isinstance(value, list):
                    value = ', '.join(str(v) for v in value)
                lines.append(f"--- {field_name} ---\n{value}\n")
            self._fill_result_box.setPlainText('\n'.join(lines))
            self._fill_save_btn.setEnabled(True)
            self._fill_clear_btn.setEnabled(True)

    @pyqtSlot(str)
    def _on_error(self, error: str) -> None:
        self._set_generating(False)
        box = self._active_result_box()
        box.setPlainText(f"Error: {error}")
        short_msg = error.splitlines()[0] if error else 'Unknown error'
        QMessageBox.critical(self, 'Generation Error', short_msg)

    # ---- save / clear ----

    def _on_save_tags(self) -> None:
        if not self._last_tags:
            QMessageBox.warning(self, 'No Tags', 'No generated tags to save.')
            return
        if not self._result_matches_selection('save the tags'):
            return
        card = self._require_card()
        if not card:
            return
        tags = [str(t).strip().lower() for t in self._last_tags if t and str(t).strip()]
        if not tags:
            QMessageBox.warning(self, 'No Tags', 'No valid tags to save.')
            return
        if self._tags_mode == 'missing':
            card.tags = merge_tags(card.tags, tags)
        else:
            card.tags = tags
        card.token_count = count_card_tokens(card)
        try:
            self.db.update_card(self._selected_id, card)
            QMessageBox.information(self, 'Saved', f"Tags saved to '{card.name}'.")
            self.card_updated.emit(self._selected_id)
        except Exception as e:
            QMessageBox.critical(self, 'Error', f"Failed to save: {e}")

    def _on_save_summary(self) -> None:
        if not self._last_summary:
            QMessageBox.warning(self, 'No Summary', 'No generated summary to save.')
            return
        if not self._result_matches_selection('save the summary'):
            return
        card = self._require_card()
        if not card:
            return
        if card.creator_notes and card.creator_notes.strip():
            reply = QMessageBox.question(
                self, 'Overwrite Notes',
                f"This will replace the existing creator notes of '{card.name}'. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        summary = self._last_summary.strip()
        if len(summary) > 5000:
            summary = summary[:5000]
        card.creator_notes = summary
        card.token_count = count_card_tokens(card)
        try:
            self.db.update_card(self._selected_id, card)
            QMessageBox.information(self, 'Saved', f"Summary saved to creator notes of '{card.name}'.")
            self.card_updated.emit(self._selected_id)
        except Exception as e:
            QMessageBox.critical(self, 'Error', f"Failed to save: {e}")

    def _on_save_alt_greeting(self) -> None:
        if not self._last_alt_greeting:
            QMessageBox.warning(self, 'No Greeting', 'No generated alternate greeting to save.')
            return
        if not self._result_matches_selection('save the greeting'):
            return
        card = self._require_card()
        if not card:
            return
        greeting = self._last_alt_greeting.strip()
        if not greeting:
            QMessageBox.warning(self, 'No Greeting', 'The generated alternate greeting is empty.')
            return
        card.alternate_greetings = [g for g in card.alternate_greetings if g and g.strip()]
        card.alternate_greetings.append(greeting)
        card.token_count = count_card_tokens(card)
        try:
            self.db.update_card(self._selected_id, card)
            QMessageBox.information(self, 'Saved', f"Alternate greeting saved to '{card.name}'.")
            self.card_updated.emit(self._selected_id)
        except Exception as e:
            QMessageBox.critical(self, 'Error', f"Failed to save: {e}")

    def _on_save_fill(self) -> None:
        """Save generated fill results to the selected card."""
        if not self._last_fill_result:
            QMessageBox.warning(self, 'No Results', 'No fill results to save.')
            return
        if not self._result_matches_selection('save the generated fields'):
            return
        card = self._require_card()
        if not card:
            return
        try:
            data = self._last_fill_result
            # Only apply the fields the user actually requested — the model
            # may return extra keys (e.g. 'name') that must never be applied
            # silently.
            requested = self._last_fill_fields or list(data)
            applied = []
            for field_name in requested:
                if field_name not in data:
                    continue
                value = data[field_name]
                if field_name == 'tags' and isinstance(value, list):
                    card.tags = [str(t).strip().lower() for t in value if t]
                    applied.append('tags')
                elif isinstance(value, str) and hasattr(card, field_name):
                    setattr(card, field_name, value)
                    applied.append(field_name)
            if not applied:
                QMessageBox.warning(self, 'Nothing Saved', 'No valid fields were found in the results.')
                return
            card.token_count = count_card_tokens(card)
            self.db.update_card(self._selected_id, card)
            QMessageBox.information(self, 'Saved', f"Saved {len(applied)} field(s) to '{card.name}'.")
            self._on_fill_card_changed()
            self.card_updated.emit(self._selected_id)
        except Exception as e:
            QMessageBox.critical(self, 'Error', f"Failed to save: {e}")

    def _on_clear_tags(self) -> None:
        self._last_tags = None
        self._tags_mode = 'replace'
        self._tags_save_btn.setEnabled(False)
        self._tags_clear_btn.setEnabled(False)
        self._tags_result_box.clear()

    def _on_clear_summary(self) -> None:
        self._last_summary = None
        self._summary_save_btn.setEnabled(False)
        self._summary_clear_btn.setEnabled(False)
        self._summary_result_box.clear()

    def _on_clear_alt_greeting(self) -> None:
        self._last_alt_greeting = None
        self._alt_greet_save_btn.setEnabled(False)
        self._alt_greet_clear_btn.setEnabled(False)
        self._alt_greet_result_box.clear()

    def _on_clear_fill(self) -> None:
        self._last_fill_result = None
        self._last_fill_fields = None
        self._fill_save_btn.setEnabled(False)
        self._fill_clear_btn.setEnabled(False)
        self._fill_result_box.clear()

    def _clear_all_results(self) -> None:
        """Discard all in-memory results when switching to a different card."""
        self._last_tags = None
        self._last_summary = None
        self._last_alt_greeting = None
        self._last_fill_result = None
        self._last_fill_fields = None
        self._tags_mode = 'replace'
        self._tags_save_btn.setEnabled(False)
        self._tags_clear_btn.setEnabled(False)
        self._summary_save_btn.setEnabled(False)
        self._summary_clear_btn.setEnabled(False)
        self._alt_greet_save_btn.setEnabled(False)
        self._alt_greet_clear_btn.setEnabled(False)
        self._fill_save_btn.setEnabled(False)
        self._fill_clear_btn.setEnabled(False)
        self._tags_result_box.clear()
        self._summary_result_box.clear()
        self._alt_greet_result_box.clear()
        self._fill_result_box.clear()

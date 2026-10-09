from __future__ import annotations

import logging
import time
import traceback
import uuid
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.app_paths import data_dir
from src.ai_client import AIClient, APIPreset, _extract_json
from src.ai_prompts import build_character_from_answers, build_wizard_question_prompts
from src.card_models import CharacterCard
from src.card_parser import write_chara_card_dual
from src.database import LibraryDatabase, sanitize_filename
from src.token_counter import count_card_tokens

logger = logging.getLogger(__name__)

_STEPS: list[tuple[str, str, str]] = [
    ('name', 'Name', "the character's name (fit it to the setting and tone)"),
    ('appearance', 'Appearance', "the character's appearance (build, age, features, clothing, and distinctive details)"),
    ('personality', 'Personality', "the character's personality (traits, values, motivations, flaws, and speech patterns)"),
    ('scenario', 'Backstory / Scenario', "the character's backstory and scenario (history, current situation, relationships, and setting)"),
    ('first_mes', 'First Message', "the character's opening first message (tone, voice, actions, and greeting situation)"),
    ('extra', 'Extra Details', 'any additional details (relationships, goals, secrets, abilities, or world-building)'),
]


class _StreamWorker(QThread):
    chunk = pyqtSignal(str)
    # Named ``completed`` so the built-in QThread.finished stays available
    # for lifetime management (deleteLater).
    completed = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, client: AIClient, system: str, user: str, parent=None):
        super().__init__(parent)
        self.client = client
        self.system = system
        self.user = user
        self._cancel = False

    def cancel(self):
        self._cancel = True
        self.client.cancel()

    def run(self):
        try:
            if self._cancel:
                return
            parts: list[str] = []
            for piece in self.client.generate(self.system, self.user, stream=True):
                if self._cancel:
                    return
                parts.append(piece)
                self.chunk.emit(piece)
            if not self._cancel:
                self.completed.emit(''.join(parts))
        except Exception as e:
            if not self._cancel:
                self.error.emit(f"{e}\n{traceback.format_exc()}")


class CharacterCreationWizard(QDialog):
    library_changed = pyqtSignal()

    def __init__(self, db: LibraryDatabase, preset: APIPreset, parent=None):
        super().__init__(parent)
        self.db = db
        self._preset = preset
        self._worker: _StreamWorker | None = None
        # Every live worker, not just the newest. A superseded worker keeps
        # running until its network read times out, and the dialog must never be
        # destroyed while any of them is alive (that aborts the process), so
        # they all have to be tracked and waited on.
        self._workers: list[_StreamWorker] = []
        self._phase: str = 'ask'  # 'ask' | 'card'
        self._step_index = 0
        self._answers: dict[str, str] = {key: '' for key, *_ in _STEPS}
        self._questions: dict[str, str] = {key: '' for key, *_ in _STEPS}
        self._image_path: str | None = None
        self._generated_card: CharacterCard | None = None

        self.setWindowTitle('Create New Character')
        self.resize(760, 600)

        layout = QHBoxLayout(self)

        # Step list.
        self._step_list = QListWidget()
        self._step_list.setFixedWidth(180)
        for _key, title, _ in _STEPS:
            self._step_list.addItem(QListWidgetItem(title))
        self._step_list.addItem(QListWidgetItem('Character Image'))
        self._step_list.addItem(QListWidgetItem('Review & Generate'))
        layout.addWidget(self._step_list)

        right = QVBoxLayout()

        self._pages = QStackedWidget()
        right.addWidget(self._pages, 1)

        # Text chat pages.
        self._chat_logs: list[QTextEdit] = []
        self._inputs: list[QLineEdit] = []
        self._send_btns: list[QPushButton] = []
        for key, title, _ in _STEPS:
            page = QWidget()
            pl = QVBoxLayout(page)
            pl.setContentsMargins(0, 0, 0, 0)
            title_lbl = QLabel(title)
            title_lbl.setStyleSheet('font-weight: bold; color: #e0e0e0;')
            pl.addWidget(title_lbl)
            chat = QTextEdit()
            chat.setReadOnly(True)
            pl.addWidget(chat, 1)
            input_row = QHBoxLayout()
            inp = QLineEdit()
            inp.setPlaceholderText('Type your answer here...')
            inp.returnPressed.connect(lambda k=key: self._submit_answer(k))
            input_row.addWidget(inp, 1)
            send = QPushButton('Send')
            send.clicked.connect(lambda checked=False, k=key: self._submit_answer(k))
            input_row.addWidget(send)
            pl.addLayout(input_row)
            self._chat_logs.append(chat)
            self._inputs.append(inp)
            self._send_btns.append(send)
            self._pages.addWidget(page)

        # Image page.
        self._image_page = QWidget()
        ipl = QVBoxLayout(self._image_page)
        ipl.setContentsMargins(0, 0, 0, 0)
        img_title = QLabel('Character Image')
        img_title.setStyleSheet('font-weight: bold; color: #e0e0e0;')
        ipl.addWidget(img_title)
        img_note = QLabel('Upload an image to use as the character avatar (optional).')
        img_note.setStyleSheet('color: #aaa; ')
        img_note.setWordWrap(True)
        ipl.addWidget(img_note)
        upload_btn = QPushButton('Upload Image...')
        upload_btn.clicked.connect(self._on_upload_image)
        ipl.addWidget(upload_btn)
        self._image_preview = QLabel('No image selected')
        self._image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_preview.setMinimumHeight(240)
        self._image_preview.setStyleSheet('color: #888; border: 1px dashed #444;')
        ipl.addWidget(self._image_preview, 1)
        clear_btn = QPushButton('Clear Image')
        clear_btn.clicked.connect(self._on_clear_image)
        ipl.addWidget(clear_btn)
        self._pages.addWidget(self._image_page)

        # Review page.
        self._review_page = QWidget()
        rpl = QVBoxLayout(self._review_page)
        rpl.setContentsMargins(0, 0, 0, 0)
        rev_title = QLabel('Review & Generate')
        rev_title.setStyleSheet('font-weight: bold; color: #e0e0e0;')
        rpl.addWidget(rev_title)
        self._review_label = QLabel('')
        self._review_label.setWordWrap(True)
        self._review_label.setStyleSheet('color: #ccc; ')
        rpl.addWidget(self._review_label)
        self._generate_btn = QPushButton('Generate Card')
        self._generate_btn.clicked.connect(self._on_generate_card)
        rpl.addWidget(self._generate_btn)
        self._streaming_label = QLabel('Generating...')
        self._streaming_label.setStyleSheet('color: #6cb6ff; font-style: italic;')
        self._streaming_label.setVisible(False)
        rpl.addWidget(self._streaming_label)
        self._card_preview = QTextEdit()
        self._card_preview.setReadOnly(True)
        self._card_preview.setMinimumHeight(160)
        rpl.addWidget(self._card_preview, 1)
        self._import_btn = QPushButton('Import to Library')
        self._import_btn.setEnabled(False)
        self._import_btn.clicked.connect(self._on_import)
        rpl.addWidget(self._import_btn)
        self._pages.addWidget(self._review_page)

        # Navigation.
        nav_row = QHBoxLayout()
        self._back_btn = QPushButton('Back')
        self._back_btn.clicked.connect(self._on_back)
        nav_row.addWidget(self._back_btn)
        self._next_btn = QPushButton('Next')
        self._next_btn.clicked.connect(self._on_next)
        nav_row.addWidget(self._next_btn)
        nav_row.addStretch()
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self._on_cancel)
        nav_row.addWidget(cancel_btn)
        right.addLayout(nav_row)

        layout.addLayout(right, 1)

        self._go_to_step(0)

    # ---- step navigation ----

    def _total_steps(self) -> int:
        return len(_STEPS) + 2

    def _go_to_step(self, index: int) -> None:
        self._step_index = index
        self._step_list.setCurrentRow(index)
        if index < len(_STEPS):
            self._pages.setCurrentIndex(index)
            key = _STEPS[index][0]
            if not self._questions[key]:
                self._ask_question(key)
        elif index == len(_STEPS):
            self._pages.setCurrentIndex(len(_STEPS))
            self._update_image_preview()
        else:
            self._pages.setCurrentIndex(len(_STEPS) + 1)
            self._refresh_review()

        self._back_btn.setEnabled(index > 0)
        # "Finish" used to be disabled at the review step while its handler
        # sat there as dead code; the review page has its own Generate/Import
        # actions, so the button is simply not shown there.
        self._next_btn.setVisible(index < self._total_steps() - 1)

    def _on_back(self) -> None:
        if self._step_index > 0:
            self._go_to_step(self._step_index - 1)

    def _on_next(self) -> None:
        if self._step_index >= self._total_steps() - 1:
            return
        self._go_to_step(self._step_index + 1)

    # ---- interview chat ----

    def _ask_question(self, key: str) -> None:
        index = next(i for i, s in enumerate(_STEPS) if s[0] == key)
        topic = _STEPS[index][2]
        context = '\n'.join(
            f"{title}: {self._answers[k]}" for k, title, _ in _STEPS if self._answers[k]
        ) or '(nothing yet)'
        system, user_prompt = build_wizard_question_prompts(topic, context)
        self._phase = 'ask'
        self._begin_stream()
        self._append_speaker(self._chat_logs[index], 'Assistant')
        self._start_stream(system, user_prompt, target_index=index)

    def _submit_answer(self, key: str) -> None:
        index = next(i for i, s in enumerate(_STEPS) if s[0] == key)
        answer = self._inputs[index].text().strip()
        if not answer:
            return
        self._answers[key] = answer
        self._chat_logs[index].append(f"\nYou: {answer}")
        self._inputs[index].clear()

    # ---- image ----

    def _on_upload_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select Character Image', '',
            'Image Files (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*)',
        )
        if not path:
            return
        self._image_path = path
        self._update_image_preview()

    def _on_clear_image(self) -> None:
        self._image_path = None
        self._update_image_preview()

    def _update_image_preview(self) -> None:
        if self._image_path and Path(self._image_path).exists():
            pixmap = QPixmap(self._image_path)
            if not pixmap.isNull():
                self._image_preview.setPixmap(
                    pixmap.scaled(
                        220, 300,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                self._image_preview.setText('')
                return
        self._image_preview.setPixmap(QPixmap())
        self._image_preview.setText('No image selected')

    # ---- review & generate ----

    def _refresh_review(self) -> None:
        lines = []
        for key, title, _ in _STEPS:
            value = self._answers.get(key, '').strip()
            lines.append(f"{title}: {value if value else '(not provided)'}")
        img = Path(self._image_path).name if self._image_path else '(none)'
        lines.append(f"Image: {img}")
        self._review_label.setText('\n'.join(lines))

    def _on_generate_card(self) -> None:
        if self._worker_is_alive(self._worker):
            return
        if not self._answers.get('name', '').strip():
            QMessageBox.warning(self, 'Generate', 'Please provide at least a name before generating.')
            return
        system, user = build_character_from_answers(self._answers)
        self._phase = 'card'
        self._generate_btn.setEnabled(False)
        self._import_btn.setEnabled(False)
        self._card_preview.clear()
        self._streaming_label.setVisible(True)
        self._start_stream(system, user, target_index=None)

    # ---- streaming plumbing ----

    def _begin_stream(self) -> None:
        self._streaming_label.setVisible(True)

    @staticmethod
    def _worker_is_alive(worker) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            # The C++ QThread was already destroyed by deleteLater.
            return False

    def _live_workers(self) -> list:
        """Every still-running stream worker, pruning finished ones."""
        self._workers = [w for w in self._workers if self._worker_is_alive(w)]
        return list(self._workers)

    def _start_stream(self, system: str, user: str, target_index: int | None) -> None:
        # Cancel any in-flight stream but keep tracking it: it is parented to
        # this dialog and cannot be destroyed while running, and dropping the
        # reference would let reject() tear the dialog down underneath it.
        for old in self._live_workers():
            old.cancel()
        client = AIClient(self._preset)
        worker = _StreamWorker(client, system, user, self)
        self._worker = worker
        self._workers.append(worker)
        # Handlers ignore signals from superseded workers so a cancelled run
        # cannot still overwrite the step it was populating.
        worker.chunk.connect(lambda text, idx=target_index: self._on_chunk(text, idx, worker))
        worker.completed.connect(
            lambda full, idx=target_index: self._on_stream_finished(full, idx, worker))
        worker.error.connect(lambda err: self._on_stream_error(err, worker))
        # Cleanup on the built-in signal so cancelled runs are still deleted.
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _append_speaker(self, chat: QTextEdit, speaker: str) -> None:
        cursor = chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(f"{speaker}: ")
        chat.setTextCursor(cursor)

    def _on_chunk(self, text: str, target_index: int | None, worker=None) -> None:
        if worker is not None and worker is not self._worker:
            return
        if self._phase == 'ask' and target_index is not None:
            self._stream_chunk(self._chat_logs[target_index], text)
        elif self._phase == 'card':
            cursor = self._card_preview.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(text)
            self._card_preview.setTextCursor(cursor)
            self._card_preview.ensureCursorVisible()

    @staticmethod
    def _stream_chunk(chat: QTextEdit, text: str) -> None:
        cursor = chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        chat.setTextCursor(cursor)
        chat.ensureCursorVisible()

    def _on_stream_finished(self, full: str, target_index: int | None, worker=None) -> None:
        if worker is not None and worker is not self._worker:
            return
        self._streaming_label.setVisible(False)
        if self._phase == 'ask' and target_index is not None:
            key = _STEPS[target_index][0]
            self._questions[key] = full.strip()
            self._chat_logs[target_index].append('')
        elif self._phase == 'card':
            self._on_card_generated(full)

    def _on_card_generated(self, full: str) -> None:
        self._generate_btn.setEnabled(True)
        try:
            raw = _extract_json(full)
            if raw is None:
                raise ValueError("AI did not return valid JSON")
            card = CharacterCard.from_spec_dict(raw)
            card.token_count = count_card_tokens(card)
            self._generated_card = card
            self._card_preview.setPlainText(
                f"Name: {card.name}\n"
                f"Tags: {', '.join(card.tags)}\n"
                f"Tokens: {card.token_count:,}\n\n"
                f"Description:\n{card.description}\n\n"
                f"Personality:\n{card.personality or '(none)'}\n\n"
                f"Scenario:\n{card.scenario or '(none)'}"
            )
            self._import_btn.setEnabled(True)
        except Exception as e:
            self._card_preview.setPlainText(f"Generation error: {e}")
            logger.warning("Wizard card generation error: %s", e)

    def _on_stream_error(self, error: str, worker=None) -> None:
        if worker is not None and worker is not self._worker:
            return
        self._streaming_label.setVisible(False)
        self._generate_btn.setEnabled(True)
        short = error.splitlines()[0] if error else 'Unknown error'
        QMessageBox.critical(self, 'Error', short)

    # ---- import ----

    def _on_import(self) -> None:
        card = self._generated_card
        if card is None:
            return

        dup = self.db.find_duplicate(card)
        if dup:
            reply = QMessageBox.question(
                self, 'Possible Duplicate',
                f"A card named '{card.name}' by '{card.creator or 'Unknown'}' already exists.\n"
                f"Import anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        save_dir = data_dir() / 'generated'
        save_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(card.name or 'Untitled')
        save_path = save_dir / f"{safe_name}_{uuid.uuid4().hex[:8]}.png"

        try:
            import io
            from PIL import Image
            from src import vault
            if self._image_path and Path(self._image_path).exists():
                img = Image.open(self._image_path).convert('RGBA')
            else:
                img = Image.new('RGBA', (400, 600), (40, 40, 60, 255))
            buf = io.BytesIO()
            img.save(buf, 'PNG')
            vault.write_bytes(save_path, buf.getvalue())
            write_chara_card_dual(save_path, save_path, card.to_spec_dict())
            card.source_path = str(save_path)
            char_id = self.db.import_card(save_path)
            if char_id:
                self._imported = True
                QMessageBox.information(self, 'Imported', f"'{card.name}' imported to library.")
                self.library_changed.emit()
                self.accept()
            else:
                QMessageBox.warning(self, 'Error', 'Failed to import character.')
        except Exception as e:
            logger.exception("Failed to import generated character")
            QMessageBox.critical(self, 'Error', f"Failed to import: {e}")

    def _shutdown_worker(self) -> bool:
        """Cooperatively cancel any running stream worker.

        Returns True when it is safe to close the dialog; a worker that is
        still finishing (mid network read) keeps the dialog open, because
        destroying a live QThread aborts the process.  *All* live workers are
        checked, not just the newest - a superseded worker stays alive until its
        request times out.
        """
        live = self._live_workers()
        if not live:
            return True
        for worker in live:
            worker.cancel()
        # Wait on each with a shared budget so a cancelled-but-stuck read can
        # never hold the dialog open indefinitely. The budget is elapsed time,
        # not "1ms per worker".
        budget_end = time.monotonic() + 3.0
        for worker in live:
            if worker.isRunning():
                remaining_ms = int(max(0.0, (budget_end - time.monotonic()) * 1000))
                if remaining_ms <= 0 or not worker.wait(remaining_ms):
                    self._streaming_label.setText('Cancelling… please close again in a moment.')
                    self._streaming_label.setVisible(True)
                    return False
        return True

    def _on_cancel(self) -> None:
        self.reject()

    def reject(self) -> None:
        # Also covers Esc / window-close: never tear down a live worker.
        # A generated-but-unimported card is thrown away here, so confirm.
        card = self._generated_card
        if card is not None and not getattr(self, '_imported', False):
            if QMessageBox.question(
                self, 'Discard Character',
                f"'{card.name}' has been generated but not imported.\n\n"
                'Discard it?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
        if self._shutdown_worker():
            super().reject()

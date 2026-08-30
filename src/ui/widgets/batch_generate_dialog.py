from __future__ import annotations

import json as _json
import logging
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from src.ai_client import AIClient, APIPreset
from src.card_models import CharacterCard
from src.card_parser import read_card_data
from src.database import LibraryDatabase
from src.tag_ops import merge_tags
from src.token_counter import count_card_tokens

logger = logging.getLogger(__name__)


def _entry_has_tags(entry: dict) -> bool:
    tags_str = entry.get('tags', '')
    if not tags_str:
        return False
    try:
        tags = _json.loads(tags_str)
        return isinstance(tags, list) and len(tags) > 0
    except (_json.JSONDecodeError, TypeError):
        return False


def _entry_has_summary(entry: dict) -> bool:
    notes = entry.get('creator_notes', '')
    return bool(notes and len(notes.strip()) > 10)


class _BatchWorker(QThread):
    progress = pyqtSignal(int, int, str)
    card_done = pyqtSignal(int, str, object)
    # Named ``completed`` so the built-in QThread.finished stays available
    # for lifetime management (deleteLater).
    completed = pyqtSignal(int, int)
    error = pyqtSignal(str)

    def __init__(self, client: AIClient, db: LibraryDatabase,
                 do_tags: bool, do_summary: bool, do_missing_tags: bool,
                 extra: str = '', length: str = '', tag_count: str = '', parent=None):
        super().__init__(parent)
        self.client = client
        self.db = db
        self.do_tags = do_tags
        self.do_summary = do_summary
        self.do_missing_tags = do_missing_tags
        self.extra = extra
        self.length = length
        self.tag_count = tag_count
        self._cancel = False

    def cancel(self):
        self._cancel = True
        self.client.cancel()

    def _update_with_retry(self, char_id: int, card: CharacterCard) -> bool:
        """Write the card with retries. Returns False when cancelled mid-write
        (the caller must not count or announce the card as done); raises when
        the database stayed locked through every retry."""
        import sqlite3
        last_err: Exception | None = None
        for attempt in range(3):
            if self._cancel:
                return False
            try:
                self.db.update_card(char_id, card)
                return True
            except sqlite3.OperationalError as e:
                last_err = e
                logger.warning("DB locked on update (attempt %d): %s", attempt + 1, e)
                import time
                time.sleep(0.5 * (attempt + 1))
        raise last_err  # type: ignore[misc]

    def run(self):
        try:
            self._run()
        except Exception as e:
            logger.exception("Batch generate failed")
            self.error.emit(str(e))

    def _run(self):
        entries = self.db.get_all()
        to_process = []
        for entry in entries:
            needs_tags = self.do_tags and not _entry_has_tags(entry)
            needs_summary = self.do_summary and not _entry_has_summary(entry)
            needs_missing = self.do_missing_tags and _entry_has_tags(entry)
            if needs_tags or needs_summary or needs_missing:
                to_process.append((entry, needs_tags, needs_summary, needs_missing))

        total = len(to_process)
        done_count = 0
        error_count = 0

        for i, (entry, needs_tags, needs_summary, needs_missing) in enumerate(to_process):
            if self._cancel:
                break

            name = entry['name']
            self.progress.emit(i + 1, total, name)

            source = entry.get('source_path', '')
            if not source or not Path(source).exists():
                self.card_done.emit(entry['id'], name, 'Skipped: file missing')
                error_count += 1
                continue

            try:
                raw = read_card_data(source)
                if not raw:
                    self.card_done.emit(entry['id'], name, 'Skipped: no card data')
                    error_count += 1
                    continue

                card = CharacterCard.from_spec_dict(raw, source)
                card.token_count = count_card_tokens(card)
                modified = False

                if needs_tags and not self._cancel:
                    tags = self.client.generate_tags(card, extra=self.extra, length=self.tag_count)
                    if self._cancel:
                        break
                    card.tags = tags
                    modified = True

                if needs_missing and not self._cancel:
                    before = len(card.tags)
                    new_tags = self.client.generate_missing_tags(
                        card, extra=self.extra, length=self.tag_count,
                    )
                    if self._cancel:
                        break
                    card.tags = merge_tags(card.tags, new_tags)
                    added = len(card.tags) - before
                    modified = True

                if needs_summary and not self._cancel:
                    summary = self.client.generate_summary(card, extra=self.extra, length=self.length)
                    if self._cancel:
                        break
                    card.creator_notes = summary
                    modified = True

                if modified and not self._cancel:
                    if not self._update_with_retry(entry['id'], card):
                        # Cancelled mid-write: neither count nor announce this
                        # card — it was not updated.
                        break
                    parts = []
                    if needs_tags:
                        parts.append(f"tags={card.tags}")
                    if needs_missing:
                        parts.append(f"added {added} tag(s)")
                    if needs_summary:
                        parts.append(f"summary={len(card.creator_notes)} chars")
                    self.card_done.emit(entry['id'], name, ', '.join(parts))
                    done_count += 1

            except Exception as e:
                self.card_done.emit(entry['id'], name, f'Error: {e}')
                error_count += 1

        self.completed.emit(done_count, error_count)


class BatchGenerateDialog(QDialog):
    library_changed = pyqtSignal()

    def __init__(self, db: LibraryDatabase, preset: APIPreset, parent=None):
        super().__init__(parent)
        self.db = db
        self._preset = preset
        self._worker: _BatchWorker | None = None
        self._was_cancelled = False

        self.setWindowTitle('Batch Generate')
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        info = QLabel('Generate tags and/or summaries for all cards that are missing them.')
        info.setStyleSheet('color: #aaa; font-size: 12px;')
        info.setWordWrap(True)
        layout.addWidget(info)

        checks = QHBoxLayout()
        self._tags_check = QCheckBox('Generate missing tags')
        self._tags_check.setChecked(True)
        checks.addWidget(self._tags_check)
        self._summary_check = QCheckBox('Generate missing summaries')
        self._summary_check.setChecked(True)
        checks.addWidget(self._summary_check)
        self._missing_tags_check = QCheckBox('Add missing tags to existing cards')
        self._missing_tags_check.setChecked(False)
        checks.addWidget(self._missing_tags_check)
        checks.addStretch()
        layout.addLayout(checks)

        len_row = QHBoxLayout()
        len_row.addWidget(QLabel('Target word count for summaries (optional):'))
        self._length_edit = QLineEdit()
        self._length_edit.setMaximumWidth(100)
        self._length_edit.setPlaceholderText('e.g., 100')
        len_row.addWidget(self._length_edit)
        len_row.addStretch()
        layout.addLayout(len_row)

        tag_row = QHBoxLayout()
        tag_row.addWidget(QLabel('Target number of tags (optional):'))
        self._tag_count_edit = QLineEdit()
        self._tag_count_edit.setMaximumWidth(100)
        self._tag_count_edit.setPlaceholderText('e.g., 10')
        tag_row.addWidget(self._tag_count_edit)
        tag_row.addStretch()
        layout.addLayout(tag_row)

        layout.addWidget(QLabel('Additional Instructions (optional):'))
        self._extra_edit = QLineEdit()
        self._extra_edit.setPlaceholderText('e.g., Focus on genre tags, keep summaries brief')
        layout.addWidget(self._extra_edit)

        self._progress_label = QLabel('')
        self._progress_label.setStyleSheet('color: #aaa; font-size: 12px;')
        self._progress_label.setVisible(False)
        layout.addWidget(self._progress_label)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        btn_row = QHBoxLayout()
        self._generate_btn = QPushButton('Generate')
        self._generate_btn.clicked.connect(self._on_generate)
        btn_row.addWidget(self._generate_btn)
        self._cancel_btn = QPushButton('Cancel')
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addStretch()
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self._on_close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    @staticmethod
    def _worker_is_alive(worker) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            # The C++ QThread was already destroyed by deleteLater.
            return False

    def _on_generate(self) -> None:
        if self._worker_is_alive(self._worker):
            QMessageBox.warning(self, 'Batch Generate', 'A batch is already running.')
            return
        do_tags = self._tags_check.isChecked()
        do_summary = self._summary_check.isChecked()
        do_missing_tags = self._missing_tags_check.isChecked()
        if not do_tags and not do_summary and not do_missing_tags:
            QMessageBox.warning(self, 'Batch Generate', 'Select at least one option.')
            return

        self._was_cancelled = False
        self._generate_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._progress_label.setVisible(True)
        self._progress_label.setText('Scanning library...')

        client = AIClient(self._preset)
        self._worker = _BatchWorker(
            client, self.db, do_tags, do_summary, do_missing_tags,
            extra=self._extra_edit.text().strip(),
            length=self._length_edit.text().strip(),
            tag_count=self._tag_count_edit.text().strip(),
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.card_done.connect(self._on_card_done)
        self._worker.completed.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        # Cleanup on the built-in signal so cancelled runs are still deleted.
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_progress(self, current: int, total: int, name: str) -> None:
        self._progress.setMaximum(total)
        self._progress.setValue(current)
        self._progress_label.setText(f"Processing {current}/{total}: {name}")

    def _on_card_done(self, char_id: int, name: str, result: str) -> None:
        logger.info("Batch: %s -> %s", name, result)

    def _on_finished(self, done: int, errors: int) -> None:
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        if getattr(self, '_was_cancelled', False):
            # The user cancelled: don't pop a "Batch Complete!" box or fire
            # library_changed for a run they aborted.
            self._progress_label.setText(f'Cancelled. {done} updated, {errors} errors.')
            return
        self._progress_label.setText(f"Done! {done} updated, {errors} errors.")
        if done or errors:
            QMessageBox.information(
                self, 'Batch Complete',
                f"Processed {done + errors} cards.\n{done} updated, {errors} errors.",
            )
        else:
            QMessageBox.information(
                self, 'Batch Complete',
                'All cards already have the selected fields.',
            )
        self.library_changed.emit()

    def _on_error(self, error: str) -> None:
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress_label.setVisible(False)
        self._progress.setVisible(False)
        short_msg = error.splitlines()[0] if error else 'Unknown error'
        QMessageBox.critical(self, 'Batch Generate Error', short_msg)

    def _on_cancel(self) -> None:
        self._was_cancelled = True
        if self._worker_is_alive(self._worker):
            # Cooperative cancel only; the worker cleans itself up via the
            # built-in finished signal (no GUI-thread wait here).
            self._worker.cancel()
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.setVisible(False)
        self._progress_label.setVisible(False)

    def _shutdown_worker(self) -> bool:
        """Cooperatively cancel any running worker.

        Returns True when it is safe to close the dialog. When a short wait
        isn't enough (the worker may be mid-file-write), the dialog stays
        open — destroying it while the QThread runs would abort the process.
        """
        worker = self._worker
        if not self._worker_is_alive(worker):
            return True
        self._was_cancelled = True
        worker.cancel()
        if worker.wait(3000):
            return True
        self._progress.setVisible(True)
        self._progress_label.setVisible(True)
        self._progress_label.setText('Cancelling… please close again in a moment.')
        return False

    def _on_close(self) -> None:
        if self._shutdown_worker():
            self.reject()

    def reject(self) -> None:
        # Also covers Esc / window-close: never tear down a live worker.
        if self._shutdown_worker():
            super().reject()

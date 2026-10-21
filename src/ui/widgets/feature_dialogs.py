"""Dialogs for the newer library features.

* :class:`CardLinterDialog` - quality check for the selected card.
* :class:`UsageStatsDialog` - token/cost statistics and the session ledger.
* :class:`BatchEditDialog` - apply one field change to many cards.
* :class:`CrashReportDialog` - create a redacted crash-report bundle.
* :class:`SemanticSearchDialog` - "cards like this one" over embeddings.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {'error': 0, 'warning': 1, 'info': 2}
_SEVERITY_LABEL = {'error': 'Error', 'warning': 'Warning', 'info': 'Note'}


class CardLinterDialog(QDialog):
    """Show the lint issues of one character card."""

    def __init__(self, card, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Card Check')
        self.resize(680, 460)
        self._card = card

        from src.card_linter import lint_card, lint_summary

        layout = QVBoxLayout(self)
        self._summary = QLabel('')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(['Severity', 'Problem', 'Suggestion'])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self._tree, 1)

        try:
            issues = lint_card(card)
        except Exception as exc:
            logger.exception("Card lint failed")
            issues = []
            self._summary.setText(f'The card could not be checked: {exc}')
        else:
            self._summary.setText(lint_summary(issues))

        for issue in sorted(issues, key=lambda i: _SEVERITY_ORDER.get(i.severity, 9)):
            item = QTreeWidgetItem([
                _SEVERITY_LABEL.get(issue.severity, issue.severity),
                issue.message,
                issue.hint or issue.field,
            ])
            self._tree.addTopLevelItem(item)
        for col in range(3):
            self._tree.resizeColumnToContents(col)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class UsageStatsDialog(QDialog):
    """Token usage and cost statistics."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Usage & Cost')
        self.resize(720, 520)

        from src.usage_ledger import (
            daily_totals, load_entries, session_stats, total_by_card,
            total_by_provider, total_by_role, total_tokens,
        )

        layout = QVBoxLayout(self)
        entries = load_entries()
        total = total_tokens(entries)
        self._summary = QLabel(
            f'{len(entries)} recorded request(s) - {total:,} tokens in total.',
        )
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._price_in = QDoubleSpinBox()
        self._price_in.setRange(0.0, 1000.0)
        self._price_in.setDecimals(4)
        self._price_in.setSingleStep(0.01)
        self._price_in.setValue(0.0)
        self._price_out = QDoubleSpinBox()
        self._price_out.setRange(0.0, 1000.0)
        self._price_out.setDecimals(4)
        self._price_out.setSingleStep(0.01)
        self._price_out.setValue(0.0)
        form = QFormLayout()
        form.addRow('Input price per 1k tokens:', self._price_in)
        form.addRow('Output price per 1k tokens:', self._price_out)
        layout.addLayout(form)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(['Group', 'Key', 'Tokens'])
        self._tree.setRootIsDecorated(False)
        layout.addWidget(self._tree, 1)

        self._cost = QLabel('')
        layout.addWidget(self._cost)

        def _show(entries, groups, title):
            self._tree.clear()
            root = QTreeWidgetItem([title, '', ''])
            for key, value in sorted(groups.items(), key=lambda kv: -kv[1]):
                root.addChild(QTreeWidgetItem(['', str(key), f'{value:,}']))
            self._tree.addTopLevelItem(root)
            self._tree.expandAll()
            for col in range(3):
                self._tree.resizeColumnToContents(col)

        _show(entries, total_by_role(entries), 'By role')
        self._by = QComboBox()
        self._by.addItems(['By role', 'By card', 'By provider', 'By day', 'By session'])
        self._by.currentIndexChanged.connect(
            lambda _i: self._switch(entries, total_by_card, total_by_provider,
                                    daily_totals, session_stats),
        )
        layout.addWidget(self._by)
        self._entries = entries
        self._switch(entries, total_by_card, total_by_provider,
                     daily_totals, session_stats)

        def _recalc():
            from src.usage_ledger import estimate_cost
            cost = sum(
                estimate_cost(e, self._price_in.value(), self._price_out.value())
                for e in entries
            )
            self._cost.setText(f'Estimated cost: {cost:,.4f}')

        self._price_in.valueChanged.connect(_recalc)
        self._price_out.valueChanged.connect(_recalc)
        _recalc()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _switch(self, entries, by_card, by_provider, by_day, by_session) -> None:
        choice = self._by.currentText()
        self._tree.clear()
        if choice == 'By card':
            groups, title = by_card(entries), 'By card'
        elif choice == 'By provider':
            groups, title = by_provider(entries), 'By provider'
        elif choice == 'By day':
            groups, title = by_day(entries), 'By day'
        elif choice == 'By session':
            rows = by_session(entries)
            root = QTreeWidgetItem(['By session', '', ''])
            for row in rows:
                child = QTreeWidgetItem([
                    '', str(row.get('session_id', '?')), f"{row.get('tokens', 0):,}",
                ])
                child.setToolTip(0, f"{row.get('messages', 0)} request(s)")
                root.addChild(child)
            self._tree.addTopLevelItem(root)
            self._tree.expandAll()
            for col in range(3):
                self._tree.resizeColumnToContents(col)
            return
        else:
            groups, title = {}, 'By role'
            from src.usage_ledger import total_by_role
            groups = total_by_role(entries)
        root = QTreeWidgetItem([title, '', ''])
        for key, value in sorted(groups.items(), key=lambda kv: -kv[1]):
            root.addChild(QTreeWidgetItem(['', str(key), f'{value:,}']))
        self._tree.addTopLevelItem(root)
        self._tree.expandAll()
        for col in range(3):
            self._tree.resizeColumnToContents(col)


class BatchEditDialog(QDialog):
    """Apply one field change to many cards at once."""

    FIELDS = [
        ('creator', 'Creator'),
        ('creator_notes', 'Creator notes'),
        ('tags', 'Tags (comma separated)'),
        ('system_prompt', 'System prompt'),
        ('character_version', 'Character version'),
    ]

    def __init__(self, card_ids, db, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Batch Edit')
        self.resize(520, 260)
        self._card_ids = list(card_ids)
        self._db = db

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f'Apply a change to {len(self._card_ids)} selected card(s). '
            f'Empty values are skipped.',
        ))
        form = QFormLayout()
        self._field = QComboBox()
        for key, label in self.FIELDS:
            self._field.addItem(label, key)
        form.addRow('Field:', self._field)
        self._value = QLineEdit()
        form.addRow('New value:', self._value)
        self._append = QCheckBox('Append to the existing value instead of replacing it')
        form.addRow('', self._append)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self) -> int:
        """Apply the change; returns the number of cards updated."""
        key = self._field.currentData()
        value = self._value.text()
        if not value.strip():
            return 0
        changed = 0
        for card_id in self._card_ids:
            row = self._db.get_by_id(card_id)
            if row is None:
                continue
            if self._append.isChecked():
                existing = str(row.get(key) or '')
                new_value = f'{existing}\n{value}' if existing else value
            else:
                new_value = value
            try:
                self._db.update_card_fields(card_id, {key: new_value})
            except Exception:
                logger.exception("Could not update card %s", card_id)
                continue
            changed += 1
        return changed


class CrashReportDialog(QDialog):
    """Create a redacted crash-report bundle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Create Crash Report')
        self.resize(520, 240)

        layout = QVBoxLayout(self)
        note = QLabel(
            'The bundle contains the app version, the recent log lines and the '
            'library counts. API keys, passwords and decryption keys are '
            'removed before it is written.',
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self._path = QLineEdit()
        layout.addWidget(self._path)
        self._status = QLabel('')
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        row = QHBoxLayout()
        self._btn = QPushButton('Create Report...')
        self._btn.clicked.connect(self._on_create)
        row.addWidget(self._btn)
        row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        row.addWidget(buttons)
        layout.addLayout(row)

    def _on_create(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        dest, _ = QFileDialog.getSaveFileName(
            self, 'Save Crash Report', 'st-explorer-crash-report.zip',
            'Zip Archives (*.zip)',
        )
        if not dest:
            return
        if not dest.lower().endswith('.zip'):
            dest += '.zip'
        try:
            from src.crash_report import write_crash_report
            path = write_crash_report(dest)
        except Exception as exc:
            logger.exception("Could not create the crash report")
            self._status.setText(f'The report could not be created: {exc}')
            return
        self._status.setText(f'Crash report written to {Path(path)}')


class PairingsDialog(QDialog):
    """Manage saved card + persona pairings.

    A pairing remembers which persona a character is usually chatted with, so
    the user can jump back into the right voice in one click.
    """

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Persona Pairings')
        self.resize(620, 420)
        self._db = db

        from src.persona_pairings import list_pairings

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            'Saved pairings keep a character together with the persona it is '
            'usually chatted with.',
        ))
        self._list = QListWidget()
        layout.addWidget(self._list, 1)

        row = QHBoxLayout()
        self._delete_btn = QPushButton('Delete')
        self._delete_btn.clicked.connect(self._on_delete)
        row.addWidget(self._delete_btn)
        row.addStretch()
        self._export_btn = QPushButton('Export...')
        self._export_btn.clicked.connect(self._on_export)
        row.addWidget(self._export_btn)
        self._import_btn = QPushButton('Import...')
        self._import_btn.clicked.connect(self._on_import)
        row.addWidget(self._import_btn)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._pairings = list_pairings()
        for pairing in self._pairings:
            item = QListWidgetItem(
                f'{pairing.card_name}  <->  {pairing.persona_name or "(no persona)"}',
            )
            item.setData(Qt.ItemDataRole.UserRole, pairing.id)
            if pairing.notes:
                item.setToolTip(pairing.notes)
            self._list.addItem(item)

    def selected_pairing_id(self) -> str:
        item = self._list.currentItem()
        if item is None:
            return ''
        return str(item.data(Qt.ItemDataRole.UserRole) or '')

    def _on_delete(self) -> None:
        pairing_id = self.selected_pairing_id()
        if not pairing_id:
            return
        from src.persona_pairings import delete_pairing
        if delete_pairing(pairing_id):
            self._list.takeItem(self._list.currentRow())

    def _on_export(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        from src.persona_pairings import export_pairings
        dest, _ = QFileDialog.getSaveFileName(
            self, 'Export Pairings', 'pairings.json', 'JSON (*.json)',
        )
        if not dest:
            return
        try:
            Path(dest).write_text(export_pairings(), encoding='utf-8')
        except OSError as exc:
            QMessageBox.warning(self, 'Export', f'The export failed: {exc}')

    def _on_import(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        from src.persona_pairings import import_pairings
        path, _ = QFileDialog.getOpenFileName(
            self, 'Import Pairings', '', 'JSON (*.json);;All files (*)',
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding='utf-8')
        except OSError as exc:
            QMessageBox.warning(self, 'Import', f'The import failed: {exc}')
            return
        count = import_pairings(text)
        QMessageBox.information(self, 'Import', f'{count} pairing(s) imported.')


class _IndexWorker(QThread):
    """Builds the embedding index off the GUI thread.

    Embedding a whole library takes a few seconds (and a number of network
    requests), which would freeze the window.
    """

    ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, cards, parent=None):
        super().__init__(parent)
        self._cards = cards

    def run(self) -> None:
        try:
            from src.semantic_search import build_index

            self.ready.emit(build_index(self._cards))
        except Exception as exc:
            logger.exception("The embedding index could not be built")
            self.failed.emit(str(exc))


class SemanticSearchDialog(QDialog):
    """Search the library semantically ("cards like this one")."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Semantic Search')
        self.resize(640, 480)
        self._db = db
        # The embedding index is built once and reused; rebuilding it for
        # every query would re-embed the whole library each time.
        self._index = None
        self._index_worker: _IndexWorker | None = None
        self._cards: list = []

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self._query = QLineEdit()
        self._query.setPlaceholderText('Describe the character you are looking for...')
        self._query.returnPressed.connect(self._on_search)
        row.addWidget(self._query, 1)
        self._btn = QPushButton('Search')
        self._btn.clicked.connect(self._on_search)
        row.addWidget(self._btn)
        layout.addLayout(row)

        self._status = QLabel(
            'Semantic search needs an embedding provider. Without one the '
            'search falls back to keyword matching.',
        )
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._list = QListWidget()
        layout.addWidget(self._list, 1)

    def _on_search(self) -> None:
        query = self._query.text().strip()
        if not query:
            return
        from src.semantic_search import semantic_or_keyword_search
        try:
            if not self._cards:
                self._cards = self._db.get_all()
            results = semantic_or_keyword_search(
                query, self._cards, index=self._index, limit=50,
            )
        except Exception as exc:
            logger.exception("Semantic search failed")
            self._status.setText(f'The search failed: {exc}')
            return
        self._list.clear()
        for card_id, score in results:
            row = self._db.get_by_id(card_id)
            if row is None:
                continue
            item = QListWidgetItem(f'{row.get("name", "?")}  ({score:.2f})')
            item.setData(Qt.ItemDataRole.UserRole, card_id)
            self._list.addItem(item)
        if not results:
            self._status.setText('No matching cards.')
        else:
            self._status.setText(f'{len(results)} result(s).')
        # The index is built in the background and used by the next search.
        self._ensure_index()

    def _ensure_index(self) -> None:
        """Build the embedding index (once) on a worker thread."""
        if self._index is not None or self._index_worker is not None:
            return
        if not self._cards:
            self._cards = self._db.get_all()
        self._status.setText('Building the embedding index...')
        self._index_worker = _IndexWorker(self._cards, self)
        self._index_worker.ready.connect(self._on_index_ready)
        self._index_worker.failed.connect(self._on_index_failed)
        self._index_worker.finished.connect(self._on_index_finished)
        self._index_worker.start()

    def _on_index_ready(self, index) -> None:
        self._index = index
        count = len(getattr(index, 'vectors', {}) or {})
        if count:
            self._status.setText(
                f'{count} card(s) indexed - the next search uses semantic ranking.'
            )
        else:
            self._status.setText(
                'No embeddings were available; the search falls back to keyword '
                'matching.'
            )

    def _on_index_failed(self, message: str) -> None:
        self._status.setText(
            f'The embedding index could not be built ({message}); the search '
            f'falls back to keyword matching.'
        )

    def _on_index_finished(self) -> None:
        self._index_worker = None

    def closeEvent(self, event) -> None:
        worker = self._index_worker
        if worker is not None and worker.isRunning():
            worker.wait(5000)
        super().closeEvent(event)

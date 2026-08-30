from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QDialog,
    QWidget,
)

from src.lorebook_sync import resolve_worlds_dir
from src.settings_manager import (
    load_st_characters_path,
    load_st_worlds_path,
    save_st_characters_path,
    save_st_worlds_path,
)
from src.sillytavern_sync import detect_st_installs

logger = logging.getLogger(__name__)


def _example_paths() -> tuple[str, str]:
    """Return OS-appropriate example (characters, worlds) paths for hints."""
    if sys.platform == 'win32':
        return (
            r'C:\SillyTavern\data\default-user\characters',
            r'C:\SillyTavern\data\default-user\worlds (auto-derived from the characters path)',
        )
    if sys.platform == 'darwin':
        return (
            '/Applications/SillyTavern/data/default-user/characters',
            '/Applications/SillyTavern/data/default-user/worlds (auto-derived from the characters path)',
        )
    return (
        '~/SillyTavern/data/default-user/characters',
        '~/SillyTavern/data/default-user/worlds (auto-derived from the characters path)',
    )


class STConfigDialog(QDialog):
    """Dialog for configuring the SillyTavern characters directory path."""

    settings_changed = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Configure SillyTavern')
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)

        path_group = QGroupBox('SillyTavern Characters Directory')
        path_layout = QVBoxLayout(path_group)

        row = QHBoxLayout()
        example_chars, example_worlds = _example_paths()
        self._path_edit = QLineEdit(load_st_characters_path())
        self._path_edit.setPlaceholderText(f'e.g. {example_chars}')
        row.addWidget(self._path_edit)
        browse_btn = QPushButton('Browse...')
        browse_btn.clicked.connect(self._on_browse)
        row.addWidget(browse_btn)
        path_layout.addLayout(row)

        detect_btn = QPushButton('Auto-Detect')
        detect_btn.clicked.connect(self._on_detect)
        path_layout.addWidget(detect_btn)

        self._candidates_list = QListWidget()
        self._candidates_list.setVisible(False)
        self._candidates_list.itemSelectionChanged.connect(self._on_candidate_selected)
        path_layout.addWidget(self._candidates_list)

        self._status_label = QLabel('')
        self._status_label.setStyleSheet('font-size: 12px; color: #aaa;')
        path_layout.addWidget(self._status_label)

        layout.addWidget(path_group)

        worlds_group = QGroupBox('SillyTavern World Info Directory (Lorebooks)')
        worlds_layout = QVBoxLayout(worlds_group)

        worlds_row = QHBoxLayout()
        saved_worlds = load_st_worlds_path()
        self._worlds_auto = not saved_worlds
        initial_chars = self._path_edit.text().strip()
        derived = resolve_worlds_dir(initial_chars) if initial_chars else ''
        self._worlds_edit = QLineEdit(saved_worlds or str(derived))
        self._worlds_edit.setPlaceholderText(f'e.g. {example_worlds}')
        self._worlds_edit.textEdited.connect(self._on_worlds_edited)
        worlds_row.addWidget(self._worlds_edit)
        worlds_browse_btn = QPushButton('Browse...')
        worlds_browse_btn.clicked.connect(self._on_browse_worlds)
        worlds_row.addWidget(worlds_browse_btn)
        worlds_layout.addLayout(worlds_row)

        worlds_hint = QLabel(
            'World-info books sync as JSON files between this directory and the '
            'Lorebooks tab. Leave this as-is to follow the characters directory.'
        )
        worlds_hint.setWordWrap(True)
        worlds_hint.setStyleSheet('font-size: 11px; color: #888;')
        worlds_layout.addWidget(worlds_hint)

        layout.addWidget(worlds_group)

        # Changing the characters path refreshes the auto-derived worlds path
        # until the user edits it manually.
        self._path_edit.textChanged.connect(self._on_characters_changed)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton('Save')
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._update_status()

    def _on_browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, 'Select SillyTavern Characters Directory')
        if path:
            self._path_edit.setText(path)
            self._update_status()

    def _on_characters_changed(self, text: str) -> None:
        """Keep the auto-derived worlds path following the characters path."""
        if not self._worlds_auto:
            return
        chars = text.strip()
        self._worlds_edit.setText(
            str(resolve_worlds_dir(chars)) if chars else ''
        )

    def _on_worlds_edited(self, _text: str) -> None:
        self._worlds_auto = False

    def _on_browse_worlds(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, 'Select SillyTavern World Info Directory',
        )
        if path:
            self._worlds_edit.setText(path)
            self._worlds_auto = False

    def _on_detect(self) -> None:
        candidates = detect_st_installs()
        self._candidates_list.clear()
        if not candidates:
            self._status_label.setText('No SillyTavern installations found.')
            self._candidates_list.setVisible(False)
            return
        for c in candidates:
            self._candidates_list.addItem(str(c))
        self._candidates_list.setVisible(True)
        self._status_label.setText(f'Found {len(candidates)} candidate(s).')

    def _on_candidate_selected(self) -> None:
        item = self._candidates_list.currentItem()
        if item:
            self._path_edit.setText(item.text())
            self._update_status()

    def _update_status(self) -> None:
        path = self._path_edit.text().strip()
        if not path:
            self._status_label.setText('No path set.')
            return
        p = Path(path)
        if not p.is_dir():
            self._status_label.setText('Directory does not exist.')
            return
        # Cheap file count only: parsing every PNG here (list_st_characters)
        # would freeze the dialog on large libraries. The full validated
        # count runs in the background via the status-bar widget.
        png_count = len(list(p.glob('*.png')))
        self._status_label.setText(
            f'Directory valid — {png_count} PNG file(s) found.'
        )

    def _save(self) -> None:
        path = self._path_edit.text().strip()
        worlds = self._worlds_edit.text().strip()
        save_st_characters_path(path)
        save_st_worlds_path(worlds)
        self.settings_changed.emit(path)
        logger.info("ST characters path saved: %s (worlds: %s)", path, worlds)
        self.accept()

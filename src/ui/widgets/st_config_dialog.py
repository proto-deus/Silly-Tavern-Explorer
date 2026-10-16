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
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QDialog,
    QWidget,
)

from src.gallery_store import resolve_user_images_dir
from src.lorebook_sync import resolve_worlds_dir
from src.settings_manager import (
    load_st_characters_path,
    load_st_user_images_path,
    load_st_worlds_path,
    clear_st_settings,
    save_st_characters_path,
    save_st_user_images_path,
    save_st_worlds_path,
)
from src.sillytavern_sync import detect_st_installs

logger = logging.getLogger(__name__)


def _example_paths() -> tuple[str, str, str]:
    """Return OS-appropriate example (characters, worlds, user-images) hints."""
    if sys.platform == 'win32':
        return (
            r'C:\SillyTavern\data\default-user\characters',
            r'C:\SillyTavern\data\default-user\worlds (auto-derived from the characters path)',
            r'C:\SillyTavern\data\default-user\user\images (auto-derived from the characters path)',
        )
    if sys.platform == 'darwin':
        return (
            '/Applications/SillyTavern/data/default-user/characters',
            '/Applications/SillyTavern/data/default-user/worlds (auto-derived from the characters path)',
            '/Applications/SillyTavern/data/default-user/user/images (auto-derived from the characters path)',
        )
    return (
        '~/SillyTavern/data/default-user/characters',
        '~/SillyTavern/data/default-user/worlds (auto-derived from the characters path)',
        '~/SillyTavern/data/default-user/user/images (auto-derived from the characters path)',
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
        example_chars, example_worlds, example_images = _example_paths()
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
        self._status_label.setStyleSheet('color: #aaa;')
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
        worlds_hint.setStyleSheet('color: #888;')
        worlds_layout.addWidget(worlds_hint)

        layout.addWidget(worlds_group)

        images_group = QGroupBox('SillyTavern User Images (Expression Images)')
        images_layout = QVBoxLayout(images_group)

        images_row = QHBoxLayout()
        saved_images = load_st_user_images_path()
        self._images_auto = not saved_images
        derived_images = resolve_user_images_dir(initial_chars) if initial_chars else ''
        self._images_edit = QLineEdit(saved_images or str(derived_images))
        self._images_edit.setPlaceholderText(f'e.g. {example_images}')
        self._images_edit.textEdited.connect(self._on_images_edited)
        images_row.addWidget(self._images_edit)
        images_browse_btn = QPushButton('Browse...')
        images_browse_btn.clicked.connect(self._on_browse_images)
        images_row.addWidget(images_browse_btn)
        images_layout.addLayout(images_row)

        images_hint = QLabel(
            'Expression images live in per-character subfolders here '
            '(e.g. <name>/happy.png) and are shared with SillyTavern. '
            'Leave this as-is to follow the characters directory.'
        )
        images_hint.setWordWrap(True)
        images_hint.setStyleSheet('color: #888;')
        images_layout.addWidget(images_hint)

        layout.addWidget(images_group)

        # Changing the characters path refreshes the auto-derived worlds and
        # user-images paths until the user edits them manually.
        self._path_edit.textChanged.connect(self._on_characters_changed)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        forget_btn = QPushButton('Forget Setup')
        forget_btn.setToolTip(
            'Clear the saved SillyTavern paths. Cards keep their links, so you '
            'can re-point at a moved install without re-linking.',
        )
        forget_btn.clicked.connect(self._on_forget)
        btn_row.addWidget(forget_btn)
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

    def _on_forget(self) -> None:
        """Clear the saved ST paths without touching the cards' ST links."""
        reply = QMessageBox.question(
            self, 'Forget SillyTavern Setup',
            'Clear the saved SillyTavern paths?\n\n'
            'Your cards keep their SillyTavern links, so re-pointing at a moved '
            'install will not require re-linking.',
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        clear_st_settings()
        self._path_edit.clear()
        self._worlds_edit.clear()
        self._images_edit.clear()
        self._worlds_auto = True
        self._images_auto = True
        self._update_status()

    def _on_characters_changed(self, text: str) -> None:
        """Keep the auto-derived worlds/user-images paths following the characters path."""
        if self._worlds_auto:
            chars = text.strip()
            self._worlds_edit.setText(
                str(resolve_worlds_dir(chars)) if chars else ''
            )
        if self._images_auto:
            chars = text.strip()
            self._images_edit.setText(
                str(resolve_user_images_dir(chars)) if chars else ''
            )

    def _on_worlds_edited(self, _text: str) -> None:
        self._worlds_auto = False

    def _on_images_edited(self, _text: str) -> None:
        self._images_auto = False

    def _on_browse_worlds(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, 'Select SillyTavern World Info Directory',
        )
        if path:
            self._worlds_edit.setText(path)
            self._worlds_auto = False

    def _on_browse_images(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, 'Select SillyTavern User Images Directory',
        )
        if path:
            self._images_edit.setText(path)
            self._images_auto = False

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
        images = self._images_edit.text().strip()
        save_st_characters_path(path)
        save_st_worlds_path(worlds)
        save_st_user_images_path(images)
        self.settings_changed.emit(path)
        logger.info(
            "ST characters path saved: %s (worlds: %s, user images: %s)",
            path, worlds, images,
        )
        self.accept()

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from src.app_paths import data_dir
from src.card_models import CharacterCard
from src.card_parser import write_chara_card_dual
from src.database import LibraryDatabase, sanitize_filename

logger = logging.getLogger(__name__)


class NewCharacterDialog(QDialog):
    """Collect a name and optional image, then create a blank character card."""

    card_added = pyqtSignal(int)

    def __init__(self, db: LibraryDatabase, parent=None):
        super().__init__(parent)
        self.db = db
        self._image_path: str | None = None

        self.setWindowTitle('New Character')
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        name_label = QLabel('Character name:')
        name_label.setStyleSheet('font-size: 12px; font-weight: bold; color: #ccc;')
        layout.addWidget(name_label)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText('Enter a name for the new character')
        layout.addWidget(self._name_edit)

        img_label = QLabel('Character image (optional):')
        img_label.setStyleSheet('font-size: 12px; font-weight: bold; color: #ccc; margin-top: 8px;')
        layout.addWidget(img_label)

        self._image_preview = QLabel('No image selected')
        self._image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_preview.setMinimumHeight(180)
        self._image_preview.setStyleSheet('color: #888; border: 1px dashed #444;')
        layout.addWidget(self._image_preview)

        img_row = QHBoxLayout()
        upload_btn = QPushButton('Upload Image...')
        upload_btn.clicked.connect(self._on_upload_image)
        img_row.addWidget(upload_btn)
        clear_btn = QPushButton('Clear')
        clear_btn.clicked.connect(self._on_clear_image)
        img_row.addWidget(clear_btn)
        img_row.addStretch()
        layout.addLayout(img_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Create')
        buttons.accepted.connect(self._on_create)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
                        160, 220,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                self._image_preview.setText('')
                return
        self._image_preview.setPixmap(QPixmap())
        self._image_preview.setText('No image selected')

    def _on_create(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, 'New Character', 'Please enter a character name.')
            return

        card = CharacterCard(name=name)

        save_dir = data_dir() / 'generated'
        save_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(name or 'Untitled')
        save_path = save_dir / f"{safe_name}_{uuid.uuid4().hex[:8]}.png"

        try:
            from PIL import Image
            if self._image_path and Path(self._image_path).exists():
                img = Image.open(self._image_path).convert('RGBA')
            else:
                img = Image.new('RGBA', (400, 600), (40, 40, 60, 255))
            img.save(save_path, 'PNG')
            write_chara_card_dual(save_path, save_path, card.to_spec_dict())
            card.source_path = str(save_path)
            char_id = self.db.import_card(save_path)
            if char_id:
                self.card_added.emit(char_id)
                self.accept()
            else:
                QMessageBox.warning(self, 'New Character', 'Failed to create character.')
        except Exception as e:
            logger.exception("Failed to create new character")
            QMessageBox.critical(self, 'New Character', f"Failed to create character: {e}")

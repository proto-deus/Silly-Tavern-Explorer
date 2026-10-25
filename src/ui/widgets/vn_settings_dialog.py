"""Per-character visual-novel settings (backdrop pin and overrides).

Everything here is optional: each dropdown has a "Global default" entry that
stores nothing, so the character follows whatever Settings -> Test says.
The dialog only *collects* values - saving them (``card_settings``) and
re-applying them to the Test tab is the caller's job.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

#: Sentinel stored for "follow the global setting" (never written to disk).
GLOBAL = None

_ON_OFF = (('Global default', GLOBAL), ('On', True), ('Off', False))


class VNSettingsDialog(QDialog):
    """Edit one character's visual-novel overrides.

    *gallery_rows* are the character's ``gallery_images`` rows and
    *backdrop_rows* the shared ``backdrop_images`` rows (dicts with
    ``file_name`` / ``caption``) so the pinned-backdrop combo can list them
    without the dialog touching the database itself.
    """

    def __init__(
        self,
        char_name: str,
        gallery_rows: list[dict] | None = None,
        overrides: dict | None = None,
        parent: QWidget | None = None,
        backdrop_rows: list[dict] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f'VN Settings \u2014 {char_name or "Character"}')
        self.setMinimumWidth(380)
        ov = dict(overrides or {})

        layout = QVBoxLayout(self)

        backdrop_group = QGroupBox('Backdrop')
        backdrop_form = QFormLayout(backdrop_group)

        self._backdrop_file = QComboBox()
        self._backdrop_file.addItem('None (use the global setting)', '')
        if backdrop_rows:
            self._backdrop_file.insertSeparator(1)
            for row in backdrop_rows:
                file_name = str(row.get('file_name') or '')
                if not file_name:
                    continue
                caption = str(row.get('caption') or '').strip() or Path(file_name).stem
                self._backdrop_file.addItem(f'{caption}  ({file_name})', file_name)
        if gallery_rows:
            # Separate the shared group from this character's gallery only
            # when both groups are present.
            if backdrop_rows:
                self._backdrop_file.insertSeparator(self._backdrop_file.count())
            for row in gallery_rows:
                file_name = str(row.get('file_name') or '')
                if not file_name:
                    continue
                caption = str(row.get('caption') or '').strip() or Path(file_name).stem
                self._backdrop_file.addItem(f'{caption}  ({file_name})', file_name)
        self._backdrop_file.setToolTip(
            'Pin a specific image as this character\'s starting backdrop.\n'
            'Lists the shared backdrops and this character\'s gallery images.\n'
            'Takes precedence over the global "Backdrop image" setting; '
            'picking an image in the chat (per chat) still overrides it.'
        )
        idx = self._backdrop_file.findData(str(ov.get('backdrop_file') or ''))
        self._backdrop_file.setCurrentIndex(idx if idx >= 0 else 0)
        backdrop_form.addRow('Pinned image:', self._backdrop_file)

        self._image_source = self._enum_field((
            ('Global default', GLOBAL),
            ('First gallery image', 'first'),
            ('Latest added', 'latest'),
            ('Random on open', 'random'),
        ), ov.get('vn_image_source', GLOBAL))
        backdrop_form.addRow('Backdrop image:', self._image_source)

        self._image_fit = self._enum_field((
            ('Global default', GLOBAL),
            ('Contain (letterbox)', 'contain'),
            ('Cover (crop to fill)', 'cover'),
            ('Stretch', 'stretch'),
        ), ov.get('vn_image_fit', GLOBAL))
        backdrop_form.addRow('Image fit:', self._image_fit)
        layout.addWidget(backdrop_group)

        chat_group = QGroupBox('Chat & Expressions')
        chat_form = QFormLayout(chat_group)

        self._advance = self._enum_field(_ON_OFF, ov.get('vn_advance_on_reply', GLOBAL))
        chat_form.addRow('Advance on reply:', self._advance)

        self._expression_auto = self._enum_field(_ON_OFF, ov.get('vn_expression_auto', GLOBAL))
        chat_form.addRow('Expression images:', self._expression_auto)

        self._backdrop_keywords = self._enum_field(
            _ON_OFF, ov.get('vn_backdrop_keywords', GLOBAL),
        )
        self._backdrop_keywords.setToolTip(
            'Switch the backdrop when a reply matches an image\'s keywords.'
        )
        chat_form.addRow('Keyword switching:', self._backdrop_keywords)
        layout.addWidget(chat_group)

        hint = QLabel(
            'Each option can follow the global Test-tab settings or override '
            'them for this character only.'
        )
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #888;')
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        reset_btn = buttons.addButton(
            'Reset to global defaults', QDialogButtonBox.ButtonRole.ResetRole,
        )
        reset_btn.clicked.connect(self._reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _enum_field(options, current) -> QComboBox:
        combo = QComboBox()
        for label, value in options:
            combo.addItem(label, value)
        idx = combo.findData(current if current is not None else GLOBAL)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        return combo

    def _reset(self) -> None:
        """Send every control back to "follow the global settings"."""
        for combo in (
            self._backdrop_file, self._image_source, self._image_fit,
            self._advance, self._expression_auto, self._backdrop_keywords,
        ):
            combo.setCurrentIndex(0)

    def values(self) -> dict:
        """Return the override dict (only keys the user actually set).

        ``backdrop_file`` is present only when an image is pinned - there is
        no global backdrop file, so "no pin" is simply the key's absence.
        """
        result: dict = {}
        file_name = self._backdrop_file.currentData()
        if file_name:
            result['backdrop_file'] = str(file_name)
        for key, combo in (
            ('vn_image_source', self._image_source),
            ('vn_image_fit', self._image_fit),
            ('vn_advance_on_reply', self._advance),
            ('vn_expression_auto', self._expression_auto),
            ('vn_backdrop_keywords', self._backdrop_keywords),
        ):
            value = combo.currentData()
            if value is not GLOBAL:
                result[key] = value
        return result

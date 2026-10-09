"""Startup password prompt for an encrypted library.

Shown by ``main.py`` before the database is opened whenever ``vault.json``
exists.  Unlocking decrypts ``library.db`` in place (``unseal_database``); a
wrong password never touches any file.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src import vault


class UnlockDialog(QDialog):
    """Modal password prompt; accepted only when the vault unlocks."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('ST Explorer - Locked')
        self.setModal(True)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        title = QLabel('This library is encrypted.')
        title.setStyleSheet('font-weight: bold; font-size: 14px;')
        layout.addWidget(title)

        hint = QLabel('Enter your password to unlock it.')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._error = QLabel('')
        self._error.setStyleSheet('color: #e05555;')
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        layout.addWidget(self._error)

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText('Password')
        self._password.returnPressed.connect(self._try_unlock)
        layout.addWidget(self._password)

        self._recovery_toggle = QCheckBox('Use recovery key instead')
        self._recovery_toggle.toggled.connect(self._on_mode_toggled)
        if not vault.get_vault().has_recovery:
            self._recovery_toggle.setVisible(False)
        layout.addWidget(self._recovery_toggle)

        buttons = QHBoxLayout()
        buttons.addStretch()
        exit_btn = QPushButton('Exit')
        exit_btn.clicked.connect(self.reject)
        buttons.addWidget(exit_btn)
        unlock_btn = QPushButton('Unlock')
        unlock_btn.setDefault(True)
        unlock_btn.clicked.connect(self._try_unlock)
        buttons.addWidget(unlock_btn)
        layout.addLayout(buttons)

        self._password.setFocus()

    def _on_mode_toggled(self, checked: bool) -> None:
        self._password.setPlaceholderText('Recovery key' if checked else 'Password')
        self._error.setVisible(False)

    def _try_unlock(self) -> None:
        # Never trim the password: Enable/Change Password store it verbatim,
        # so trimming here made a password with edge whitespace impossible to
        # type back at startup (permanent lockout without the recovery key).
        secret = self._password.text()
        if not secret:
            self._show_error('Please enter a value.')
            return
        if self._recovery_toggle.isChecked():
            ok = vault.get_vault().unlock_with_recovery(secret)
            what = 'recovery key'
        else:
            ok = vault.get_vault().unlock(secret)
            what = 'password'
        if ok:
            self.accept()
        else:
            self._show_error(f'Incorrect {what}. Please try again.')
            self._password.selectAll()
            self._password.setFocus()

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.setVisible(True)

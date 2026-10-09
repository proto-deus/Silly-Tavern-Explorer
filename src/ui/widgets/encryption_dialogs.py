"""Encryption management dialogs: enable, change password, disable, recovery key.

These run their file migration (seal/unseal of every data file) on a
background :class:`_SealWorker` thread so a large library never freezes the
UI, mirroring the ImportWorker pattern.  The live ``library.db`` is left
alone during migration: it is sealed automatically when the app exits and
unsealed at the next startup.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from src import vault

logger = logging.getLogger(__name__)

# Parentless workers are held here so they cannot be garbage-collected while
# run() executes, even if the dialog that started them is destroyed first.
_LIVE_SEAL_WORKERS: list['_SealWorker'] = []


class _SealWorker(QThread):
    """Seal or unseal every data file, reporting progress."""

    progress = pyqtSignal(int, int, str)   # done, total, filename
    finished_ok = pyqtSignal(int, int)     # changed, failed
    failed = pyqtSignal(str)

    def __init__(self, seal: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self._seal = seal
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            files = list(vault.iter_data_files())
            total = len(files)
            changed = 0
            failed = 0
            for i, path in enumerate(files):
                if self._cancelled:
                    break
                try:
                    if self._seal:
                        did = vault.seal_file(path)
                    else:
                        did = vault.unseal_file(path)
                    if did:
                        changed += 1
                except (OSError, vault.VaultError) as exc:
                    failed += 1
                    logger.error('Cannot process %s: %s', path, exc)
                self.progress.emit(i + 1, total, Path(path).name)
            self.finished_ok.emit(changed, failed)
        except Exception as exc:
            logger.exception('Seal migration failed')
            self.failed.emit(str(exc))


def _password_strength(text: str) -> str:
    """Rough strength label for the setup hint (not a policy enforcer)."""
    if len(text) < 8:
        return 'weak (use at least 8 characters)'
    if len(text) < 12 and not any(c.isdigit() for c in text):
        return 'fair'
    return 'strong'


class EnableEncryptionDialog(QDialog):
    """Set the vault password (and optionally mint a recovery key)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Enable Data Encryption')
        self.setMinimumWidth(420)
        self._recovery_key: str | None = None

        layout = QVBoxLayout(self)
        intro = QLabel(
            'Choose a password to encrypt your library. Every card, chat '
            'session, lorebook, and the database will be encrypted on disk.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.textChanged.connect(self._on_password_changed)
        form.addRow('Password:', self._password)

        self._confirm = QLineEdit()
        self._confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('Confirm password:', self._confirm)

        self._strength = QLabel('')
        form.addRow('', self._strength)
        layout.addLayout(form)

        self._recovery = QCheckBox('Also generate a recovery key (recommended)')
        self._recovery.setChecked(True)
        layout.addWidget(self._recovery)

        warning = QLabel(
            'If you forget your password'
            ' (and lose the recovery key), your data cannot be recovered. '
            'There is no back door.'
        )
        warning.setWordWrap(True)
        warning.setStyleSheet('color: #c06020;')
        layout.addWidget(warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_password_changed(self, text: str) -> None:
        self._strength.setText(f'Strength: {_password_strength(text)}')

    def _validate(self) -> None:
        password = self._password.text()
        if not password:
            QMessageBox.warning(self, 'Enable Encryption', 'Please enter a password.')
            return
        if password != self._confirm.text():
            QMessageBox.warning(self, 'Enable Encryption', 'The passwords do not match.')
            return
        self.accept()

    def get_values(self) -> tuple[str, bool]:
        """Return ``(password, with_recovery)`` after accept."""
        return self._password.text(), self._recovery.isChecked()


class RecoveryKeyDialog(QDialog):
    """One-time display of a generated recovery key."""

    def __init__(self, recovery_key: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Recovery Key')
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        intro = QLabel(
            'This is your recovery key. It can unlock your library if you '
            'forget your password. Save it somewhere safe \u2014 it is shown '
            'only once.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._key_box = QPlainTextEdit(recovery_key)
        self._key_box.setReadOnly(True)
        self._key_box.setMaximumHeight(80)
        layout.addWidget(self._key_box)

        copy_btn = QPushButton('Copy to Clipboard')
        copy_btn.clicked.connect(self._copy_key)
        row = QHBoxLayout()
        row.addWidget(copy_btn)
        row.addStretch()
        layout.addLayout(row)

        self._saved = QCheckBox('I have saved the recovery key')
        layout.addWidget(self._saved)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self._validate)
        layout.addWidget(buttons)

    def _copy_key(self) -> None:
        self._key_box.selectAll()
        self._key_box.copy()

    def _validate(self) -> None:
        if not self._saved.isChecked():
            QMessageBox.warning(
                self, 'Recovery Key',
                'Please confirm that you have saved the recovery key.',
            )
            return
        self.accept()


class ChangePasswordDialog(QDialog):
    """Change the vault password (instant re-wrap, no file re-encryption)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Change Password')
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._current = QLineEdit()
        self._current.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('Current password:', self._current)
        self._new = QLineEdit()
        self._new.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('New password:', self._new)
        self._confirm = QLineEdit()
        self._confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('Confirm new password:', self._confirm)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        if not self._new.text():
            QMessageBox.warning(self, 'Change Password', 'Please enter a new password.')
            return
        if self._new.text() != self._confirm.text():
            QMessageBox.warning(self, 'Change Password', 'The new passwords do not match.')
            return
        self.accept()

    def get_values(self) -> tuple[str, str]:
        return self._current.text(), self._new.text()


class DisableEncryptionDialog(QDialog):
    """Confirm and decrypt the library (requires the current password)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Disable Data Encryption')
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        intro = QLabel(
            'Your library will be decrypted and stored in plain text. '
            'Enter your password to confirm.'
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('Password:', self._password)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        if not vault.get_vault().verify_password(self._password.text()):
            QMessageBox.warning(self, 'Disable Encryption', 'Incorrect password.')
            return
        self.accept()

    def get_password(self) -> str:
        return self._password.text()


class MigrationDialog(QDialog):
    """Progress dialog for the seal/unseal migration pass."""

    def __init__(self, seal: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Encrypting Library' if seal else 'Decrypting Library')
        self.setMinimumWidth(420)
        self._seal = seal

        layout = QVBoxLayout(self)
        self._label = QLabel('Preparing...')
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

        from PyQt6.QtWidgets import QProgressBar
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        layout.addWidget(self._bar)

        self._worker = _SealWorker(seal)
        # Drop wrappers of workers that already finished so the registry
        # cannot grow without bound across repeated migrations.
        _LIVE_SEAL_WORKERS[:] = [w for w in _LIVE_SEAL_WORKERS if not w.isFinished()]
        _LIVE_SEAL_WORKERS.append(self._worker)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self._cancel)
        self._buttons = buttons
        layout.addWidget(buttons)

        self._changed = 0
        self._failed = 0
        self._done = False
        self._completed = False
        self._cancelled = False

    def start(self) -> None:
        self._worker.start()

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self._bar.setMaximum(max(1, total))
        self._bar.setValue(done)
        self._label.setText(f'{done}/{total} \u2014 {name}')

    def _on_finished(self, changed: int, failed: int) -> None:
        self._changed = changed
        self._failed = failed
        self._done = True
        self._completed = True
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._failed = -1
        self._done = True
        self._completed = False
        QMessageBox.critical(self, 'Migration Failed', message)
        self.reject()

    def _cancel(self) -> None:
        self._cancelled = True
        self._worker.cancel()
        self._done = True
        self.reject()

    def closeEvent(self, event) -> None:
        self._cancelled = True
        self._worker.cancel()
        super().closeEvent(event)

    def cleanup(self) -> None:
        self._worker.cancel()
        # A live QThread must outlive its Python wrapper: if the worker is
        # still winding down after a cancel, wait for it (cooperative cancel
        # makes this short) and only release it once it has finished.
        self._worker.wait(10000)
        if not self._worker.isFinished():
            # Still running: keep it in the registry so it is not GC'd.
            return
        try:
            _LIVE_SEAL_WORKERS.remove(self._worker)
        except ValueError:
            pass
        self._worker.setParent(None)

    @property
    def completed_ok(self) -> bool:
        """True only when the migration ran to completion with no failures.

        A cancelled dialog used to report ``(0, 0)`` and was treated as a
        success - which deleted the vault envelope while files were still
        encrypted. Callers must check this before acting on ``result``.
        """
        return self._completed and not self._cancelled and self._failed == 0

    @property
    def result(self) -> tuple[int, int]:
        """``(changed, failed)`` once the migration finished.

        Note that a cancelled migration also reports ``(0, 0)`` or a partial
        count; check :attr:`completed_ok` before treating it as success.
        """
        return self._changed, self._failed

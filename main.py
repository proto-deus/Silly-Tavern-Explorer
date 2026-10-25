import logging
import os
import sys
from pathlib import Path

from src.app_paths import data_dir


def _setup_logging() -> None:
    # The file handler only when the app log is enabled (Settings > App);
    # the console stream is always attached.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        from src.settings_manager import load_logging_enabled
        enabled = load_logging_enabled()
    except Exception:
        enabled = True
    if enabled:
        log_dir = data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.FileHandler(log_dir / 'app.log', encoding='utf-8'),
        )

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=handlers,
    )


def _install_crash_hook() -> None:
    """Log unhandled exceptions instead of letting PyQt abort the process.

    PyQt6 treats an exception that escapes a Qt slot as fatal: it calls
    ``sys.excepthook`` and, when that is still the default hook, aborts the
    process outright - leaving nothing in the app log.  Installing this
    hook keeps the app alive and every such failure visible in the log.
    """
    logger = logging.getLogger(__name__)

    def _log(etype, value, tb) -> None:
        logger.critical('Unhandled exception', exc_info=(etype, value, tb))

    def _hook(etype, value, tb) -> None:
        _log(etype, value, tb)
        try:
            from PyQt6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                # Non-modal show(): exec() could re-enter slot dispatch.
                QMessageBox(
                    QMessageBox.Icon.Critical,
                    'ST Explorer',
                    f'An unexpected error occurred:\n\n{value}\n\n'
                    'See the log for details: ~/.st-explorer/app.log',
                ).show()
        except Exception:
            pass

    sys.excepthook = _hook

    import threading

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        _log(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = _thread_hook


def _check_dependencies() -> list[str]:
    """Check that all required packages are installed.

    Returns a list of missing package names.
    """
    import importlib.util
    # cryptography encrypts the API keys on macOS/Linux - without it they fall
    # back to plain text silently - so it is required, not optional.
    # symspellpy backs the text-field spell checker (its frequency dictionary
    # ships with the package).
    required = ['PyQt6', 'PIL', 'tiktoken', 'requests', 'markdown', 'cryptography', 'symspellpy']
    missing = []
    for name in required:
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    return missing


def _show_missing_dep_error(missing: list[str]) -> None:
    """Show a user-friendly error about missing dependencies."""
    packages = ', '.join(missing)
    message = (
        f"The following required packages are not installed:\n\n"
        f"  {packages}\n\n"
        f"Please install them by running:\n\n"
        f"  pip install -r requirements.txt\n"
    )
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, message, 'ST Explorer - Missing Dependencies', 0x10,
        )
        return
    # macOS/Linux: a GUI box is only possible when PyQt6 itself is
    # installed; otherwise fall back to the terminal.
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        sys.stderr.write(message)
        return
    # The reference must be kept: without it a freshly created QApplication
    # could be garbage-collected before the message box is shown.
    app = QApplication.instance() or QApplication([sys.argv[0]])
    QMessageBox.critical(None, 'ST Explorer - Missing Dependencies', message)


def _start_auto_backup() -> None:
    """Run the automatic library backup in the background, if enabled.

    The portable config controls it::

        [backup]
        auto_backup = true
        keep = 5

    The backup runs on a daemon thread so the window appears immediately; a
    failure is logged and never surfaces to the user (a scheduled backup must
    not interrupt anything).
    """
    try:
        from src.portable_config import get
        if not bool(get('backup.auto_backup', False)):
            return
        keep = max(1, min(50, int(get('backup.keep', 5))))
    except Exception:
        return

    import threading

    def _run() -> None:
        try:
            from src.library_backup import run_scheduled_backup
            result = run_scheduled_backup(keep=keep)
            if result:
                logging.getLogger(__name__).info(
                    "Automatic backup written to %s", result.get('path', '?'),
                )
        except Exception:
            logging.getLogger(__name__).exception("The automatic backup failed")

    thread = threading.Thread(target=_run, name='auto-backup', daemon=True)
    thread.start()


def main():
    # Point tiktoken at the vocab files bundled by build.spec so token
    # counting works offline in the frozen executable (must be set before
    # tiktoken is first imported).
    if getattr(sys, 'frozen', False):
        bundled_cache = Path(getattr(sys, '_MEIPASS', '.')) / 'tiktoken_cache'
        if bundled_cache.is_dir():
            os.environ.setdefault('TIKTOKEN_CACHE_DIR', str(bundled_cache))

    _setup_logging()
    _install_crash_hook()
    logger = logging.getLogger(__name__)

    missing = _check_dependencies()
    if missing:
        _show_missing_dep_error(missing)
        sys.exit(1)

    from PyQt6.QtWidgets import QApplication, QMessageBox
    from src.database import LibraryDatabase
    from src.single_instance import SingleInstance
    from src.ui.main_window import MainWindow
    from src.ui.styles import apply_theme
    from src.settings_manager import load_font_size

    app = QApplication(sys.argv)
    app.setApplicationName('ST Explorer')
    app.setOrganizationName('STExplorer')
    from src.settings_manager import load_theme
    apply_theme(app, load_font_size(), load_theme())
    # Cap the size of any image the app is willing to decode.  Pillow's own
    # default is high enough that a 380 KiB PNG can expand to 381 MiB of RAM.
    try:
        from src import image_safety
        image_safety.apply_limits()
    except Exception:
        logging.getLogger(__name__).exception("Could not apply the image size limit")

    lock = SingleInstance()
    try:
        # A restart (File > Restore Library) launches the new instance while
        # this one is still shutting down and therefore still holds the
        # lock.  Give it a short grace period instead of failing with
        # "already running".
        def _on_wait(seconds: float) -> None:
            # Without this the window appears to hang for the whole grace
            # period, with no sign of what it is waiting for.
            logger.info(
                "Waiting for the other instance to close (%.0f s)", seconds,
            )
            try:
                from PyQt6.QtCore import Qt
                from PyQt6.QtWidgets import QApplication
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            except Exception:
                pass

        if not lock.acquire(wait_seconds=15.0, on_wait=_on_wait):
            QMessageBox.warning(
                None, 'ST Explorer',
                'ST Explorer is already running.',
            )
            sys.exit(0)

        from src import vault
        if vault.get_vault().enabled:
            # Encrypted library: the password must be verified and the
            # database unsealed before anything opens it.
            from src.ui.widgets.unlock_dialog import UnlockDialog
            unlock = UnlockDialog()
            if unlock.exec() != unlock.DialogCode.Accepted:
                logger.info("Startup cancelled at the unlock prompt")
                sys.exit(0)
            try:
                vault.unseal_database()
            except vault.VaultError:
                logging.getLogger(__name__).exception("Database unseal failed")
                QMessageBox.critical(
                    None, 'ST Explorer',
                    'The library database could not be unlocked.\n\n'
                    'See the log for details: ~/.st-explorer/app.log',
                )
                sys.exit(1)
            if vault.migration_pending():
                # The vault was enabled but the sealing pass never finished
                # (a crash or a cancelled migration).  Half the library is
                # still in plaintext - say so instead of letting it pass
                # unnoticed.
                logging.getLogger(__name__).warning(
                    "The encryption migration did not finish; re-sealing the library.",
                )
                try:
                    vault.seal_all_files()
                    vault.seal_database()
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Could not finish the encryption migration",
                    )
                else:
                    QMessageBox.information(
                        None, 'ST Explorer',
                        'The encryption of your library had not finished and '
                        'has now been completed.',
                    )

        db = LibraryDatabase()
        window = MainWindow(db)
        _start_auto_backup()
    except Exception:
        logging.getLogger(__name__).exception("Startup failed")
        QMessageBox.critical(
            None, 'ST Explorer',
            'The application failed to start.\n\n'
            'See the log for details: ~/.st-explorer/app.log',
        )
        sys.exit(1)

    window.show()

    # Seal the database on the way out (aboutToQuit fires even when the
    # dirty-changes guard cancels a close, but by then exec() is returning
    # and the seal only runs once the event loop is done).
    def _seal_on_exit() -> None:
        try:
            from src import vault
            vault.seal_database()
        except Exception:
            logging.getLogger(__name__).exception("Failed to seal the library database")

    app.aboutToQuit.connect(_seal_on_exit)

    logger.info("ST Explorer started")
    try:
        sys.exit(app.exec())
    finally:
        # The OS lock is released when the process dies anyway, but an
        # explicit release means the lock is gone before the interpreter
        # starts tearing down - so a restart (File > Restore Library) does
        # not have to wait for it.
        try:
            lock.release()
        except Exception:
            logging.getLogger(__name__).debug(
                "The single-instance lock could not be released", exc_info=True,
            )


if __name__ == '__main__':
    main()

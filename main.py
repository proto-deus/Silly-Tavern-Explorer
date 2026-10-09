import logging
import os
import sys
from pathlib import Path

from src.app_paths import data_dir


def _setup_logging() -> None:
    log_dir = data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / 'app.log'

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _check_dependencies() -> list[str]:
    """Check that all required packages are installed.

    Returns a list of missing package names.
    """
    import importlib.util
    # cryptography encrypts the API keys on macOS/Linux - without it they fall
    # back to plain text silently - so it is required, not optional.
    required = ['PyQt6', 'PIL', 'tiktoken', 'requests', 'markdown', 'cryptography']
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


def main():
    # Point tiktoken at the vocab files bundled by build.spec so token
    # counting works offline in the frozen executable (must be set before
    # tiktoken is first imported).
    if getattr(sys, 'frozen', False):
        bundled_cache = Path(getattr(sys, '_MEIPASS', '.')) / 'tiktoken_cache'
        if bundled_cache.is_dir():
            os.environ.setdefault('TIKTOKEN_CACHE_DIR', str(bundled_cache))

    _setup_logging()
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
    apply_theme(app, load_font_size())

    lock = SingleInstance()
    try:
        if not lock.acquire():
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

        db = LibraryDatabase()
        window = MainWindow(db)
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
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

from __future__ import annotations

import logging
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


from src.app_paths import data_dir
from src.database import LibraryDatabase
from src.settings_manager import (
    load_window_geometry, save_window_geometry, load_st_characters_path,
    load_st_worlds_path,
    load_font_size, save_font_size,
    load_theme, save_theme,
    save_library_selected_ids, load_library_selected_ids,
    save_library_scroll_position, load_library_scroll_position,
    save_edit_selected_id, load_edit_selected_id,
)
from src.sillytavern_sync import detect_st_installs
from src.ui.ai_tab import AITab
from src.ui.edit_tab import EditTab
from src.ui.focus import application_focus_blocks_shortcut
from src.ui.library_tab import LibraryTab
from src.ui.test_tab import TestTab
from src.ui.widgets.command_palette import CommandPalette
from src.ui.widgets.gallery_tab import GalleryTab
from src.ui.widgets.lorebooks_tab import LorebooksTab
from src.ui.shortcuts import ACTIONS
from src.ui.widgets.character_sidebar import CharacterSidebar

logger = logging.getLogger(__name__)

# The sort action ids and the labels the Library tab's sort combo shows.
# The combo offers every entry of ``SORT_LABELS`` (Name, Date Added, Token
# Count, Favorites, Rating, Random), so the View > Sort menu must offer
# exactly the same set - otherwise picking "Rating" in the combo left the
# menu showing a *different* sort as checked.
_SORT_ACTION_LABELS: dict[str, str] = {
    'view.sort_name': 'Name',
    'view.sort_date': 'Date Added',
    'view.sort_tokens': 'Token Count',
    'view.sort_favorites': 'Favorites',
    'view.sort_rating': 'Rating',
    'view.sort_random': 'Random',
}
_SORT_ACTION_BY_LABEL: dict[str, str] = {
    label: action_id for action_id, label in _SORT_ACTION_LABELS.items()
}


def format_library_stats(stats: dict) -> str:
    """Format a stats dict (count/tokens/favorites) into a status-bar string.

    Pure function so it can be unit-tested without a Qt event loop.
    """
    count = stats.get('count', 0)
    tokens = stats.get('tokens', 0)
    favorites = stats.get('favorites', 0)
    parts = [f"{count} card{'s' if count != 1 else ''}", f"{tokens:,} total tokens"]
    if favorites:
        parts.append(f"{favorites} favorite{'s' if favorites != 1 else ''}")
    return '  |  '.join(parts)


def build_restart_command(executable: str, argv: list[str], frozen: bool = False) -> list[str]:
    """Build the command line that relaunches the app.

    Frozen (PyInstaller) mode: ``[exe, *user_args]``.  Dev mode must keep
    ``argv[0]`` (the script path) — dropping it spawns a bare interpreter
    instead of restarting the application.  Pure function so both modes can
    be unit-tested without launching a process.
    """
    if frozen:
        return [executable, *argv[1:]]
    script = argv[0] if argv else ''
    return [executable, script, *argv[1:]]


def _palette_label(menu_path: str, label: str) -> str:
    """Build the path-qualified label shown in the command palette.

    ``('View/Sort', 'Sort by Rating')`` becomes ``'View > Sort > Rating'``:
    the submenu is part of the searchable text, and a label that merely
    repeats the submenu name is shortened ("Sort by Rating" -> "Rating").
    """
    parts = [part.strip() for part in menu_path.split('/') if part.strip()]
    tail = parts[-1].casefold() if parts else ''
    if tail and label.casefold().startswith(tail + ' '):
        label = label[len(tail) + 1:].strip()
        if label.casefold().startswith('by '):
            label = label[3:].strip()
    return ' > '.join(parts + [label])


class MainWindow(QMainWindow):
    def __init__(self, db: LibraryDatabase):
        super().__init__()
        self.db = db
        self._suppress_tab_change = False
        self._actions: dict[str, QAction] = {}
        self._st_pushpull_worker = None
        self._st_bulk_worker = None
        # Progress dialogs are child widgets: they are disposed explicitly
        # (close + deleteLater) instead of just dropping the reference.
        self._st_progress: QProgressDialog | None = None
        self._backup_progress: QProgressDialog | None = None
        self._restore_progress: QProgressDialog | None = None
        self._encrypted_backup_progress: QProgressDialog | None = None
        self._command_palette: CommandPalette | None = None
        self.setWindowTitle('ST Explorer - SillyTavern Character Card Explorer')
        self.setMinimumSize(1000, 800)
        self.resize(1200, 1000)

        geometry = load_window_geometry()
        if geometry is not None:
            self.restoreGeometry(geometry)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self._sidebar = CharacterSidebar(db)
        layout.addWidget(self._sidebar)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)

        self._library_tab = LibraryTab(db)
        self._edit_tab = EditTab(db)
        self._gallery_tab = GalleryTab(db)
        self._ai_tab = AITab(db)
        self._lorebooks_tab = LorebooksTab(db)
        self._test_tab = TestTab(db)

        self._tabs.addTab(self._library_tab, 'Library')
        self._tabs.addTab(self._edit_tab, 'Edit')
        self._tabs.addTab(self._gallery_tab, 'Gallery')
        self._tabs.addTab(self._ai_tab, 'Generate')
        self._tabs.addTab(self._lorebooks_tab, 'Lorebooks')
        self._tabs.addTab(self._test_tab, 'Test')

        self._stats_label = QLabel()
        self._stats_label.setStyleSheet('color: #aaa; padding: 0 6px;')
        self._stats_label.setAccessibleName('Library statistics')

        self._filter_label = QLabel()
        self._filter_label.setStyleSheet('color: #aaa; padding: 0 6px;')
        self._filter_label.setAccessibleName('Filtered card count')
        self._filter_label.setVisible(False)

        self._library_tab.edit_requested.connect(self._go_to_edit)
        self._library_tab.library_changed.connect(self._update_status)
        self._library_tab.library_changed.connect(self._edit_tab.refresh_tag_completer)
        self._library_tab.library_changed.connect(self._sidebar.load_cards)
        self._library_tab.status_message.connect(self._show_status_message)
        self._library_tab.filter_stats_changed.connect(self._on_filter_stats_changed)
        self._library_tab.sort_label_changed.connect(self._sync_sort_menu)
        self._edit_tab.status_message.connect(self._show_status_message)
        self._edit_tab.card_deleted.connect(self._on_edit_card_deleted)
        self._edit_tab.card_updated.connect(self._on_edit_card_updated)
        self._edit_tab.card_added.connect(self._on_edit_card_added)
        self._gallery_tab.status_message.connect(self._show_status_message)
        self._gallery_tab.settings_requested.connect(self._on_settings)
        self._gallery_tab.card_updated.connect(self._on_edit_card_updated)
        self._gallery_tab.card_updated.connect(self._on_gallery_card_updated)
        self._library_tab.card_selected.connect(self._on_card_selected)
        self._ai_tab.settings_requested.connect(self._on_settings)
        self._ai_tab.library_changed.connect(self._on_ai_library_changed)
        self._ai_tab.card_updated.connect(self._on_ai_card_updated)
        self._test_tab.settings_requested.connect(self._on_settings)
        self._test_tab.status_message.connect(self._show_status_message)
        self._lorebooks_tab.settings_requested.connect(self._on_settings)
        self._lorebooks_tab.status_message.connect(self._show_status_message)
        self._lorebooks_tab.lorebooks_changed.connect(self._test_tab.reload_lorebooks)
        self._edit_tab.settings_requested.connect(self._on_settings)
        self._library_tab.settings_requested.connect(self._on_settings)
        self._sidebar.card_selected.connect(self._on_sidebar_card_selected)
        self._sidebar.card_double_clicked.connect(self._sidebar.show_full_image)
        self._sidebar.search_changed.connect(self._library_tab.set_search_text)
        self._library_tab.search_changed.connect(self._sidebar.set_search_text)
        # The favorites filter and the sort order are shared between the
        # grid and the sidebar: they used to be independent, so "View >
        # Favorites" left the sidebar listing non-favourites and the two
        # lists could disagree about the order.
        self._sidebar.favorites_filter_changed.connect(
            self._library_tab.set_favorites_only,
        )
        self._library_tab.favorites_filter_changed.connect(
            self._sidebar.set_favorites_only,
        )
        self._sidebar.sort_changed.connect(self._library_tab.set_sort_by_label)
        self._library_tab.sort_label_changed.connect(self._sidebar.set_sort_by_label)
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._build_menus()
        self._library_tab.load_cards()
        self._update_status()
        self._restore_tab_state()

        # The initial tab is Library, which has its own full-page grid; hide the
        # shared sidebar until the user switches to Edit/Generate/Test.
        self._sidebar.setVisible(False)

        from src.ui.widgets.st_status_widget import STStatusWidget
        self._st_status = STStatusWidget()
        self._st_status.clicked.connect(self._on_st_configure)
        self.statusBar().addPermanentWidget(self._st_status)
        self.statusBar().addPermanentWidget(self._filter_label)
        self.statusBar().addPermanentWidget(self._stats_label)
        # Detection probes several drive roots and can take a while — defer
        # it (plus the first status refresh) until after the window paints.
        QTimer.singleShot(0, self._init_st_integration)

    def _init_st_integration(self) -> None:
        """Deferred SillyTavern bootstrap: autodetect, status, watcher."""
        self._maybe_autodetect_st()
        self._st_status.refresh()
        self._setup_st_watcher()

    def _update_status(self) -> None:
        stats = self.db.get_library_stats()
        self._stats_label.setText(format_library_stats(stats))

    def _on_filter_stats_changed(self, matched: int, library_total: int) -> None:
        """Show how many cards the current Library filter matches.

        *matched* is the number of cards the Library tab's current filter
        (search text, tags, favorites, collection) selects, and
        *library_total* is the size of the whole library.  Without this the
        status bar only reported the size of the whole library, so a search
        that matched 3 of 340 cards still read "340 cards" - and the
        filtered figure was only visible on the Library tab itself.
        """
        if library_total <= 0:
            self._filter_label.setVisible(False)
            return
        if library_total == matched:
            # The filter does not narrow the library down: the plain
            # library total is enough.
            self._filter_label.setVisible(False)
            return
        self._filter_label.setText(f'{matched:,} of {library_total:,} shown')
        self._filter_label.setVisible(True)

    def _restore_tab_state(self) -> None:
        """Restore selected cards and scroll positions from the previous session."""
        # Defer scroll restoration so the layout has settled.
        from PyQt6.QtCore import QTimer

        saved_ids = load_library_selected_ids()
        if saved_ids:
            self._library_tab.set_selected_ids(saved_ids)

        edit_id = load_edit_selected_id()
        if edit_id is not None:
            self._sidebar.set_selected_id(edit_id)

        scroll_pos = load_library_scroll_position()
        if scroll_pos > 0:
            QTimer.singleShot(100, lambda: self._library_tab.set_scroll_position(scroll_pos))

    def _show_status_message(self, message: str, timeout: int) -> None:
        self.statusBar().showMessage(message, timeout)

    # ---- Menu bar & shortcuts ----

    def _dispose_progress(self, attr: str) -> None:
        """Close, delete and forget one of the progress dialogs.

        A ``QProgressDialog`` is a child widget: dropping the Python
        reference does not destroy the C++ object, so every bulk sync,
        import or backup left one hidden dialog alive for the lifetime of
        the window.
        """
        dialog = getattr(self, attr, None)
        setattr(self, attr, None)
        if dialog is None:
            return
        try:
            dialog.close()
            dialog.deleteLater()
        except RuntimeError:
            # The C++ object is already gone.
            pass

    def _build_menus(self) -> None:
        """Build the menu bar from the centralised shortcuts registry."""
        menubar = self.menuBar()
        menus: dict[str, QMenu] = {}
        # The action that opens each submenu - a separator that belongs to
        # the parent menu has to be *inserted* before it, not appended.
        menu_actions: dict[str, QAction] = {}

        def get_menu(path: str) -> QMenu:
            if path in menus:
                return menus[path]
            parts = path.split('/')
            if len(parts) == 1:
                menu = menubar.addMenu(parts[0])
            else:
                parent = get_menu('/'.join(parts[:-1]))
                menu = parent.addMenu(parts[-1])
                menu_actions[path] = parent.actions()[-1]
            menus[path] = menu
            return menu

        sort_group = QActionGroup(self)
        sort_group.setExclusive(True)

        # Menus that already have at least one action.  A separator that
        # precedes the *first* entry of a submenu belongs to the parent menu
        # (before the submenu itself) - a separator at the top of a submenu
        # renders as a dangling line above its first item.
        filled: set[str] = set()

        for action_def in ACTIONS:
            menu = get_menu(action_def.menu_path)
            if action_def.separator_before:
                if action_def.menu_path not in filled and '/' in action_def.menu_path:
                    parent_path = '/'.join(action_def.menu_path.split('/')[:-1])
                    parent_menu = get_menu(parent_path)
                    submenu_action = menu_actions.get(action_def.menu_path)
                    if submenu_action is not None:
                        parent_menu.insertSeparator(submenu_action)
                    else:
                        parent_menu.addSeparator()
                else:
                    menu.addSeparator()
            act = QAction(action_def.label, self)
            sequences = [
                QKeySequence(seq)
                for seq in (action_def.shortcut, *action_def.shortcut_aliases)
                if seq
            ]
            if len(sequences) == 1:
                act.setShortcut(sequences[0])
            elif sequences:
                # Several key sequences may trigger the same action (the
                # command palette answers to both Ctrl+K and Ctrl+P).
                act.setShortcuts(sequences)
            if action_def.action_id in _SORT_ACTION_LABELS:
                act.setCheckable(True)
                sort_group.addAction(act)
            menu.addAction(act)
            filled.add(action_def.menu_path)
            self._actions[action_def.action_id] = act
        # Let the Edit tab drive the enabled state of Undo/Redo from its
        # snapshot history.
        self._edit_tab._actions = self._actions
        self._edit_tab._update_undo_actions()

        self._actions['view.sort_name'].setChecked(True)

        # Wire every action to its handler.
        routing: dict[str, callable] = {
            'file.import': self._on_file_import,
            'file.export_png': self._on_file_export_png,
            'file.export_json': self._on_file_export_json,
            'file.backup': self._on_file_backup,
            'file.backup_library': self._on_file_backup_library,
            'file.restore_library': self._on_file_restore_library,
            'file.check_library': self._on_file_check_library,
            'file.quit': self._on_file_quit,
            'edit.save': self._on_edit_save,
            'edit.revert': self._on_edit_revert,
            'edit.undo': self._on_edit_undo,
            'edit.redo': self._on_edit_redo,
            'edit.duplicate': self._on_edit_duplicate,
            'edit.delete': self._on_edit_delete,
            'edit.find': self._on_edit_find,
            'view.favorites': self._on_view_favorites,
            'view.sort_name': lambda: self._on_view_sort('Name'),
            'view.sort_date': lambda: self._on_view_sort('Date Added'),
            'view.sort_tokens': lambda: self._on_view_sort('Token Count'),
            'view.sort_favorites': lambda: self._on_view_sort('Favorites'),
            'view.sort_rating': lambda: self._on_view_sort('Rating'),
            'view.sort_random': lambda: self._on_view_sort('Random'),
            'view.refresh': self._on_view_refresh,
            'view.zoom_in': lambda: self._on_view_zoom(16),
            'view.zoom_out': lambda: self._on_view_zoom(-16),
            'view.zoom_reset': self._on_view_zoom_reset,
            'view.font_size': self._on_view_font_size,
            'view.find_duplicates': self._on_view_find_duplicates,
            'view.statistics': self._on_view_statistics,
            'view.lint_card': self._on_view_lint_card,
            'view.batch_edit': self._on_view_batch_edit,
            'view.semantic_search': self._on_view_semantic_search,
            'view.usage_stats': self._on_view_usage_stats,
            'view.command_palette': self._on_command_palette,
            'file.backup_encrypted': self._on_file_backup_encrypted,
            'st.configure': self._on_st_configure,
            'st.sync': self._on_st_sync,
            'st.sync_lorebooks': self._on_st_sync_lorebooks,
            'st.push_all': self._on_st_push_all,
            'st.pull_all': self._on_st_pull_all,
            'st.push_selected': self._on_st_push_selected,
            'st.pull_selected': self._on_st_pull_selected,
            'st.refresh': self._on_st_refresh,
            'settings.open': self._on_settings,
            'help.about': self._on_help_about,
            'help.view_log': self._on_help_view_log,
            'help.crash_report': self._on_help_crash_report,
            'tab.library': lambda: self._switch_tab(self._library_tab),
            'tab.edit': lambda: self._switch_tab(self._edit_tab),
            'tab.gallery': lambda: self._switch_tab(self._gallery_tab),
            'tab.generate': lambda: self._switch_tab(self._ai_tab),
            'tab.lorebooks': lambda: self._switch_tab(self._lorebooks_tab),
            'tab.test': lambda: self._switch_tab(self._test_tab),
        }
        for action_id, handler in routing.items():
            action = self._actions.get(action_id)
            if action is not None:
                action.triggered.connect(handler)

    def _sync_sort_menu(self, label: str) -> None:
        """Update the checked sort action when the combo changes."""
        action_id = _SORT_ACTION_BY_LABEL.get(label)
        if action_id and action_id in self._actions:
            self._actions[action_id].setChecked(True)

    # ---- command palette ----

    def _command_palette_entries(self) -> tuple[list[tuple[str, str, str]], dict[str, QAction]]:
        """Collect the commands the palette offers.

        Only actions that are currently enabled and visible are offered, so
        the palette never lists a command that cannot run right now.
        """
        entries: list[tuple[str, str, str]] = []
        actions: dict[str, QAction] = {}
        for action_def in ACTIONS:
            if action_def.action_id == 'view.command_palette':
                # Re-opening the palette from the palette is pointless.
                continue
            action = self._actions.get(action_def.action_id)
            if action is None or not action.isVisible() or not action.isEnabled():
                continue
            shortcut = action.shortcut().toString() or action_def.shortcut
            entries.append((
                action_def.action_id,
                _palette_label(action_def.menu_path, action_def.label),
                shortcut,
            ))
            actions[action_def.action_id] = action
        return entries, actions

    def _on_command_palette(self) -> None:
        """Open the command palette (Ctrl+K / Ctrl+P)."""
        entries, actions = self._command_palette_entries()
        if not entries:
            self.statusBar().showMessage('No commands available.', 3000)
            return
        palette = CommandPalette(entries, actions, self)
        palette.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._command_palette = palette
        # While the palette is open its own shortcut would open a second
        # one, so the action is disabled until the palette closes.
        action = self._actions.get('view.command_palette')
        if action is not None:
            action.setEnabled(False)
        palette.finished.connect(self._on_command_palette_finished)
        palette.show()
        palette.raise_()
        palette.activateWindow()

    def _on_command_palette_finished(self) -> None:
        self._command_palette = None
        action = self._actions.get('view.command_palette')
        if action is not None:
            action.setEnabled(True)

    # ---- File handlers ----

    def _on_file_import(self) -> None:
        self._tabs.setCurrentWidget(self._library_tab)
        self._library_tab.import_card()

    def _on_file_export_png(self) -> None:
        widget = self._tabs.currentWidget()
        if isinstance(widget, (LibraryTab, EditTab)):
            widget.export_png()

    def _on_file_export_json(self) -> None:
        widget = self._tabs.currentWidget()
        if isinstance(widget, LibraryTab):
            self._library_tab._on_export_json()
        elif isinstance(widget, EditTab):
            self._edit_tab._export_json()

    def _on_file_backup(self) -> None:
        """Create the rotating ``.bak`` copy of the database.

        The checkpoint takes a write lock on the whole database and the
        copy reads it, so this runs on a worker thread: on a large library
        it froze the window for seconds.
        """
        from PyQt6.QtCore import QThread

        class _DbBackupWorker(QThread):
            finished_ok = pyqtSignal(str)
            failed = pyqtSignal(str)

            def __init__(self, db):
                super().__init__()
                self._db = db

            def run(self) -> None:
                try:
                    path = self._db.backup()
                    self.finished_ok.emit(str(path))
                except Exception as exc:
                    logger.exception("Database backup failed")
                    self.failed.emit(str(exc))

        self._db_backup_worker = _DbBackupWorker(self.db)
        self._db_backup_worker.finished_ok.connect(self._on_db_backup_ok)
        self._db_backup_worker.failed.connect(self._on_db_backup_fail)
        self._db_backup_worker.finished.connect(self._db_backup_worker.deleteLater)
        self._db_backup_worker.start()

    def _on_db_backup_ok(self, path: str) -> None:
        self.statusBar().showMessage(f"Backup saved to {Path(path).name}", 5000)

    def _on_db_backup_fail(self, message: str) -> None:
        QMessageBox.critical(self, 'Backup', f"Failed to create backup: {message}")

    def _on_file_backup_library(self) -> None:
        """Zip the whole library (DB + cards + sessions + thumbnails)."""
        from src.library_backup import BackupCancelled, create_backup
        from PyQt6.QtCore import QThread

        dest, _ = QFileDialog.getSaveFileName(
            self, 'Backup Library', 'st-explorer-backup.zip', 'Zip Archives (*.zip)',
        )
        if not dest:
            return
        if not dest.lower().endswith('.zip'):
            dest += '.zip'

        class _BackupWorker(QThread):
            finished_ok = pyqtSignal(dict)
            failed = pyqtSignal(str)

            def __init__(self, path: str):
                super().__init__()
                self.path = path
                self.cancelled = False

            def run(self) -> None:
                try:
                    def progress(fraction: float, message: str) -> bool:
                        return not self.cancelled

                    manifest = create_backup(self.path, progress_cb=progress)
                    self.finished_ok.emit(manifest)
                except BackupCancelled:
                    pass
                except Exception as exc:
                    logger.exception("Library backup failed")
                    self.failed.emit(str(exc))

        self._backup_worker = _BackupWorker(dest)
        self._backup_progress = QProgressDialog('Creating backup...', 'Cancel', 0, 0, self)
        progress = self._backup_progress
        progress.setWindowTitle('Backup Library')
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModality.WindowModal)

        # Bound-method slots (not closures): PyQt delivers signals emitted
        # from a QThread to QObject receiver methods via queued connections,
        # so these run on the GUI thread.
        progress.canceled.connect(self._on_backup_library_cancel)
        self._backup_worker.finished_ok.connect(self._on_backup_library_ok)
        self._backup_worker.failed.connect(self._on_backup_library_fail)
        # Cleanup on the built-in signal so the thread object is deleted.
        self._backup_worker.finished.connect(self._backup_worker.deleteLater)
        self._backup_worker.start()
        progress.exec()
        # The dialog is a child widget: it has to be deleted explicitly.
        self._dispose_progress('_backup_progress')

    def _on_backup_library_cancel(self) -> None:
        if getattr(self, '_backup_worker', None) is not None:
            try:
                self._backup_worker.cancelled = True
            except RuntimeError:
                pass

    def _on_backup_library_ok(self, manifest: dict) -> None:
        self._dispose_progress('_backup_progress')
        counts = manifest.get('counts', {})
        self.statusBar().showMessage(
            f"Library backup saved ({counts.get('cards', 0)} cards)", 5000,
        )

    def _on_backup_library_fail(self, message: str) -> None:
        self._dispose_progress('_backup_progress')
        QMessageBox.critical(self, 'Backup Library', f'Backup failed:\n{message}')

    def _on_file_check_library(self) -> None:
        """Run the library integrity check (File > Check Library)."""
        from src.ui.dialog_helper import exec_dialog
        from src.ui.widgets.doctor_dialog import DoctorDialog
        self.statusBar().showMessage('Checking the library...', 0)
        try:
            dlg = DoctorDialog(self.db, self)
            exec_dialog(dlg)
        finally:
            self.statusBar().showMessage('Library check finished.', 5000)

    def _on_file_restore_library(self) -> None:
        """Replace the current library with a backup zip (prompts restart)."""
        from src.library_backup import BackupCancelled, restore_backup
        from PyQt6.QtCore import QThread

        confirm = QMessageBox.warning(
            self, 'Restore Library',
            'Restoring a backup replaces your ENTIRE current library:\n'
            'cards, chat sessions, and database.\n\n'
            'This cannot be undone. Continue?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        src, _ = QFileDialog.getOpenFileName(
            self, 'Restore Library', '', 'Zip Archives (*.zip)',
        )
        if not src:
            return

        class _RestoreWorker(QThread):
            finished_ok = pyqtSignal(dict)
            failed = pyqtSignal(str)

            def __init__(self, path: str):
                super().__init__()
                self.path = path
                self.cancelled = False

            def run(self) -> None:
                try:
                    def progress(fraction: float, message: str) -> bool:
                        return not self.cancelled

                    manifest = restore_backup(self.path, progress_cb=progress)
                    self.finished_ok.emit(manifest)
                except BackupCancelled:
                    pass
                except Exception as exc:
                    logger.exception("Library restore failed")
                    self.failed.emit(str(exc))

        self._restore_worker = _RestoreWorker(src)
        self._restore_progress = QProgressDialog('Restoring library...', 'Cancel', 0, 0, self)
        progress = self._restore_progress
        progress.setWindowTitle('Restore Library')
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModality.WindowModal)

        # Bound-method slots: delivered on the GUI thread even though the
        # worker thread emits them.
        progress.canceled.connect(self._on_restore_library_cancel)
        self._restore_worker.finished_ok.connect(self._on_restore_library_ok)
        self._restore_worker.failed.connect(self._on_restore_library_fail)
        # Cleanup on the built-in signal so the thread object is deleted.
        self._restore_worker.finished.connect(self._restore_worker.deleteLater)
        self._restore_worker.start()
        progress.exec()
        # The dialog is a child widget: it has to be deleted explicitly.
        self._dispose_progress('_restore_progress')

    def _on_restore_library_cancel(self) -> None:
        # Past the point of no return (files are being swapped) a cancel
        # can't be honoured safely; it stops *before* extraction starts.
        if getattr(self, '_restore_worker', None) is not None:
            try:
                self._restore_worker.cancelled = True
            except RuntimeError:
                pass

    def _on_restore_library_ok(self, _manifest: dict) -> None:
        self._dispose_progress('_restore_progress')
        self.statusBar().showMessage('Library restored.', 5000)
        # Restart is mandatory, not optional: open database connections and
        # cached UI state still point at the pre-restore library, so letting
        # the user decline would leave the app running against a swapped-out
        # data dir.
        QMessageBox.information(
            self, 'Restore Library',
            'Restore complete. ST Explorer will now restart to load the '
            'restored library.',
        )
        self._restart_application()

    def _on_restore_library_fail(self, message: str) -> None:
        self._dispose_progress('_restore_progress')
        QMessageBox.critical(self, 'Restore Library', f'Restore failed:\n{message}')

    def _restart_application(self) -> bool:
        """Close the window and launch a fresh instance of the app.

        The window is closed *first*: the close can still be vetoed (the
        Edit tab has unsaved changes, or a background worker refuses to
        stop), and starting the new process before that decision would leave
        two instances running against the same data directory - and the new
        one would lose the single-instance race and exit again.

        Returns ``True`` when a new instance was started.
        """
        import subprocess
        import sys

        if not self.close():
            self.statusBar().showMessage(
                'Restart cancelled - close ST Explorer and start it again to '
                'load the restored library.',
                8000,
            )
            return False
        try:
            cmd = build_restart_command(
                sys.executable, list(sys.argv), frozen=bool(getattr(sys, 'frozen', False)),
            )
            subprocess.Popen(cmd)
        except Exception:
            logger.exception("Restart failed; falling back to plain close")
            return False
        return True

    def _on_file_quit(self) -> None:
        self.close()

    # ---- Edit handlers ----

    def _on_edit_save(self) -> None:
        self._edit_tab.save()

    def _on_edit_revert(self) -> None:
        self._edit_tab.revert()

    def _on_edit_undo(self) -> None:
        if self._tabs.currentWidget() is not self._edit_tab:
            self.statusBar().showMessage(
                'Undo applies to the Edit tab; switch to it first.', 4000,
            )
            return
        self._edit_tab.undo()

    def _on_edit_redo(self) -> None:
        if self._tabs.currentWidget() is not self._edit_tab:
            self.statusBar().showMessage(
                'Redo applies to the Edit tab; switch to it first.', 4000,
            )
            return
        self._edit_tab.redo()

    def _on_edit_duplicate(self) -> None:
        widget = self._tabs.currentWidget()
        if isinstance(widget, LibraryTab):
            widget.duplicate_card()
        elif isinstance(widget, EditTab):
            widget.duplicate_card()
        else:
            # The Generate/Lorebooks/Test tabs have no selection of their own;
            # say so rather than appearing to do nothing.
            sid = self._sidebar.current_id()
            if sid is not None:
                self.statusBar().showMessage(
                    'Duplicate is available from the Library or Edit tab.', 4000,
                )
            else:
                self.statusBar().showMessage('Select a card first.', 4000)

    def _on_edit_delete(self) -> None:
        # 'Del' is a window-wide shortcut, so it fires no matter which widget
        # has the keyboard focus.  Inside a text field the key belongs to the
        # field, and inside a combo box / item view it belongs to that widget
        # - pressing Del while the collection filter was focused used to
        # delete the selected card.  Only honour it on the Library tab, and
        # only when no text/choice widget owns the keyboard.
        widget = self._tabs.currentWidget()
        if widget is not self._library_tab:
            return
        if application_focus_blocks_shortcut():
            return
        self._library_tab.delete_selected()

    def _on_edit_card_deleted(self, char_id: int) -> None:
        self._library_tab._remove_thumbnail_in_place(char_id)
        self._library_tab._update_selection_ui()
        self._sidebar.remove_card(char_id)
        self._update_status()

    def _on_edit_card_updated(self, char_id: int) -> None:
        self._sidebar.refresh_card(char_id)
        self._library_tab._refresh_single_thumbnail(char_id)
        # The saved text changed the token total, so the status-bar figure was
        # stale until some unrelated action happened to emit library_changed.
        self._update_status()

    def _on_edit_card_added(self, char_id: int) -> None:
        # Resolve unsaved edits for the previous card first, then load the
        # newly created card into the editor form (the Edit tab is frontmost
        # here since the creation dialog was opened from it). Writing
        # ``set_selected_id`` directly would leave a stale form under the new
        # card's id, letting Ctrl+S overwrite the new card with the previous
        # card's content.
        proceed = True
        if self._edit_tab.is_dirty():
            result = self._prompt_dirty_switch()
            if result == 'cancel':
                proceed = False
        self._library_tab.load_cards()
        if not proceed:
            self._update_status()
            return
        self._sidebar.load_cards()
        self._sidebar.select_card(char_id)
        self._edit_tab.load_card_by_id(char_id)
        self._ai_tab.select_card(char_id)
        self._lorebooks_tab.set_selected_card(char_id)
        self._update_status()

    def _on_ai_library_changed(self) -> None:
        # Generate-tab batch writes mutate cards in place; the parsed-card
        # cache must go with the grid rebuild or the detail pane keeps
        # serving the pre-batch card (the sync path invalidates for the
        # same reason).
        self._library_tab.invalidate_card_cache()
        self._library_tab.load_cards()
        # library_changed already triggers a sidebar rebuild via the signal
        # wiring — an explicit second load_cards() here would rebuild the
        # sidebar twice per Generate-tab change.
        self._update_status()

    def _on_ai_card_updated(self, char_id: int) -> None:
        """Refresh the single card updated by the Generate tab without a full
        library/sidebar rebuild (which orphans the visible sidebar thumbnails
        and causes a storm of stray top-level windows)."""
        self._sidebar.refresh_card(char_id)
        self._library_tab._refresh_single_thumbnail(char_id)
        self._update_status()

    def _on_gallery_card_updated(self, char_id: int) -> None:
        """A Gallery 'Set as Card Image' changed the PNG under the editor."""
        if self._edit_tab.form_loaded_for_id == char_id and not self._edit_tab.is_dirty():
            self._edit_tab.reload_current()

    def _on_edit_find(self) -> None:
        self._tabs.setCurrentWidget(self._library_tab)
        self._library_tab.focus_search()

    # ---- View handlers ----

    def _on_view_favorites(self) -> None:
        # 'F' is a window-wide shortcut, so it fires no matter which widget
        # has the keyboard focus.  Inside a text field the key belongs to the
        # field, and inside a combo box / item view it belongs to that widget.
        # Scope it to the Library tab and ignore it while such a widget owns
        # the keyboard.
        if self._tabs.currentWidget() is not self._library_tab:
            return
        if application_focus_blocks_shortcut():
            return
        self._library_tab.toggle_favorites_filter()

    def _on_view_sort(self, label: str) -> None:
        self._library_tab.set_sort_by_label(label)

    def _on_view_zoom(self, delta: int) -> None:
        """Zoom the Library grid.

        Scoped to the Library tab: the action used to resize the invisible
        grid from any tab, so Ctrl+= on the Edit tab changed a thumbnail size
        the user could not see.
        """
        widget = self._tabs.currentWidget()
        if isinstance(widget, LibraryTab):
            widget.zoom_thumbnails(delta)

    def _on_view_zoom_reset(self) -> None:
        """Restore the default thumbnail size (Ctrl+0)."""
        widget = self._tabs.currentWidget()
        if isinstance(widget, LibraryTab):
            widget.reset_thumbnail_size()

    def _switch_tab(self, target: QWidget) -> None:
        """Focus another tab (Ctrl+1 .. Ctrl+6)."""
        if self._tabs.currentWidget() is target:
            return
        self._tabs.setCurrentWidget(target)

    def _on_view_refresh(self) -> None:
        widget = self._tabs.currentWidget()
        if isinstance(widget, LibraryTab):
            widget.refresh()
        elif isinstance(widget, (EditTab, TestTab, AITab)):
            self._sidebar.load_cards()
            sid = self._sidebar.current_id()
            if sid is not None:
                if isinstance(widget, EditTab):
                    # Reloading discards unsaved edits — same guard as a
                    # tab switch or sidebar selection change.
                    if widget.is_dirty():
                        result = self._prompt_dirty_switch()
                        if result == 'cancel':
                            return
                    widget.load_card_by_id(sid)
                elif isinstance(widget, TestTab):
                    widget.select_card(sid)
                elif isinstance(widget, AITab):
                    widget.select_card(sid)
        elif isinstance(widget, LorebooksTab):
            widget.refresh_books()

    def _on_view_font_size(self) -> None:
        from src.ui.dialog_helper import exec_dialog_with
        from src.ui.styles import apply_theme, available_themes

        dlg = QDialog(self)
        dlg.setWindowTitle('Font Size & Theme')
        dlg.setMinimumWidth(250)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel('Set the application font size (px):'))
        row = QHBoxLayout()
        spin = QSpinBox()
        # The same range the settings clamp to, so a hand-edited value
        # cannot put the dialog out of sync with the stored setting.
        spin.setRange(8, 32)
        spin.setSuffix(' px')
        spin.setValue(load_font_size())
        row.addWidget(spin)
        layout.addLayout(row)

        layout.addWidget(QLabel('Theme:'))
        theme_combo = QComboBox()
        theme_combo.addItems(available_themes())
        theme_combo.setCurrentText(load_theme())
        layout.addWidget(theme_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        def _collect(d: QDialog):
            accepted = d.result() == QDialog.DialogCode.Accepted
            if not accepted:
                return None
            return spin.value(), theme_combo.currentText()

        result = exec_dialog_with(dlg, _collect)
        if result is not None:
            size, theme = result
            save_font_size(size)
            save_theme(theme)
            apply_theme(QApplication.instance(), size, theme)
            # Every tab that bakes a pixel size into a widget stylesheet has
            # to be told about the new size.
            for tab in (
                self._test_tab, self._library_tab, self._gallery_tab,
                self._edit_tab, self._ai_tab, self._lorebooks_tab,
            ):
                refresh = getattr(tab, 'refresh_font_size', None)
                if callable(refresh):
                    try:
                        refresh()
                    except Exception:
                        logger.debug("Could not refresh the font size of %r", tab, exc_info=True)

    def _on_view_find_duplicates(self) -> None:
        from src.ui.widgets.duplicate_scanner import DuplicateScannerDialog
        dlg = DuplicateScannerDialog(self.db, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.exec()
        # Deletions from the scanner must be reflected in the grid (the
        # toolbar path in LibraryTab.find_duplicates does the same).
        self._library_tab.invalidate_card_cache()
        self._library_tab.load_cards()

    def _on_view_statistics(self) -> None:
        from src.ui.widgets.stats_dialog import StatsDialog
        dlg = StatsDialog(self.db, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.exec()

    def _on_view_lint_card(self) -> None:
        """Quality-check the selected card."""
        from src.ui.widgets.feature_dialogs import CardLinterDialog
        card_id = self._library_tab.get_selected_id()
        if card_id is None:
            QMessageBox.information(
                self, 'Check Card', 'Select a card in the Library tab first.',
            )
            return
        card = self._edit_tab.current_card()
        if card is None:
            from src.card_models import CharacterCard
            from src.card_parser import read_card_data
            row = self.db.get_by_id(card_id)
            card = CharacterCard()
            if row:
                data = read_card_data(row.get('source_path', '')) or {}
                inner = data.get('data') if isinstance(data.get('data'), dict) else data
                for key, value in inner.items():
                    if hasattr(card, key):
                        setattr(card, key, value)
        dlg = CardLinterDialog(card, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.exec()

    def _on_view_batch_edit(self) -> None:
        """Apply one field change to every selected card."""
        from src.ui.widgets.feature_dialogs import BatchEditDialog
        ids = self._library_tab.get_selected_ids()
        if not ids:
            QMessageBox.information(
                self, 'Batch Edit', 'Select one or more cards in the Library tab first.',
            )
            return
        dlg = BatchEditDialog(ids, self.db, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        changed = dlg.apply()
        self._library_tab.load_cards()
        QMessageBox.information(
            self, 'Batch Edit', f'{changed} card(s) updated.',
        )

    def _on_view_semantic_search(self) -> None:
        from src.ui.widgets.feature_dialogs import SemanticSearchDialog
        dlg = SemanticSearchDialog(self.db, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.exec()

    def _on_view_usage_stats(self) -> None:
        from src.ui.widgets.feature_dialogs import UsageStatsDialog
        dlg = UsageStatsDialog(self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.exec()

    def _on_file_backup_encrypted(self) -> None:
        """Create a password-protected backup archive."""
        from src.library_backup import BackupCancelled, create_encrypted_backup
        from PyQt6.QtCore import QThread

        dest, _ = QFileDialog.getSaveFileName(
            self, 'Encrypted Backup', 'st-explorer-backup.enc',
            'Encrypted Archives (*.enc)',
        )
        if not dest:
            return
        if not dest.lower().endswith('.enc'):
            dest += '.enc'
        password, ok = QInputDialog.getText(
            self, 'Encrypted Backup',
            'Choose a password for the backup.\n'
            'It cannot be recovered if you forget it.',
            QLineEdit.EchoMode.Password,
        )
        if not ok or not password:
            return

        class _EncryptedBackupWorker(QThread):
            finished_ok = pyqtSignal(str, dict)
            failed = pyqtSignal(str)

            def __init__(self, path: str, secret: str):
                super().__init__()
                self.path = path
                self.secret = secret
                self.cancelled = False

            def cancel(self) -> None:
                """Ask the worker to stop (cooperative)."""
                self.cancelled = True

            def run(self) -> None:
                try:
                    def progress(fraction: float, message: str) -> bool:
                        return not self.cancelled

                    manifest = create_encrypted_backup(
                        self.path, self.secret, progress_cb=progress,
                    )
                    self.finished_ok.emit(self.path, manifest)
                except BackupCancelled:
                    pass
                except Exception as exc:
                    logger.exception("Encrypted backup failed")
                    self.failed.emit(str(exc))

        # Zipping the whole library and encrypting it with a key derived
        # from the password takes seconds - running it on the GUI thread
        # froze the window with no way to cancel.
        self._encrypted_backup_worker = _EncryptedBackupWorker(dest, password)
        self._encrypted_backup_progress = QProgressDialog(
            'Creating encrypted backup...', 'Cancel', 0, 0, self,
        )
        progress = self._encrypted_backup_progress
        progress.setWindowTitle('Encrypted Backup')
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.canceled.connect(self._on_encrypted_backup_cancel)
        self._encrypted_backup_worker.finished_ok.connect(self._on_encrypted_backup_ok)
        self._encrypted_backup_worker.failed.connect(self._on_encrypted_backup_fail)
        self._encrypted_backup_worker.finished.connect(
            self._encrypted_backup_worker.deleteLater,
        )
        self._encrypted_backup_worker.start()
        progress.exec()
        self._dispose_progress('_encrypted_backup_progress')

    def _on_encrypted_backup_cancel(self) -> None:
        worker = getattr(self, '_encrypted_backup_worker', None)
        if worker is not None:
            try:
                worker.cancelled = True
            except RuntimeError:
                pass

    def _on_encrypted_backup_ok(self, path: str, manifest: dict) -> None:
        self._dispose_progress('_encrypted_backup_progress')
        QMessageBox.information(
            self, 'Encrypted Backup',
            f'Encrypted backup written to {path}\n'
            f'({manifest.get("counts", {}).get("cards", 0)} card(s))',
        )

    def _on_encrypted_backup_fail(self, message: str) -> None:
        self._dispose_progress('_encrypted_backup_progress')
        QMessageBox.critical(self, 'Encrypted Backup', f'The backup failed:\n{message}')

    def _on_help_crash_report(self) -> None:
        from src.ui.widgets.feature_dialogs import CrashReportDialog
        dlg = CrashReportDialog(self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.exec()

    # ---- Help handlers ----

    def _on_help_about(self) -> None:
        QMessageBox.about(
            self, 'About ST Explorer',
            '<h3>ST Explorer</h3>'
            '<p>A desktop application for browsing, editing, and managing '
            'SillyTavern character cards.</p>'
            '<p>Built with PyQt6, Pillow, tiktoken, and SQLite.</p>',
        )

    def _on_help_view_log(self) -> None:
        log_path = data_dir() / 'app.log'
        if not log_path.exists():
            QMessageBox.information(self, 'View Log', 'No log file found.')
            return
        dlg = QDialog(self)
        dlg.setWindowTitle('Application Log')
        dlg.resize(800, 600)
        layout = QVBoxLayout(dlg)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setStyleSheet('font-family: Consolas, "Courier New", monospace; ')
        try:
            content = log_path.read_text(encoding='utf-8', errors='replace')
            if len(content) > 20000:
                content = '... (showing last 20,000 characters)\n' + content[-20000:]
            text.setPlainText(content)
            scrollbar = text.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        except OSError as e:
            text.setPlainText(f'Error reading log: {e}')
        layout.addWidget(text)
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)
        # exec_dialog (not dlg.exec()): a parented dialog without
        # WA_DeleteOnClose leaks one viewer (and its 20 KB of text) per
        # invocation for the life of the main window.
        from src.ui.dialog_helper import exec_dialog
        exec_dialog(dlg)

    # ---- SillyTavern handlers ----

    def _maybe_autodetect_st(self) -> None:
        """Auto-detect SillyTavern on first launch if not yet configured."""
        from src.settings_manager import (
            save_st_characters_path,
            load_st_auto_detect_done,
            save_st_auto_detect_done,
        )
        if load_st_characters_path():
            return
        if load_st_auto_detect_done():
            return
        # Detection probes several drive roots and can take a while; run it
        # only once, but only mark it "done" when it actually produced a
        # candidate — otherwise a user who installs ST later would never be
        # auto-configured.
        candidates = detect_st_installs()
        if len(candidates) == 1:
            save_st_characters_path(str(candidates[0]))
            save_st_auto_detect_done(True)
            logger.info("Auto-detected SillyTavern at %s", candidates[0])
        elif len(candidates) > 1:
            # Ambiguous: let the user pick manually, don't retry next launch.
            save_st_auto_detect_done(True)

    def _on_st_configure(self) -> None:
        from src.ui.widgets.st_config_dialog import STConfigDialog
        dlg = STConfigDialog(self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.settings_changed.connect(self._on_st_path_changed)
        dlg.exec()

    def _on_st_path_changed(self, path: str) -> None:
        self._st_status.refresh()
        self._setup_st_watcher()

    def _on_st_sync(self) -> None:
        path = load_st_characters_path()
        if not path or not Path(path).is_dir():
            QMessageBox.warning(
                self, 'SillyTavern',
                'SillyTavern directory not configured.\n'
                'Click "Configure..." to set it up.',
            )
            self._on_st_configure()
            return
        from src.ui.widgets.st_sync_dialog import STSyncDialog
        dlg = STSyncDialog(self.db, path, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.sync_completed.connect(self._on_st_sync_completed)
        dlg.exec()
        self._st_status.refresh()

    def _on_st_sync_completed(self) -> None:
        # Sync may have rewritten source files on disk; stale CharacterCard
        # cache entries must not be served in the detail pane afterwards.
        self._library_tab.invalidate_card_cache()
        self._library_tab.load_cards()
        self._st_status.refresh()

    def _on_st_sync_lorebooks(self) -> None:
        """Open the two-way lorebook (world info) sync dialog."""
        characters_path = load_st_characters_path()
        if not characters_path or not Path(characters_path).is_dir():
            QMessageBox.warning(
                self, 'Lorebook Sync',
                'SillyTavern directory not configured.\n'
                'Click "Configure..." to set it up.',
            )
            self._on_st_configure()
            return
        from src.lorebook_sync import resolve_worlds_dir
        from src.ui.widgets.lorebook_sync_dialog import LorebookSyncDialog

        worlds_dir = load_st_worlds_path() or str(resolve_worlds_dir(characters_path))
        dlg = LorebookSyncDialog(worlds_dir, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.sync_completed.connect(self._on_lorebook_sync_completed)
        dlg.exec()

    def _on_lorebook_sync_completed(self) -> None:
        """Refresh lorebook consumers after a sync changed the library."""
        self._lorebooks_tab.refresh_books()
        self._test_tab.reload_lorebooks()

    def _on_st_push_all(self) -> None:
        path = load_st_characters_path()
        if not path or not Path(path).is_dir():
            QMessageBox.warning(self, 'SillyTavern', 'SillyTavern directory not configured.')
            self._on_st_configure()
            return
        from src.sillytavern_sync import build_push_all_plan
        self._plan_bulk_sync(build_push_all_plan, path, 'Pushing all to SillyTavern...')

    def _on_st_pull_all(self) -> None:
        path = load_st_characters_path()
        if not path or not Path(path).is_dir():
            QMessageBox.warning(self, 'SillyTavern', 'SillyTavern directory not configured.')
            self._on_st_configure()
            return
        from src.sillytavern_sync import build_pull_all_plan
        self._plan_bulk_sync(build_pull_all_plan, path, 'Pulling all from SillyTavern...')

    def _plan_bulk_sync(self, plan_builder, path: str, label: str) -> None:
        """Scan both libraries in the background, then run the bulk sync.

        Building the comparison requires reading + hashing every PNG on
        both sides — far too slow for the UI thread.
        """
        from src.ui.widgets.sync_worker import ScanWorker

        if self._sync_workers_busy():
            self.statusBar().showMessage('A sync operation is already running.', 3000)
            return
        self.statusBar().showMessage('Scanning libraries...', 0)
        worker = ScanWorker(self.db, path, self)
        self._st_plan_worker = worker
        # Context for the completion slots (bound methods run on the GUI
        # thread even though the worker emits them).
        self._st_plan_builder = plan_builder
        self._st_plan_path = path
        self._st_plan_label = label

        worker.completed.connect(self._on_st_scan_completed)
        worker.failed.connect(self._on_st_scan_failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @staticmethod
    def _worker_is_alive(worker) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            # The C++ QThread was already destroyed by deleteLater.
            return False

    def _sync_workers_busy(self) -> bool:
        """True while any scan/push-pull/bulk sync worker is running.

        One guard for all three: asymmetric checks let a "Push Selected"
        start during a scan and run ``bulk_sync`` concurrently with the
        scan's follow-up, overwriting each other's ST files.
        """
        for attr in ('_st_plan_worker', '_st_pushpull_worker', '_st_bulk_worker'):
            if self._worker_is_alive(getattr(self, attr, None)):
                return True
        return False

    def _on_st_scan_completed(self, pairs) -> None:
        from src.sillytavern_sync import SyncAction
        plan_builder = getattr(self, '_st_plan_builder', None)
        path = getattr(self, '_st_plan_path', None)
        label = getattr(self, '_st_plan_label', 'Syncing...')
        self._st_plan_worker = None
        if plan_builder is None or path is None:
            return
        plan = [item for item in plan_builder(pairs) if item.action != SyncAction.SKIP]
        if not plan:
            self.statusBar().showMessage('Nothing to sync.', 5000)
            return
        self._run_bulk_sync(plan, path, label)

    def _on_st_scan_failed(self, message: str) -> None:
        self._st_plan_worker = None
        logger.error("Bulk sync scan failed: %s", message)
        self.statusBar().showMessage(f'Scan failed: {message}', 5000)

    def _on_st_push_selected(self) -> None:
        self._st_push_pull_selected('push')

    def _on_st_pull_selected(self) -> None:
        self._st_push_pull_selected('pull')

    def _st_push_pull_selected(self, direction: str) -> None:
        """Push/pull the currently selected card via a background worker."""
        from src.sillytavern_sync import SyncAction
        from src.ui.widgets.sync_worker import PushPullWorker

        char_id = self._sidebar.current_id()
        if char_id is None:
            QMessageBox.information(
                self, 'SillyTavern', 'Select a card first (Edit / Generate / Test sidebar).',
            )
            return
        path = load_st_characters_path()
        if not path or not Path(path).is_dir():
            QMessageBox.warning(self, 'SillyTavern', 'SillyTavern directory not configured.')
            self._on_st_configure()
            return
        if self._sync_workers_busy():
            self.statusBar().showMessage('A sync operation is already running.', 3000)
            return
        action = SyncAction.PUSH if direction == 'push' else SyncAction.PULL
        label = f"{'Pushing' if direction == 'push' else 'Pulling'} selected card..."
        self.statusBar().showMessage(label, 0)
        worker = PushPullWorker(self.db, path, char_id, action, self)
        self._st_pushpull_worker = worker

        worker.completed.connect(self._on_pushpull_selected_done)
        # Cleanup on the built-in signal so the thread object is deleted.
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_pushpull_selected_done(self, summary) -> None:
        self._st_pushpull_worker = None
        self.statusBar().showMessage(summary.message() or 'Done.', 5000)
        if summary.errors:
            QMessageBox.warning(
                self, 'SillyTavern Sync',
                summary.message() + "\n\nErrors:\n" + '\n'.join(summary.errors[:20]),
            )
        self._on_sync_changed()

    def _run_bulk_sync(self, plan, path: str, label: str) -> None:
        from src.ui.widgets.sync_worker import SyncWorker
        if self._sync_workers_busy():
            # Refuse rather than overwrite a live worker: replacing the slot
            # orphaned the running thread (invisible to shutdown) and gave
            # both workers independent filename-collision sets.
            self.statusBar().showMessage('A sync operation is already running.', 3000)
            return
        self.statusBar().showMessage(label, 0)
        # A *separate* attribute from _st_pushpull_worker: PushPullWorker and
        # SyncWorker both ran bulk_sync concurrently when they shared one slot,
        # giving them independent filename-collision sets that could overwrite
        # each other's ST files.
        self._st_bulk_worker = SyncWorker(self.db, plan, path, self)
        self._st_progress = QProgressDialog(label, 'Cancel', 0, len(plan), self)
        self._st_progress.setWindowTitle('SillyTavern Sync')
        self._st_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._st_progress.setMinimumDuration(300)
        self._st_progress.setAutoClose(False)
        self._st_progress.setAutoReset(False)
        self._st_progress.canceled.connect(self._st_bulk_worker.cancel)
        self._st_bulk_worker.progress.connect(self._on_st_bulk_progress)
        self._st_bulk_worker.completed.connect(self._on_st_bulk_finished)
        # Cleanup on the built-in signal so cancelled runs are still deleted.
        self._st_bulk_worker.finished.connect(self._st_bulk_worker.deleteLater)
        self._st_bulk_worker.start()

    def _on_st_bulk_progress(self, current: int, total: int, name: str) -> None:
        progress = getattr(self, '_st_progress', None)
        if progress is None:
            return
        progress.setMaximum(total)
        progress.setValue(current)
        progress.setLabelText(f"Syncing {current}/{total}: {name}")

    def _on_st_bulk_finished(self, result) -> None:
        summary, _pairs = result
        self._dispose_progress('_st_progress')
        self._st_bulk_worker = None
        self.statusBar().showMessage(summary.message(), 5000)
        if summary.errors:
            QMessageBox.warning(
                self, 'SillyTavern Sync',
                summary.message() + "\n\nErrors:\n" + '\n'.join(summary.errors[:20]),
            )
        self._on_sync_changed()

    def _on_sync_changed(self) -> None:
        """Refresh everything a completed sync could have invalidated.

        The Edit tab must be reloaded too: pull rewrites the card file on disk,
        so leaving the editor's form buffer populated would let the next Ctrl+S
        overwrite the pulled version with the pre-pull contents.
        """
        self._library_tab.invalidate_card_cache()
        self._library_tab.load_cards()
        selected = self._sidebar.current_id()
        if selected is not None and self._edit_tab.current_id() == selected:
            self._edit_tab.reload_current()
        self._st_status.refresh()

    def _shutdown_st_workers(self, timeout_ms: int = 3000) -> bool:
        """Cancel background workers and wait briefly.

        Returns True when no worker is running any more (or stopped within
        the timeout). False means the window must stay open — destroying a
        live QThread aborts the process. Covers the backup/restore workers
        too: they are unparented and would otherwise be garbage-collected
        mid-run when the window closes right after a cancelled progress
        dialog.

        The wait is bounded by a *shared* deadline: every worker is asked to
        stop first, then they are awaited together.  Waiting on each one
        with the full timeout in turn could block the shutdown for
        5 x timeout_ms (15 s) with no feedback.
        """
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        workers: list[tuple[str, object]] = []
        for attr in ('_st_plan_worker', '_st_pushpull_worker', '_st_bulk_worker',
                     '_backup_worker', '_restore_worker', '_encrypted_backup_worker',
                     '_db_backup_worker'):
            worker = getattr(self, attr, None)
            if not self._worker_is_alive(worker):
                setattr(self, attr, None)
                continue
            try:
                worker.cancel()
            except AttributeError:
                # Backup/restore workers expose a cooperative flag instead.
                try:
                    worker.cancelled = True
                except Exception:
                    pass
            workers.append((attr, worker))
        ok = True
        for attr, worker in workers:
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            try:
                finished = worker.wait(remaining_ms)
            except RuntimeError:
                finished = True
            if finished:
                setattr(self, attr, None)
            else:
                ok = False
        from src.ui.edit_tab import wait_for_token_warmup
        wait_for_token_warmup(timeout_ms)
        return ok

    def _on_st_refresh(self) -> None:
        self._st_status.refresh()
        self.statusBar().showMessage('ST status refreshed.', 3000)

    def _setup_st_watcher(self) -> None:
        """Set up a QFileSystemWatcher on the ST characters directory."""
        path = load_st_characters_path()
        if not path:
            return
        from PyQt6.QtCore import QFileSystemWatcher
        if not hasattr(self, '_st_watcher'):
            self._st_watcher = QFileSystemWatcher(self)
            self._st_watcher.directoryChanged.connect(self._on_st_dir_changed)
        else:
            current = self._st_watcher.directories()
            if current:
                self._st_watcher.removePaths(current)
        if Path(path).is_dir():
            self._st_watcher.addPath(path)

    def _on_st_dir_changed(self, path: str) -> None:
        """Called when the ST characters directory changes."""
        self._st_status.refresh()
        self.statusBar().showMessage(
            'SillyTavern directory changed — open Sync to update.', 5000,
        )

    # ---- Dirty-state guard ----

    def _prompt_dirty_switch(self) -> str:
        """Ask the user what to do with unsaved edit-tab changes.

        Returns ``'save'``, ``'discard'``, or ``'cancel'``.
        """
        reply = QMessageBox.warning(
            self, 'Unsaved Changes',
            'The current card has unsaved changes.\n'
            'What would you like to do?',
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Save:
            if self._edit_tab.save():
                return 'save'
            return 'cancel'
        if reply == QMessageBox.StandardButton.Discard:
            # Actually discard: reloading the card clears the form buffer so
            # a later Ctrl+S cannot resurrect the thrown-away content.
            self._edit_tab.discard_edits()
            return 'discard'
        return 'cancel'

    def _go_to_edit(self, char_id: int) -> None:
        if self._edit_tab.is_dirty():
            result = self._prompt_dirty_switch()
            if result == 'cancel':
                return
        self._sidebar.set_selected_id(char_id)
        self._edit_tab.set_selected_id(char_id)
        self._ai_tab.select_card(char_id)
        self._gallery_tab.set_selected_card(char_id)
        self._lorebooks_tab.set_selected_card(char_id)
        self._tabs.setCurrentWidget(self._edit_tab)

    def _on_card_selected(self, char_id: int) -> None:
        """Sync a Library-grid selection to the shared sidebar and the tabs."""
        # select_card (not just set_selected_id): the sidebar follows the
        # Library click by highlighting the card AND scrolling it into view,
        # so the same card is findable in both lists.
        self._sidebar.select_card(char_id)
        self._ai_tab.select_card(char_id)
        self._edit_tab.set_selected_id(char_id)
        self._gallery_tab.set_selected_card(char_id)
        self._lorebooks_tab.set_selected_card(char_id)

    def _on_sidebar_card_selected(self, char_id: int) -> None:
        """Handle a card selection from the shared sidebar."""
        if self._edit_tab.is_dirty() and char_id != self._edit_tab._current_id:
            result = self._prompt_dirty_switch()
            if result == 'cancel':
                self._sidebar.set_selected_id(self._edit_tab._current_id)
                return
        self._sidebar.set_selected_id(char_id)
        self._edit_tab.set_selected_id(char_id)
        self._ai_tab.select_card(char_id)
        self._gallery_tab.set_selected_card(char_id)
        self._lorebooks_tab.set_selected_card(char_id)
        self._library_tab.reveal_card(char_id, clear_filters=True)
        widget = self._tabs.currentWidget()
        if isinstance(widget, EditTab):
            self._edit_tab.load_card_by_id(char_id)
        elif isinstance(widget, TestTab):
            self._test_tab.select_card(char_id)
        # GalleryTab is intentionally NOT re-touched here: set_selected_card()
        # above already refreshed it while visible (or deferred the load).
        # AITab likewise: select_card() above already ran, and calling it again
        # re-parsed the PNG and re-counted its tokens for no reason.

    def _on_settings(self) -> None:
        from src.ui.widgets.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.settings_changed.connect(self._on_settings_changed)
        dlg.exec()

    def _on_settings_changed(self) -> None:
        self._ai_tab.reload_preset()
        self._test_tab.reload_preset()
        self._lorebooks_tab.reload_preset()
        from src.ui.spell_check import apply_enabled_to_all

        apply_enabled_to_all()

    def _on_tab_changed(self, index: int) -> None:
        if self._suppress_tab_change:
            return
        widget = self._tabs.widget(index)
        # Guard: if leaving the Edit tab with unsaved changes, prompt.
        if not isinstance(widget, EditTab) and self._edit_tab.is_dirty():
            result = self._prompt_dirty_switch()
            if result == 'cancel':
                self._suppress_tab_change = True
                self._tabs.setCurrentWidget(self._edit_tab)
                self._suppress_tab_change = False
                return

        is_library = isinstance(widget, LibraryTab)
        self._sidebar.setVisible(not is_library)
        if is_library:
            # Keep the grid in step with the shared sidebar's selection: the
            # chosen card is selected and scrolled into view, with any filters
            # hiding it cleared so the follow always succeeds.
            self._library_tab.reveal_card(self._sidebar.current_id())
            return

        sid = self._sidebar.current_id()
        if isinstance(widget, EditTab):
            # Skip the reload when this card is already in the form: re-parsing
            # the PNG is slow and it reset the Ctrl+Z history on every visit.
            if sid is not None and sid != widget.form_loaded_for_id:
                widget.load_card_by_id(sid)
        elif isinstance(widget, AITab):
            if sid is not None:
                widget.select_card(sid)
        elif isinstance(widget, GalleryTab):
            # Cheap when already current; loads when the sidebar's restored
            # selection has never reached the gallery.
            if sid is not None:
                widget.set_selected_card(sid)
        elif isinstance(widget, TestTab):
            if sid is not None:
                widget.select_card(sid)

    def closeEvent(self, event: QCloseEvent) -> None:
        # Guard: prompt if the Edit tab has unsaved changes.
        if self._edit_tab.is_dirty():
            reply = QMessageBox.warning(
                self, 'Unsaved Changes',
                'The current card has unsaved changes. Save before closing?',
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Save:
                if not self._edit_tab.save():
                    event.ignore()
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
        # Wait briefly for any running AI generation workers to finish/cancel
        # before closing, to avoid incomplete writes or dangling threads.
        if not self._ai_tab.cleanup_workers(timeout_ms=3000):
            logger.warning("AI workers did not finish within timeout; keeping window open")
            QMessageBox.warning(
                self, 'Still Generating',
                'A generation task did not cancel in time.\n'
                'The window stays open — please wait for it to finish or try again.',
            )
            event.ignore()
            return
        # Likewise cancel any running card import worker (Phase 8A).
        if not self._library_tab.cleanup_workers(timeout_ms=3000):
            # Keep the window open: closing now would destroy a live QThread
            # and abort the process ("QThread: Destroyed while running").
            logger.warning("Import worker did not finish within timeout; keeping window open")
            QMessageBox.warning(
                self, 'Import In Progress',
                'A card import did not cancel in time.\n'
                'The window stays open — please wait for it to finish or try again.',
            )
            event.ignore()
            return
        # Cancel any running gallery image import worker.
        if not self._gallery_tab.cleanup_workers(timeout_ms=3000):
            logger.warning("Gallery import worker did not finish within timeout; keeping window open")
            QMessageBox.warning(
                self, 'Import In Progress',
                'A gallery import did not cancel in time.\n'
                'The window stays open — please wait for it to finish or try again.',
            )
            event.ignore()
            return
        # Cancel any running Test-tab chat worker.
        if not self._test_tab.cleanup_workers(timeout_ms=3000):
            logger.warning("Test chat worker did not finish within timeout; keeping window open")
            QMessageBox.warning(
                self, 'Still Generating',
                'A chat response did not cancel in time.\n'
                'The window stays open — please wait for it to finish or try again.',
            )
            event.ignore()
            return
        # Flush pending lorebook autosave and cancel its generation workers.
        self._lorebooks_tab.flush()
        if not self._lorebooks_tab.cleanup_workers(timeout_ms=3000):
            logger.warning("Lorebook generation worker did not finish within timeout; keeping window open")
            QMessageBox.warning(
                self, 'Still Generating',
                'A lorebook generation did not cancel in time.\n'
                'The window stays open — please wait for it to finish or try again.',
            )
            event.ignore()
            return
        # Cancel any running SillyTavern sync workers.
        if not self._shutdown_st_workers(timeout_ms=3000):
            logger.warning("SillyTavern sync worker did not finish within timeout; keeping window open")
            QMessageBox.warning(
                self, 'Sync In Progress',
                'A SillyTavern sync did not stop in time.\n'
                'The window stays open — please wait for it to finish or try again.',
            )
            event.ignore()
            return
        # Stop the ST status scan: it runs on a child QThread, and destroying a
        # live QThread with its parent aborts the process.
        try:
            self._st_status.shutdown()
        except RuntimeError:
            pass
        save_window_geometry(self.saveGeometry())
        # Persist tab state for next session.
        save_library_selected_ids(self._library_tab.get_selected_ids())
        save_library_scroll_position(self._library_tab.get_scroll_position())
        save_edit_selected_id(self._sidebar.current_id())
        super().closeEvent(event)

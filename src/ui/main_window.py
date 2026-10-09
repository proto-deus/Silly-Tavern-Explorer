from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
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
    save_library_selected_ids, load_library_selected_ids,
    save_library_scroll_position, load_library_scroll_position,
    save_edit_selected_id, load_edit_selected_id,
)
from src.sillytavern_sync import detect_st_installs
from src.ui.ai_tab import AITab
from src.ui.edit_tab import EditTab
from src.ui.library_tab import LibraryTab
from src.ui.test_tab import TestTab
from src.ui.widgets.lorebooks_tab import LorebooksTab
from src.ui.shortcuts import ACTIONS
from src.ui.widgets.character_sidebar import CharacterSidebar

logger = logging.getLogger(__name__)


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


class MainWindow(QMainWindow):
    def __init__(self, db: LibraryDatabase):
        super().__init__()
        self.db = db
        self._suppress_tab_change = False
        self._actions: dict[str, QAction] = {}
        self._st_pushpull_worker = None
        self._st_bulk_worker = None
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
        self._ai_tab = AITab(db)
        self._lorebooks_tab = LorebooksTab(db)
        self._test_tab = TestTab(db)

        self._tabs.addTab(self._library_tab, 'Library')
        self._tabs.addTab(self._edit_tab, 'Edit')
        self._tabs.addTab(self._ai_tab, 'Generate')
        self._tabs.addTab(self._lorebooks_tab, 'Lorebooks')
        self._tabs.addTab(self._test_tab, 'Test')

        self._stats_label = QLabel()
        self._stats_label.setStyleSheet('color: #aaa; padding: 0 6px;')

        self._library_tab.edit_requested.connect(self._go_to_edit)
        self._library_tab.generate_tags_requested.connect(self._go_to_ai_tags)
        self._library_tab.library_changed.connect(self._update_status)
        self._library_tab.library_changed.connect(self._edit_tab.refresh_tag_completer)
        self._library_tab.library_changed.connect(self._sidebar.load_cards)
        self._library_tab.status_message.connect(self._show_status_message)
        self._library_tab.sort_label_changed.connect(self._sync_sort_menu)
        self._edit_tab.status_message.connect(self._show_status_message)
        self._edit_tab.card_deleted.connect(self._on_edit_card_deleted)
        self._edit_tab.card_updated.connect(self._on_edit_card_updated)
        self._edit_tab.card_added.connect(self._on_edit_card_added)
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

    def _build_menus(self) -> None:
        """Build the menu bar from the centralised shortcuts registry."""
        menubar = self.menuBar()
        menus: dict[str, QMenu] = {}

        def get_menu(path: str) -> QMenu:
            if path in menus:
                return menus[path]
            parts = path.split('/')
            if len(parts) == 1:
                menu = menubar.addMenu(parts[0])
            else:
                parent = get_menu('/'.join(parts[:-1]))
                menu = parent.addMenu(parts[-1])
            menus[path] = menu
            return menu

        sort_group = QActionGroup(self)
        sort_group.setExclusive(True)
        sort_labels = {
            'view.sort_name': 'Name',
            'view.sort_date': 'Date Added',
            'view.sort_tokens': 'Token Count',
            'view.sort_favorites': 'Favorites',
            'view.sort_random': 'Random',
        }

        for action_def in ACTIONS:
            menu = get_menu(action_def.menu_path)
            if action_def.separator_before:
                menu.addSeparator()
            act = QAction(action_def.label, self)
            if action_def.shortcut:
                act.setShortcut(QKeySequence(action_def.shortcut))
            if action_def.action_id in sort_labels:
                act.setCheckable(True)
                sort_group.addAction(act)
            menu.addAction(act)
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
            'view.sort_random': lambda: self._on_view_sort('Random'),
            'view.refresh': self._on_view_refresh,
            'view.zoom_in': lambda: self._on_view_zoom(16),
            'view.zoom_out': lambda: self._on_view_zoom(-16),
            'view.font_size': self._on_view_font_size,
            'view.find_duplicates': self._on_view_find_duplicates,
            'view.statistics': self._on_view_statistics,
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
        }
        for action_id, handler in routing.items():
            action = self._actions.get(action_id)
            if action is not None:
                action.triggered.connect(handler)

    def _sync_sort_menu(self, label: str) -> None:
        """Update the checked sort action when the combo changes."""
        mapping = {
            'Name': 'view.sort_name',
            'Date Added': 'view.sort_date',
            'Token Count': 'view.sort_tokens',
            'Favorites': 'view.sort_favorites',
            'Random': 'view.sort_random',
        }
        action_id = mapping.get(label)
        if action_id and action_id in self._actions:
            self._actions[action_id].setChecked(True)

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
        try:
            bak_path = self.db.backup()
            self.statusBar().showMessage(f"Backup saved to {bak_path.name}", 5000)
        except Exception as e:
            QMessageBox.critical(self, 'Backup', f"Failed to create backup: {e}")

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

    def _on_backup_library_cancel(self) -> None:
        if getattr(self, '_backup_worker', None) is not None:
            try:
                self._backup_worker.cancelled = True
            except RuntimeError:
                pass

    def _on_backup_library_ok(self, manifest: dict) -> None:
        progress = getattr(self, '_backup_progress', None)
        if progress is not None:
            progress.reset()
        counts = manifest.get('counts', {})
        self.statusBar().showMessage(
            f"Library backup saved ({counts.get('cards', 0)} cards)", 5000,
        )

    def _on_backup_library_fail(self, message: str) -> None:
        progress = getattr(self, '_backup_progress', None)
        if progress is not None:
            progress.reset()
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

    def _on_restore_library_cancel(self) -> None:
        # Past the point of no return (files are being swapped) a cancel
        # can't be honoured safely; it stops *before* extraction starts.
        if getattr(self, '_restore_worker', None) is not None:
            try:
                self._restore_worker.cancelled = True
            except RuntimeError:
                pass

    def _on_restore_library_ok(self, _manifest: dict) -> None:
        progress = getattr(self, '_restore_progress', None)
        if progress is not None:
            progress.reset()
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
        progress = getattr(self, '_restore_progress', None)
        if progress is not None:
            progress.reset()
        QMessageBox.critical(self, 'Restore Library', f'Restore failed:\n{message}')

    def _restart_application(self) -> None:
        """Close the window and launch a fresh instance of the app."""
        import subprocess
        import sys
        try:
            cmd = build_restart_command(
                sys.executable, list(sys.argv), frozen=bool(getattr(sys, 'frozen', False)),
            )
            subprocess.Popen(cmd)
        except Exception:
            logger.exception("Restart failed; falling back to plain close")
        self.close()

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
        # 'Del' is a window-wide shortcut, and a read-only QTextEdit does not
        # claim it - so pressing Delete while reading the Library detail pane
        # used to delete the selected card. Only honour it on the Library tab,
        # and not while a text field has focus.
        widget = self._tabs.currentWidget()
        if widget is not self._library_tab:
            return
        if isinstance(QApplication.focusWidget(), (QLineEdit, QTextEdit, QPlainTextEdit)):
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

    def _on_edit_find(self) -> None:
        self._tabs.setCurrentWidget(self._library_tab)
        self._library_tab.focus_search()

    # ---- View handlers ----

    def _on_view_favorites(self) -> None:
        # 'F' is a window-wide shortcut and a read-only QTextEdit does not
        # claim it, so clicking into the Library detail pane and pressing F
        # toggled the filter. Scope it to the Library tab and ignore it while a
        # text field has focus.
        if self._tabs.currentWidget() is not self._library_tab:
            return
        if isinstance(QApplication.focusWidget(), (QLineEdit, QTextEdit, QPlainTextEdit)):
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
        dlg = QDialog(self)
        dlg.setWindowTitle('Font Size')
        dlg.setMinimumWidth(250)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel('Set the application font size (px):'))
        row = QHBoxLayout()
        spin = QSpinBox()
        spin.setRange(8, 32)
        spin.setValue(load_font_size())
        row.addWidget(spin)
        layout.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            size = spin.value()
            save_font_size(size)
            from PyQt6.QtWidgets import QApplication
            from src.ui.styles import apply_theme
            apply_theme(QApplication.instance(), size)
            self._test_tab.refresh_font_size()
            self._library_tab.refresh_font_size()

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
        dlg.exec()

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
        progress = getattr(self, '_st_progress', None)
        if progress is not None:
            progress.close()
            self._st_progress = None
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
        """
        ok = True
        for attr in ('_st_plan_worker', '_st_pushpull_worker', '_st_bulk_worker',
                     '_backup_worker', '_restore_worker'):
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
            if not worker.wait(timeout_ms):
                ok = False
            else:
                setattr(self, attr, None)
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
        self._sidebar.select_card(char_id)
        self._edit_tab.set_selected_id(char_id)
        self._ai_tab.select_card(char_id)
        self._lorebooks_tab.set_selected_card(char_id)
        self._tabs.setCurrentWidget(self._edit_tab)

    def _go_to_ai_tags(self, char_id: int) -> None:
        self._sidebar.select_card(char_id)
        self._ai_tab.select_card(char_id)
        self._edit_tab.set_selected_id(char_id)
        self._lorebooks_tab.set_selected_card(char_id)
        self._tabs.setCurrentWidget(self._ai_tab)

    def _on_card_selected(self, char_id: int) -> None:
        """Sync a Library-grid selection to the shared sidebar and the tabs."""
        self._sidebar.set_selected_id(char_id)
        self._ai_tab.select_card(char_id)
        self._edit_tab.set_selected_id(char_id)
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
        self._lorebooks_tab.set_selected_card(char_id)
        widget = self._tabs.currentWidget()
        if isinstance(widget, EditTab):
            self._edit_tab.load_card_by_id(char_id)
        elif isinstance(widget, TestTab):
            self._test_tab.select_card(char_id)
        # AITab is intentionally NOT re-selected here: select_card() above
        # already ran, and calling it again re-parsed the PNG and re-counted its
        # tokens for no reason.

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
            return

        if self._sidebar.current_id() is not None:
            self._sidebar.scroll_selected_to_top()

        sid = self._sidebar.current_id()
        if isinstance(widget, EditTab):
            # Skip the reload when this card is already in the form: re-parsing
            # the PNG is slow and it reset the Ctrl+Z history on every visit.
            if sid is not None and sid != widget.form_loaded_for_id:
                widget.load_card_by_id(sid)
        elif isinstance(widget, AITab):
            if sid is not None:
                widget.select_card(sid)
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

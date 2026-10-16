"""Gallery tab: per-character extra images, ST expression images, V3 assets.

Three panes over one character (selected via the shared sidebar):

* **Gallery** — extra images kept in the library under
  ``gallery/<char_id>/`` (add, drag-reorder, caption, export, set as the
  card's portrait);
* **Expressions** — SillyTavern expression images managed directly in
  ``<st user images>/<charName>/`` so ST sees every change;
* **Card Assets** — read-only view of the V3 ``assets`` array, with
  previews when the referenced file can be found on disk.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src import gallery_store

logger = logging.getLogger(__name__)

GALLERY_LOADER_OWNER = 'gallery'

# Qt item roles used by both list panes.
ROLE_PATH = 256            # full path to the image ('' when missing)
ROLE_CAPTION = 257         # gallery caption / expression display name
ROLE_IMAGE_ID = 258        # gallery DB row id (gallery pane only)
ROLE_FILE_NAME = 259       # stored file name (gallery pane only)

IMAGE_FILE_FILTER = 'Image Files (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;All Files (*)'


def _item_alive(item) -> bool:
    """True when *item*'s C++ object still exists.

    ``QListWidget.clear()`` deletes its items immediately while PyQt keeps the
    Python wrapper; calling any method on a dead wrapper raises RuntimeError,
    which - inside a slot - aborts the whole process.
    """
    if item is None:
        return False
    try:
        from PyQt6 import sip
    except ImportError:  # pragma: no cover - PyQt6 always ships sip
        pass
    else:
        try:
            return not sip.isdeleted(item)
        except (TypeError, RuntimeError):
            return False
    try:
        item.data(0)
        return True
    except RuntimeError:
        return False


def filter_image_paths(paths: list[str]) -> list[str]:
    """Return only the paths whose suffix is a supported image type.

    Pure function so the drop-target filter can be unit-tested without Qt.
    """
    return [p for p in paths if gallery_store.is_image_file(p)]


def expression_slots(
    found: dict[str, Path],
    defaults: tuple[str, ...] = gallery_store.DEFAULT_EXPRESSIONS,
) -> list[tuple[str, str]]:
    """Merge the default ST expressions with any custom ones on disk.

    Returns ``(display_name, key)`` pairs: the defaults first (whether or not
    an image exists, so a missing slot is visible as a gap), then custom
    expressions sorted by name.
    """
    slots: list[tuple[str, str]] = [(name, name) for name in defaults]
    for key in sorted(found):
        if key not in defaults:
            slots.append((key, key))
    return slots


def format_pane_counts(gallery: int, expressions: int,
                       missing_defaults: int) -> str:
    """Header summary line for the tab, e.g. ``12 gallery · 5/7 expressions``."""
    parts = [f'{gallery} gallery']
    if missing_defaults:
        parts.append(f'{expressions} expressions ({missing_defaults} missing)')
    else:
        parts.append(f'{expressions} expressions')
    return ' · '.join(parts)


def _empty_icon():
    """Transparent icon used to clear a slot's thumbnail."""
    from PyQt6.QtGui import QIcon, QPixmap
    pixmap = QPixmap(1, 1)
    pixmap.fill(Qt.GlobalColor.transparent)
    return QIcon(pixmap)


try:
    from PyQt6.QtCore import QSize, Qt, pyqtSignal
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QFileDialog,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QMenu,
        QMessageBox,
        QProgressDialog,
        QPushButton,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover - exercised only without Qt installed
    QWidget = None  # type: ignore[misc,assignment]


if QWidget is not None:

    class GalleryList(QListWidget):
        """Icon-mode list whose internal drag-reorder emits ``reordered``.

        ``rowsMoved`` is never fired for an InternalMove drop (the internal
        QListModel swallows the change), so persistence hangs off the drop
        itself instead.
        """

        reordered = pyqtSignal()

        def dropEvent(self, event) -> None:
            super().dropEvent(event)
            self.reordered.emit()


    class GalleryTab(QWidget):
        """Browse and manage one character's gallery and expression images."""

        status_message = pyqtSignal(str, int)
        settings_requested = pyqtSignal()
        card_updated = pyqtSignal(int)

        def __init__(self, db, parent=None):
            super().__init__(parent)
            self.db = db
            self._char_id: int | None = None
            self._entry: dict | None = None
            self._load_pending = False
            self._generation = 0
            self._thumb_paths: dict[str, object] = {}
            self._worker = None
            self._progress: QProgressDialog | None = None
            self._lightbox = None
            self._build_ui()

        # ---- UI construction ----

        def _build_ui(self) -> None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(6, 6, 6, 6)

            header = QHBoxLayout()
            self._title_label = QLabel('No character selected')
            self._title_label.setStyleSheet('font-weight: bold; font-size: 14px;')
            header.addWidget(self._title_label)
            self._count_label = QLabel('')
            self._count_label.setStyleSheet('color: #888;')
            header.addWidget(self._count_label)
            header.addStretch()

            self._add_btn = QPushButton('Add Images...')
            self._add_btn.clicked.connect(self._on_add_images)
            header.addWidget(self._add_btn)
            self._folder_btn = QPushButton('Import Folder...')
            self._folder_btn.clicked.connect(self._on_import_folder)
            header.addWidget(self._folder_btn)
            self._export_btn = QPushButton('Export All...')
            self._export_btn.clicked.connect(self._on_export_all)
            header.addWidget(self._export_btn)
            self._refresh_btn = QPushButton('Refresh')
            self._refresh_btn.clicked.connect(self.refresh)
            header.addWidget(self._refresh_btn)
            layout.addLayout(header)

            self._panes = QTabWidget()
            layout.addWidget(self._panes, 1)

            self._gallery_list = self._make_list(drag_reorder=True)
            self._gallery_list.itemDoubleClicked.connect(self._on_gallery_double_clicked)
            self._gallery_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._gallery_list.customContextMenuRequested.connect(self._on_gallery_menu)
            self._gallery_list.reordered.connect(self._on_rows_moved)
            self._gallery_hint = QLabel('')
            self._gallery_hint.setWordWrap(True)
            self._gallery_hint.setStyleSheet('color: #888;')
            self._panes.addTab(self._wrap_with_hint(
                self._gallery_hint, self._gallery_list,
            ), 'Gallery')

            expr_pane = QWidget()
            expr_layout = QVBoxLayout(expr_pane)
            expr_layout.setContentsMargins(0, 0, 0, 0)
            self._expr_hint = QLabel('')
            self._expr_hint.setWordWrap(True)
            self._expr_hint.setStyleSheet('color: #888;')
            expr_layout.addWidget(self._expr_hint)
            self._expr_list = self._make_list()
            self._expr_list.itemDoubleClicked.connect(self._on_expr_double_clicked)
            self._expr_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._expr_list.customContextMenuRequested.connect(self._on_expr_menu)
            expr_layout.addWidget(self._expr_list, 1)
            self._panes.addTab(expr_pane, 'Expressions')

            self._assets_list = self._make_list()
            self._assets_list.itemDoubleClicked.connect(self._on_asset_double_clicked)
            self._panes.addTab(self._wrap_with_hint(
                QLabel('Declared V3 assets for this card (read-only).'),
                self._assets_list,
            ), 'Card Assets')

            self._panes.currentChanged.connect(lambda _i: self._update_actions())
            self.setAcceptDrops(True)
            self._update_actions()

        def _wrap_with_hint(self, hint: QLabel, lst: QListWidget) -> QWidget:
            pane = QWidget()
            layout = QVBoxLayout(pane)
            layout.setContentsMargins(0, 0, 0, 0)
            hint.setStyleSheet('color: #888;')
            layout.addWidget(hint)
            layout.addWidget(lst, 1)
            return pane

        def _make_list(self, drag_reorder: bool = False) -> QListWidget:
            lst = GalleryList() if drag_reorder else QListWidget()
            lst.setViewMode(QListWidget.ViewMode.IconMode)
            lst.setIconSize(QSize(112, 112))
            lst.setResizeMode(QListWidget.ResizeMode.Adjust)
            lst.setWrapping(True)
            lst.setSpacing(8)
            lst.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            lst.setDragDropMode(
                QAbstractItemView.DragDropMode.InternalMove if drag_reorder
                else QAbstractItemView.DragDropMode.NoDragDrop
            )
            return lst

        # ---- selection / loading ----

        def set_selected_card(self, char_id: int | None) -> None:
            """Record the shared sidebar selection (cheap; loads when shown)."""
            if char_id == self._char_id and not self._load_pending:
                return
            self._char_id = char_id
            if self.isVisible():
                self.refresh()
            else:
                self._load_pending = True

        def showEvent(self, event) -> None:
            super().showEvent(event)
            if self._load_pending:
                self._load_pending = False
                self.refresh()

        def refresh(self) -> None:
            """Rebuild all three panes for the selected character."""
            self._generation += 1
            self._thumb_paths.clear()
            char_id = self._char_id
            self._entry = self.db.get_by_id(char_id) if char_id is not None else None
            if self._entry is None:
                self._title_label.setText('No character selected')
                self._count_label.setText('')
                for lst in (self._gallery_list, self._expr_list, self._assets_list):
                    lst.clear()
                self._update_actions()
                return

            self._title_label.setText(self._entry.get('name') or 'Unnamed')
            self._refresh_gallery()
            self._refresh_expressions()
            self._refresh_assets()
            missing = 0
            exp_dir = self._expression_dir()
            if exp_dir is not None:
                found = gallery_store.list_expression_images(exp_dir)
                missing = sum(
                    1 for name in gallery_store.DEFAULT_EXPRESSIONS
                    if name not in found
                )
                expr_count = len(found)
            else:
                expr_count = 0
            self._count_label.setText(format_pane_counts(
                self._gallery_list.count(), expr_count, missing,
            ))
            self._update_actions()

        def _update_actions(self) -> None:
            enabled = self._entry is not None
            self._add_btn.setEnabled(enabled)
            self._folder_btn.setEnabled(enabled)
            self._export_btn.setEnabled(enabled and self._gallery_list.count() > 0)

        # ---- gallery pane ----

        def _refresh_gallery(self) -> None:
            self._forget_thumbs(self._gallery_list)
            self._gallery_list.clear()
            rows = self.db.list_gallery_images(self._char_id)
            self._gallery_hint.setText(
                f'{len(rows)} image(s). Add with Add Images..., or drop '
                'image files here. Drag thumbnails to reorder.'
                if rows else
                'No gallery images yet. Use Add Images..., or drop '
                'image files here.'
            )
            for row in rows:
                path = str(gallery_store.gallery_image_path(
                    self._char_id, row['file_name'],
                ))
                caption = row.get('caption') or Path(row['file_name']).stem
                item = QListWidgetItem(caption)
                item.setData(ROLE_PATH, path if Path(path).is_file() else '')
                item.setData(ROLE_CAPTION, row.get('caption') or '')
                item.setData(ROLE_IMAGE_ID, row['id'])
                item.setData(ROLE_FILE_NAME, row['file_name'])
                item.setToolTip(f"{row['file_name']}\n{row.get('caption') or ''}".strip())
                item.setSizeHint(QSize(128, 150))
                self._gallery_list.addItem(item)
                if item.data(ROLE_PATH):
                    self._request_thumb(item, path)

        def _request_thumb(self, item: QListWidgetItem, path: str) -> None:
            from src.ui.widgets.async_image import AsyncImageLoader, cache_key
            generation = self._generation
            key = cache_key(path, 112)
            loader = AsyncImageLoader(path, 112, context=(key, generation),
                                      owner=GALLERY_LOADER_OWNER)
            loader.signals.loaded.connect(self._on_thumb_loaded)
            self._thumb_paths[key] = item
            loader.submit()

        def _forget_thumbs(self, lst: QListWidget) -> None:
            """Drop thumb registrations for *lst*'s current items.

            Called just before the list is cleared so a late thumbnail
            callback cannot look up a deleted ``QListWidgetItem``.
            """
            for i in range(lst.count()):
                item = lst.item(i)
                path = item.data(ROLE_PATH) or ''
                if path:
                    from src.ui.widgets.async_image import cache_key
                    self._thumb_paths.pop(cache_key(path, 112), None)

        def _on_thumb_loaded(self, qimg, path, _target_px, context) -> None:
            from PyQt6.QtGui import QPixmap
            try:
                key, generation = context
            except (TypeError, ValueError):
                return
            if generation != self._generation or qimg is None:
                return
            item = self._thumb_paths.get(key)
            if item is None:
                return
            # The item may have been deleted (list cleared), removed or
            # re-pointed since the request; touching a dead wrapper aborts.
            if not _item_alive(item):
                self._thumb_paths.pop(key, None)
                return
            if item.data(ROLE_PATH) not in ('', path):
                return
            pixmap = QPixmap.fromImage(qimg)
            item.setIcon(pixmap)

        def _on_rows_moved(self, *args) -> None:
            """Persist a drag-reorder of gallery thumbnails."""
            if self._char_id is None:
                return
            order = []
            for i in range(self._gallery_list.count()):
                image_id = self._gallery_list.item(i).data(ROLE_IMAGE_ID)
                if image_id is not None:
                    order.append(int(image_id))
            try:
                self.db.set_gallery_order(self._char_id, order)
            except Exception:
                logger.exception("Could not persist gallery order")

        def _on_gallery_double_clicked(self, item: QListWidgetItem) -> None:
            self._open_lightbox(self._gallery_items_with_paths(), self._item_index(item))

        def _gallery_items_with_paths(self) -> tuple[list[str], list[str]]:
            paths, labels = [], []
            for i in range(self._gallery_list.count()):
                item = self._gallery_list.item(i)
                path = item.data(ROLE_PATH) or ''
                if path:
                    paths.append(path)
                    labels.append(item.text())
            return paths, labels

        def _item_index(self, item: QListWidgetItem) -> int:
            path = item.data(ROLE_PATH) or ''
            paths, _ = self._gallery_items_with_paths()
            try:
                return paths.index(path)
            except ValueError:
                return 0

        def _on_gallery_menu(self, pos) -> None:
            items = self._gallery_list.selectedItems()
            if not items:
                return
            path = items[0].data(ROLE_PATH) or ''
            menu = QMenu(self._gallery_list)
            open_act = menu.addAction('Open')
            open_act.triggered.connect(lambda: self._open_lightbox(
                self._gallery_items_with_paths(), self._item_index(items[0]),
            ))
            card_act = menu.addAction('Set as Card Image...')
            card_act.triggered.connect(lambda: self._set_as_card_image(path))
            caption_act = menu.addAction('Edit Caption...')
            caption_act.triggered.connect(lambda: self._edit_caption(items[0]))
            export_act = menu.addAction('Export This...')
            export_act.triggered.connect(lambda: self._export_one(path))
            reveal_act = menu.addAction('Reveal in Folder')
            reveal_act.triggered.connect(lambda: self._reveal(path))
            menu.addSeparator()
            delete_act = menu.addAction('Delete')
            delete_act.triggered.connect(self._delete_gallery_items)
            menu.exec(self._gallery_list.viewport().mapToGlobal(pos))

        def _edit_caption(self, item: QListWidgetItem) -> None:
            image_id = item.data(ROLE_IMAGE_ID)
            current = item.data(ROLE_CAPTION) or ''
            text, ok = QInputDialog.getText(
                self, 'Edit Caption', 'Caption:', text=current,
            )
            if not ok:
                return
            try:
                self.db.set_gallery_caption(int(image_id), text)
            except Exception:
                logger.exception("Could not save gallery caption")
                return
            item.setData(ROLE_CAPTION, text)
            item.setText(text or Path(item.data(ROLE_FILE_NAME)).stem)

        def _delete_gallery_items(self) -> None:
            items = self._gallery_list.selectedItems()
            if not items:
                return
            reply = QMessageBox.question(
                self, 'Delete Images',
                f'Delete {len(items)} image(s) from the gallery?',
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            for item in items:
                row = self.db.delete_gallery_image(int(item.data(ROLE_IMAGE_ID)))
                if row:
                    gallery_store.delete_gallery_file(
                        row['character_id'], row['file_name'],
                    )
            self._refresh_gallery()
            self._update_actions()

        # ---- expressions pane ----

        def _user_images_dir(self) -> Path | None:
            from src.settings_manager import (
                load_st_characters_path, load_st_user_images_path,
            )
            configured = load_st_user_images_path().strip()
            if configured:
                return Path(configured)
            characters = load_st_characters_path().strip()
            if characters:
                return gallery_store.resolve_user_images_dir(characters)
            return None

        def _expression_dir(self) -> Path | None:
            user_images = self._user_images_dir()
            if user_images is None or self._entry is None:
                return None
            stem = gallery_store.expression_stem_for(
                self._entry.get('st_avatar_url'), self._entry.get('name') or '',
            )
            return gallery_store.expression_images_dir(user_images, stem)

        def _refresh_expressions(self) -> None:
            self._forget_thumbs(self._expr_list)
            self._expr_list.clear()
            user_images = self._user_images_dir()
            if user_images is None:
                self._expr_hint.setText(
                    'SillyTavern is not configured. Set the characters '
                    'directory in SillyTavern > Configure to manage '
                    'expression images.'
                )
                self._expr_list.setEnabled(False)
                return
            self._expr_list.setEnabled(True)
            exp_dir = self._expression_dir()
            found = gallery_store.list_expression_images(exp_dir) if exp_dir else {}
            slots = expression_slots(found)
            missing = sum(
                1 for name in gallery_store.DEFAULT_EXPRESSIONS if name not in found
            )
            self._expr_hint.setText(
                f'{len(found)} image(s) on disk'
                + (f', {missing} default slot(s) empty.' if missing else '.')
                + ' Files live in SillyTavern\'s user/images folder.'
            )
            for display_name, key in slots:
                path = str(found[key]) if key in found else ''
                item = QListWidgetItem(display_name if path else f'{display_name} (missing)')
                item.setData(ROLE_PATH, path)
                item.setData(ROLE_CAPTION, display_name)
                item.setData(ROLE_FILE_NAME, Path(path).name if path else '')
                item.setSizeHint(QSize(128, 150))
                if not path:
                    item.setForeground(self.palette().color(self.foregroundRole()))
                    item.setToolTip(f'No image for "{display_name}" yet.')
                else:
                    item.setToolTip(path)
                self._expr_list.addItem(item)
                if path:
                    self._request_thumb(item, path)

        def _on_expr_double_clicked(self, item: QListWidgetItem) -> None:
            path = item.data(ROLE_PATH) or ''
            if not path:
                self._replace_expression(item)
                return
            self._open_lightbox(self._expr_items_with_paths(), self._expr_item_index(item))

        def _expr_items_with_paths(self) -> tuple[list[str], list[str]]:
            paths, labels = [], []
            for i in range(self._expr_list.count()):
                item = self._expr_list.item(i)
                path = item.data(ROLE_PATH) or ''
                if path:
                    paths.append(path)
                    labels.append(item.data(ROLE_CAPTION) or item.text())
            return paths, labels

        def _expr_item_index(self, item: QListWidgetItem) -> int:
            path = item.data(ROLE_PATH) or ''
            paths, _ = self._expr_items_with_paths()
            try:
                return paths.index(path)
            except ValueError:
                return 0

        def _on_expr_menu(self, pos) -> None:
            item = self._expr_list.itemAt(pos)
            if item is None:
                return
            self._expr_list.setCurrentItem(item)
            path = item.data(ROLE_PATH) or ''
            menu = QMenu(self._expr_list)
            replace_act = menu.addAction('Add / Replace Image...')
            replace_act.triggered.connect(lambda: self._replace_expression(item))
            if path:
                open_act = menu.addAction('Open')
                open_act.triggered.connect(lambda: self._open_lightbox(
                    self._expr_items_with_paths(), self._expr_item_index(item),
                ))
                card_act = menu.addAction('Set as Card Image...')
                card_act.triggered.connect(lambda: self._set_as_card_image(path))
                copy_act = menu.addAction('Copy to Gallery')
                copy_act.triggered.connect(lambda: self._copy_to_gallery(path))
                reveal_act = menu.addAction('Reveal in Folder')
                reveal_act.triggered.connect(lambda: self._reveal(path))
                delete_act = menu.addAction('Delete')
                delete_act.triggered.connect(lambda: self._delete_expression(item))
            menu.exec(self._expr_list.viewport().mapToGlobal(pos))

        def _replace_expression(self, item: QListWidgetItem) -> None:
            if self._entry is None:
                return
            exp_dir = self._expression_dir()
            if exp_dir is None:
                self.status_message.emit(
                    'Set the SillyTavern user images directory first.', 5000,
                )
                self.settings_requested.emit()
                return
            source, _ = QFileDialog.getOpenFileName(
                self, 'Select Expression Image', '', IMAGE_FILE_FILTER,
            )
            if not source:
                return
            name = item.data(ROLE_CAPTION) or ''
            try:
                written = gallery_store.write_expression_image(exp_dir, name, source)
            except Exception as e:
                QMessageBox.critical(
                    self, 'Expression Image', f'Could not save the image: {e}',
                )
                return
            item.setData(ROLE_PATH, str(written))
            item.setText(name)
            item.setToolTip(str(written))
            self._request_thumb(item, str(written))
            self.status_message.emit(f'Expression "{name}" updated.', 4000)

        def _delete_expression(self, item: QListWidgetItem) -> None:
            exp_dir = self._expression_dir()
            name = item.data(ROLE_CAPTION) or ''
            if exp_dir is None:
                return
            reply = QMessageBox.question(
                self, 'Delete Expression Image',
                f'Delete the image for "{name}" from SillyTavern?',
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            if gallery_store.delete_expression_image(exp_dir, name):
                item.setData(ROLE_PATH, '')
                item.setText(f'{name} (missing)')
                item.setIcon(_empty_icon())
                self.status_message.emit(f'Expression "{name}" removed.', 4000)

        def _copy_to_gallery(self, path: str) -> None:
            if self._char_id is None or not path:
                return
            try:
                meta = gallery_store.import_gallery_image(self._char_id, path)
                self.db.add_gallery_image(
                    self._char_id, meta['file_name'], meta['content_hash'],
                )
            except Exception as e:
                QMessageBox.critical(self, 'Gallery', f'Could not copy the image: {e}')
                return
            self._refresh_gallery()
            self.status_message.emit('Copied to the gallery.', 4000)

        # ---- assets pane ----

        def _card_assets(self) -> list[dict]:
            if self._entry is None:
                return []
            source = self._entry.get('source_path') or ''
            if not source or not Path(source).is_file():
                return []
            try:
                from src.card_parser import read_card_data
                raw = read_card_data(source) or {}
            except Exception:
                logger.exception("Could not read card data for the assets pane")
                return []
            assets = raw.get('assets')
            if not isinstance(assets, list):
                data = raw.get('data')
                assets = data.get('assets') if isinstance(data, dict) else None
            return [a for a in (assets or []) if isinstance(a, dict)]

        def _refresh_assets(self) -> None:
            self._forget_thumbs(self._assets_list)
            self._assets_list.clear()
            entries = self._card_assets()
            if not entries:
                return
            source = self._entry.get('source_path') or ''
            card_dir = Path(source).parent if source else Path('.')
            user_images = self._user_images_dir()
            stem = ''
            if self._entry is not None:
                stem = gallery_store.expression_stem_for(
                    self._entry.get('st_avatar_url'),
                    self._entry.get('name') or '',
                )
            for entry in entries:
                uri = str(entry.get('uri') or '')
                if uri == 'ccdefault':
                    resolved = Path(source) if source else None
                else:
                    resolved = gallery_store.resolve_asset_path(
                        entry, card_dir, user_images, stem,
                    )
                label = gallery_store.asset_description(entry)
                item = QListWidgetItem(label if resolved else f'{label} (not found)')
                item.setData(ROLE_PATH, str(resolved) if resolved else '')
                item.setSizeHint(QSize(128, 150))
                if resolved:
                    item.setToolTip(str(resolved))
                    self._request_thumb(item, str(resolved))
                else:
                    item.setToolTip(uri or label)
                self._assets_list.addItem(item)

        def _on_asset_double_clicked(self, item: QListWidgetItem) -> None:
            path = item.data(ROLE_PATH) or ''
            if not path:
                return
            self._open_lightbox(self._assets_items_with_paths(),
                                self._assets_item_index(item))

        def _assets_items_with_paths(self) -> tuple[list[str], list[str]]:
            paths, labels = [], []
            for i in range(self._assets_list.count()):
                item = self._assets_list.item(i)
                path = item.data(ROLE_PATH) or ''
                if path:
                    paths.append(path)
                    labels.append(item.text())
            return paths, labels

        def _assets_item_index(self, item: QListWidgetItem) -> int:
            path = item.data(ROLE_PATH) or ''
            paths, _ = self._assets_items_with_paths()
            try:
                return paths.index(path)
            except ValueError:
                return 0

        # ---- shared actions ----

        def _open_lightbox(self, items: tuple[list[str], list[str]], index: int) -> None:
            paths, labels = items
            if not paths:
                return
            from src.ui.widgets.image_viewer import GalleryLightbox
            if self._lightbox is not None:
                self._lightbox.close()
                self._lightbox.deleteLater()
            name = (self._entry or {}).get('name') or 'Gallery'
            self._lightbox = GalleryLightbox(
                paths, index, labels=labels, title=name, parent=self,
            )
            self._lightbox.show()

        def _set_as_card_image(self, source_image: str) -> None:
            if self._entry is None or not source_image:
                return
            card_path = self._entry.get('source_path') or ''
            if not card_path or not Path(card_path).is_file():
                QMessageBox.warning(self, 'Card Image', 'The card file was not found.')
                return
            reply = QMessageBox.question(
                self, 'Set as Card Image',
                'Replace the character\'s portrait with this image?\n\n'
                'The card data is preserved.',
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            try:
                from src.card_parser import replace_card_image, save_thumbnail
                replace_card_image(card_path, source_image, card_path)
                thumb_path = self._entry.get('thumbnail_path') or ''
                if thumb_path and not save_thumbnail(card_path, thumb_path):
                    logger.warning("Thumbnail regeneration failed for %s", card_path)
            except Exception as e:
                QMessageBox.critical(self, 'Card Image', f'Failed to update the image: {e}')
                return
            self.card_updated.emit(int(self._entry['id']))
            self.status_message.emit('Character image updated.', 4000)

        def _reveal(self, path: str) -> None:
            if not path:
                return
            from src.path_utils import open_containing_folder
            open_containing_folder(path)

        def _export_one(self, path: str) -> None:
            if not path:
                return
            dest, _ = QFileDialog.getSaveFileName(
                self, 'Export Image', Path(path).name,
                'PNG Files (*.png);;All Files (*)',
            )
            if not dest:
                return
            try:
                from src import vault
                vault.copy_out(path, dest)
            except Exception as e:
                QMessageBox.critical(self, 'Export', f'Failed to export: {e}')

        def _on_export_all(self) -> None:
            if self._entry is None:
                return
            directory = QFileDialog.getExistingDirectory(self, 'Export Gallery Images To')
            if not directory:
                return
            name = self._entry.get('name') or 'character'
            from src.database import sanitize_filename
            base = Path(directory) / sanitize_filename(name)
            try:
                base.mkdir(parents=True, exist_ok=True)
                count = 0
                for i in range(self._gallery_list.count()):
                    item = self._gallery_list.item(i)
                    path = item.data(ROLE_PATH) or ''
                    if path:
                        from src import vault
                        vault.copy_out(path, base / item.data(ROLE_FILE_NAME))
                        count += 1
                exp_dir = self._expression_dir()
                found = gallery_store.list_expression_images(exp_dir) if exp_dir else {}
                if found:
                    expr_base = base / 'expressions'
                    expr_base.mkdir(exist_ok=True)
                    for _key, path in found.items():
                        from src import vault
                        vault.copy_out(path, expr_base / path.name)
                        count += 1
            except Exception as e:
                QMessageBox.critical(self, 'Export', f'Export failed: {e}')
                return
            self.status_message.emit(
                f'Exported {count} image(s) to {base}', 6000,
            )

        # ---- add / import ----

        def _on_add_images(self) -> None:
            paths, _ = QFileDialog.getOpenFileNames(
                self, 'Add Gallery Images', '', IMAGE_FILE_FILTER,
            )
            if paths:
                self._import_paths(filter_image_paths(paths))

        def _on_import_folder(self) -> None:
            directory = QFileDialog.getExistingDirectory(self, 'Import Images From Folder')
            if not directory:
                return
            paths = filter_image_paths([
                str(p) for p in sorted(Path(directory).iterdir()) if p.is_file()
            ])
            if not paths:
                self.status_message.emit('No image files in that folder.', 4000)
                return
            self._import_paths(paths)

        def _import_paths(self, paths: list[str]) -> None:
            if self._char_id is None or not paths:
                return
            if self._worker_alive(self._worker):
                self.status_message.emit(
                    'An import is already running; wait for it to finish.', 5000,
                )
                return
            from src.ui.widgets.gallery_import_worker import GalleryImportWorker
            worker = GalleryImportWorker(self._char_id, paths, self.db, self)
            self._worker = worker
            self._progress = QProgressDialog(
                'Adding images...', 'Cancel', 0, len(paths), self,
            )
            self._progress.setWindowTitle('Gallery')
            worker.progress.connect(self._on_import_progress)
            worker.completed.connect(self._on_import_completed)
            worker.error.connect(self._on_import_error)
            # Bind cleanup to this concrete worker: a stale ``finished``
            # delivery must never deleteLater() a newer, running worker.
            worker.finished.connect(lambda w=worker: self._on_import_finished(w))
            worker.start()

        @staticmethod
        def _worker_alive(worker) -> bool:
            if worker is None:
                return False
            try:
                return worker.isRunning()
            except RuntimeError:
                return False

        def _on_import_progress(self, done: int, total: int, name: str) -> None:
            if self._progress is not None:
                self._progress.setLabelText(f'Adding {name} ({done + 1}/{total})...')
                self._progress.setValue(done)

        def _on_import_completed(self, summary) -> None:
            self._refresh_gallery()
            self._update_actions()
            self.status_message.emit(summary.message(), 6000)

        def _on_import_error(self, message: str) -> None:
            QMessageBox.critical(self, 'Gallery', f'Import failed: {message}')

        def _on_import_finished(self, worker=None) -> None:
            if worker is not None and worker is not self._worker:
                # A superseded worker's trailing signal; it owns no progress
                # dialog and must not clear the live import's state.
                try:
                    worker.deleteLater()
                except RuntimeError:
                    pass
                return
            if self._progress is not None:
                self._progress.close()
                self._progress = None
            worker = self._worker
            self._worker = None
            if worker is not None:
                try:
                    worker.deleteLater()
                except RuntimeError:
                    pass

        # ---- drag & drop ----

        def _droppable_paths(self, mime) -> list[str]:
            if not mime.hasUrls():
                return []
            return filter_image_paths([
                u.toLocalFile() for u in mime.urls() if u.toLocalFile()
            ])

        def dragEnterEvent(self, event) -> None:
            if self._droppable_paths(event.mimeData()):
                event.acceptProposedAction()
            else:
                event.ignore()

        def dragMoveEvent(self, event) -> None:
            if self._droppable_paths(event.mimeData()):
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event) -> None:
            paths = self._droppable_paths(event.mimeData())
            if paths:
                self._import_paths(paths)
                event.acceptProposedAction()

        # ---- shutdown ----

        def cleanup_workers(self, timeout_ms: int = 3000) -> bool:
            """Cancel and wait for the import worker; False if still running."""
            worker = self._worker
            if worker is not None:
                try:
                    worker.cancel()
                    if not worker.wait(timeout_ms):
                        return False
                except RuntimeError:
                    pass
            return True

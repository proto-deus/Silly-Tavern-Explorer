from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from src.ui.widgets.async_image import (
    AsyncImageLoader,
    _PIXMAP_CACHE,
    THUMBNAIL_LOADER_OWNER,
    cache_key,
)


class CardThumbnail(QWidget):
    clicked = pyqtSignal(int)
    # char_id, ctrl_pressed, shift_pressed — drives multi-select in the grid.
    selection_requested = pyqtSignal(int, bool, bool)
    double_clicked = pyqtSignal(int)

    def __init__(
        self,
        char_id: int,
        name: str,
        thumb_path: str,
        is_favorite: bool = False,
        token_count: int = 0,
        parent: QWidget | None = None,
        loader_owner: str = THUMBNAIL_LOADER_OWNER,
    ):
        super().__init__(parent)
        self.char_id = char_id
        self._selected = False
        self._is_favorite = is_favorite
        self._thumb_path = thumb_path
        self._img_size = 152
        self._name_lines_max = 4
        self._loader_owner = loader_owner
        self._star_label: QLabel | None = None
        self.setFixedWidth(self._img_size + 8)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._image_label = QLabel()
        self._image_label.setFixedSize(self._img_size, self._img_size)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_border()
        layout.addWidget(self._image_label)

        self._token_badge = QLabel(self._image_label)
        self._token_badge.setStyleSheet(
            'background-color: rgba(0, 0, 0, 160); color: #ddd;'
            'padding: 1px 4px; border-radius: 3px;'
        )
        self._token_badge.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._token_badge.hide()
        self.set_token_count(token_count)

        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(2)
        self._name_row = name_row
        if is_favorite:
            self._add_star()
        self._name_label = QLabel(name)
        # Card names are untrusted input: never let QLabel auto-detect
        # rich text (a name like "<img ...>" would corrupt the display).
        self._name_label.setTextFormat(Qt.TextFormat.PlainText)
        self._name_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._name_label.setWordWrap(True)
        self._name_label.setStyleSheet('font-weight: bold; color: #e0e0e0;')
        name_row.addWidget(self._name_label, 1)
        layout.addLayout(name_row)

        self._apply_name_fit()
        self._request_load()

    def _apply_name_fit(self) -> None:
        """Size the name label and card so long names wrap instead of clipping.

        The card keeps a fixed width but its height grows (up to a few wrapped
        lines) when the character name needs more room.
        """
        fm = self._name_label.fontMetrics()
        line_height = fm.lineSpacing()
        name_width = self._img_size - 8
        if self._star_label is not None:
            name_width -= self._star_label.sizeHint().width() + 2
        name_width = max(20, name_width)

        rect = fm.boundingRect(
            QRect(0, 0, name_width, 10000),
            Qt.TextFlag.TextWordWrap,
            self._name_label.text() or ' ',
        )
        needed = rect.height()
        min_height = max(line_height, 40)
        max_height = line_height * self._name_lines_max
        name_height = min(max(needed, min_height), max_height)

        self._name_label.setFixedHeight(name_height)
        total = self._img_size + 12 + name_height
        self.setFixedSize(self._img_size + 8, total)

    def _apply_border(self) -> None:
        border = '#5b9bd5' if self._selected else '#3a3a3a'
        self._image_label.setStyleSheet(f'''
            QLabel {{
                background-color: #2a2a2a;
                border: 2px solid {border};
                border-radius: 6px;
            }}
        ''')

    def _request_load(self) -> None:
        """Load the thumbnail, using the cache or an async worker."""
        path = self._thumb_path
        if not path or not Path(path).exists():
            self._show_no_image()
            return
        ratio = self.devicePixelRatio() or 1.0
        target_px = max(1, int((self._img_size - 4) * ratio))
        # Capture mtime now so the completion key matches even if the file
        # is rewritten while the load is in flight (the next request will
        # see the new mtime and bypass the stale cache entry).
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            mtime = 0.0
        key = cache_key(path, target_px, mtime)
        cached = _PIXMAP_CACHE.get(key)
        if cached is not None:
            self._set_pixmap(cached)
            return
        # Dispatch an async load; show the bordered placeholder meanwhile.
        # The request's cache key travels with the load so the completion
        # handler caches under exactly this key — even if another request
        # (post-rewrite, new mtime) supersedes this one in the meantime.
        self._apply_border()
        self._image_label.setText('')
        loader = AsyncImageLoader(path, target_px, context=key, owner=self._loader_owner)
        loader.signals.loaded.connect(self._on_image_loaded)
        loader.submit()

    def _on_image_loaded(self, qimg, path: str, target_px: int, key=None) -> None:
        # Stale load guard: discard results for a different path or size.
        if path != self._thumb_path:
            return
        if qimg is None:
            self._show_no_image()
            return
        ratio = self.devicePixelRatio() or 1.0
        expected_px = max(1, int((self._img_size - 4) * ratio))
        if target_px != expected_px:
            return  # a newer load at the current size is en route
        pixmap = QPixmap.fromImage(qimg)
        pixmap.setDevicePixelRatio(ratio)
        if key is None:
            key = cache_key(path, target_px)
        _PIXMAP_CACHE.put(key, pixmap)
        self._set_pixmap(pixmap)

    def _set_pixmap(self, pixmap) -> None:
        self._image_label.setPixmap(pixmap)
        self._apply_border()

    def _show_no_image(self) -> None:
        self._image_label.setText('No Image')
        self._image_label.setStyleSheet('''
            QLabel {
                background-color: #2a2a2a;
                border: 2px solid #3a3a3a;
                border-radius: 6px;
                color: #888;
                            }
        ''')

    def set_thumb_size(self, img_size: int) -> None:
        """Resize the thumbnail widget and reload the image at the new size."""
        self._img_size = img_size
        self._image_label.setFixedSize(img_size, img_size)
        self._apply_border()
        self._request_load()
        self._reposition_badge()
        self._apply_name_fit()

    def set_token_count(self, count: int) -> None:
        """Show or hide the token-count badge on the thumbnail."""
        if count and count > 0:
            if count >= 10000:
                text = f'{count / 1000:.1f}k'
            else:
                text = f'{count:,}'
            self._token_badge.setText(text)
            self._token_badge.adjustSize()
            self._reposition_badge()
            self._token_badge.show()
        else:
            self._token_badge.hide()

    def _reposition_badge(self) -> None:
        """Place the badge at the bottom-right of the image label."""
        self._token_badge.adjustSize()
        x = self._image_label.width() - self._token_badge.width() - 2
        y = self._image_label.height() - self._token_badge.height() - 2
        self._token_badge.move(max(0, x), max(0, y))
        self._token_badge.raise_()

    def _add_star(self) -> None:
        """Insert the favorite star at the front of the name row (once)."""
        if self._star_label is not None:
            return
        self._star_label = QLabel('\u2605')
        self._star_label.setStyleSheet('color: #ffcc44; ')
        self._name_row.insertWidget(0, self._star_label)

    def _remove_star(self) -> None:
        if self._star_label is None:
            return
        self._star_label.setParent(None)
        self._star_label.deleteLater()
        self._star_label = None

    def update_from_entry(
        self,
        name: str,
        thumb_path: str,
        is_favorite: bool = False,
        token_count: int | None = None,
    ) -> None:
        """Update an existing thumbnail in-place without recreating the widget.

        ``token_count=None`` leaves the badge untouched: callers that only
        change e.g. the favorite flag must not wipe the token count.
        """
        self._is_favorite = is_favorite
        self._thumb_path = thumb_path
        self._name_label.setText(name)
        # Keep the star in sync with the new flag (adding/removing changes
        # the name-row width, so do it before re-fitting the name).
        if is_favorite:
            self._add_star()
        else:
            self._remove_star()
        if token_count is not None:
            self.set_token_count(token_count)
        self._request_load()
        self._apply_name_fit()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_border()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            mods = event.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            self.clicked.emit(self.char_id)
            self.selection_requested.emit(self.char_id, ctrl, shift)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self.char_id)
        super().mouseDoubleClickEvent(event)

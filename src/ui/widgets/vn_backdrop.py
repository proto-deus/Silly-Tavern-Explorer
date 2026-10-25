"""Visual-novel backdrop stage for the Test tab.

The stage hosts three things stacked on top of each other: a gallery backdrop
image, an optional expression sprite, and (at the bottom) the chat scroll area
the Test tab builds, separated from the image by a divider.  One widget owns
the geometry so switching between the classic full-height chat and the stacked
VN layout is a ``set_mode`` call rather than re-parenting widgets around
layouts.  The image and the chat sections are strictly disjoint: the image can
never paint over the chat text.

Images load through :class:`~src.ui.widgets.async_image.AsyncImageLoader`
because gallery files may be sealed by the vault; never open them directly.
"""
from __future__ import annotations

import random
from pathlib import Path

from src.ui.widgets.image_viewer import wrap_index

# Display modes for the stage.
MODE_CHAT = 'chat'
MODE_STACKED = 'stacked'
MODES = (MODE_CHAT, MODE_STACKED)

#: Height (px) of the divider between the image and the chat sections.
DIVIDER_H = 2
#: Top inset (px) of the chat viewport so the first text line is never flush
#: against the divider / section edge.
CHAT_TOP_INSET = 6

#: Fit modes for the backdrop image.
FIT_CONTAIN = 'contain'
FIT_COVER = 'cover'
FIT_STRETCH = 'stretch'
FIT_MODES = (FIT_CONTAIN, FIT_COVER, FIT_STRETCH)

#: Sprite alignment within the backdrop area.
SPRITE_POSITIONS = ('left', 'center', 'right')


def fit_rect(
    img_w: float, img_h: float, box_w: float, box_h: float, mode: str = FIT_CONTAIN,
) -> tuple[int, int, int, int]:
    """Return the ``(x, y, w, h)`` rect an image should occupy inside a box.

    ``contain`` letterboxes (whole image visible), ``cover`` fills the box
    (image overflows equally on both sides and is clipped), ``stretch``
    ignores the aspect ratio.  Pure so the layout math can be tested
    without Qt.
    """
    box_w = max(0.0, float(box_w))
    box_h = max(0.0, float(box_h))
    if img_w <= 0 or img_h <= 0 or box_w <= 0 or box_h <= 0:
        return (0, 0, int(box_w), int(box_h))
    if mode == FIT_STRETCH:
        return (0, 0, round(box_w), round(box_h))
    scale = (
        min(box_w / img_w, box_h / img_h)
        if mode == FIT_CONTAIN
        else max(box_w / img_w, box_h / img_h)
    )
    w = img_w * scale
    h = img_h * scale
    x = (box_w - w) / 2
    y = (box_h - h) / 2
    return (round(x), round(y), round(w), round(h))


def sprite_rect(
    box_w: float, box_h: float, img_w: float, img_h: float,
    height_pct: float = 75, position: str = 'center',
) -> tuple[int, int, int, int]:
    """Return the ``(x, y, w, h)`` rect for an expression sprite.

    The sprite keeps its aspect ratio, is scaled so its height is
    *height_pct* of the box height, and stands on the bottom edge like a
    visual-novel character.  *position* selects the horizontal anchor.
    """
    box_w = max(0.0, float(box_w))
    box_h = max(0.0, float(box_h))
    if img_w <= 0 or img_h <= 0 or box_w <= 0 or box_h <= 0:
        return (0, 0, 0, 0)
    pct = max(5.0, min(100.0, float(height_pct)))
    h = box_h * pct / 100.0
    w = img_w * (h / img_h)
    if w > box_w:
        # Very wide sprite: shrink to fit instead of overflowing sideways.
        w = box_w
        h = img_h * (w / img_w)
    if position == 'left':
        x = 0.0
    elif position == 'right':
        x = box_w - w
    else:
        x = (box_w - w) / 2
    y = box_h - h
    return (round(x), round(y), round(w), round(h))


def default_index(source: str, count: int, rng: random.Random | None = None) -> int:
    """Return the gallery index a fresh chat should open on.

    ``first`` -> 0, ``latest`` -> last entry (callers that track add-order
    pass the list already ordered that way), ``random`` -> a random pick.
    Returns ``-1`` when *count* is empty.
    """
    if count <= 0:
        return -1
    if source == 'random':
        return (rng or random).randrange(count)
    if source == 'latest':
        return count - 1
    return 0


try:
    from PyQt6.QtCore import QRect, Qt, pyqtSignal
    from PyQt6.QtGui import QColor, QFont, QPainter, QPixmap
    from PyQt6.QtWidgets import QLabel, QPushButton, QWidget
except ImportError:
    QWidget = None  # type: ignore[misc,assignment]


if QWidget is not None:

    class _BackdropLabel(QWidget):
        """Paints the backdrop pixmap (or a text placeholder) with a fit mode.

        A custom painter instead of ``QLabel.setScaledContents`` because the
        fit modes need aspect-ratio-aware placement (and clipping for
        ``cover``), which a label cannot express.
        """

        clicked = pyqtSignal()

        def __init__(self, parent: QWidget | None = None):
            super().__init__(parent)
            self._pixmap: QPixmap | None = None
            self._placeholder = ''
            self._fit_mode = FIT_CONTAIN

        def set_pixmap(self, pixmap: QPixmap | None) -> None:
            self._pixmap = pixmap
            self.update()

        def set_placeholder(self, text: str) -> None:
            self._placeholder = text or ''
            self.update()

        def set_fit_mode(self, mode: str) -> None:
            self._fit_mode = mode if mode in FIT_MODES else FIT_CONTAIN
            self.update()

        def mousePressEvent(self, event) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit()
            super().mousePressEvent(event)

        def paintEvent(self, event) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            if self._pixmap is not None and not self._pixmap.isNull():
                x, y, w, h = fit_rect(
                    self._pixmap.width(), self._pixmap.height(),
                    self.width(), self.height(), self._fit_mode,
                )
                painter.drawPixmap(QRect(x, y, w, h), self._pixmap)
            elif self._placeholder:
                painter.setPen(QColor(128, 128, 128, 200))
                font = QFont(self.font())
                font.setPixelSize(max(14, self.height() // 10))
                painter.setFont(font)
                painter.drawText(
                    self.rect(), int(Qt.AlignmentFlag.AlignCenter), self._placeholder,
                )
            painter.end()

    class _SpriteLabel(QWidget):
        """Expression sprite: aspect-preserving, bottom-aligned by its rect."""

        def __init__(self, parent: QWidget | None = None):
            super().__init__(parent)
            self._pixmap: QPixmap | None = None
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        def set_pixmap(self, pixmap: QPixmap | None) -> None:
            self._pixmap = pixmap
            self.setVisible(pixmap is not None and not pixmap.isNull())
            self.update()

        def pixmap(self) -> QPixmap | None:
            return self._pixmap

        def paintEvent(self, event) -> None:
            if self._pixmap is None or self._pixmap.isNull():
                return
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            x, y, w, h = fit_rect(
                self._pixmap.width(), self._pixmap.height(),
                self.width(), self.height(), FIT_CONTAIN,
            )
            painter.drawPixmap(QRect(x, y, w, h), self._pixmap)
            painter.end()

    class VNBackdrop(QWidget):
        """Backdrop + sprite + nav chrome, with the chat area embedded at the
        bottom (or filling everything, in the classic chat mode).

        The caller keeps ownership of the chat widget but places it with
        :meth:`set_chat_widget`; from then on the stage positions it
        together with the image so the two never fight over geometry.  In the
        stacked VN layout a divider sits between the image and the chat so
        the two sections read as separate panes.
        """

        #: The backdrop was clicked (open the lightbox).
        image_clicked = pyqtSignal()
        #: The visible image changed (nav buttons or ``show_index``).
        index_changed = pyqtSignal(int)

        def __init__(self, parent: QWidget | None = None):
            super().__init__(parent)
            self._mode = MODE_CHAT
            self._chat_pct = 30
            self._chat_opacity = 60
            self._fit_mode = FIT_CONTAIN
            self._show_caption = True
            self._sprite_position = 'center'
            self._sprite_height_pct = 75
            self._paths: list[str] = []
            self._labels: list[str] = []
            self._index = -1
            self._generation = 0
            self._sprite_generation = 0
            self._chat_widget: QWidget | None = None
            # Small path->pixmap cache: navigating the gallery back and forth
            # should not re-decode the file every time.  Keyed like
            # async_image.cache_key so a rewritten file misses the cache.
            self._img_cache: dict[str, QPixmap] = {}
            self._sprite_cache: dict[str, QPixmap] = {}

            self._image_label = _BackdropLabel(self)
            self._sprite_label = _SpriteLabel(self)
            self._sprite_label.setVisible(False)
            self._caption = QLabel('', self)
            self._caption.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            )
            self._caption.setStyleSheet(
                'background-color: rgba(0, 0, 0, 120); color: #e0e0e0;'
                'padding: 3px 8px; border-radius: 4px;'
            )
            self._prev_btn = self._nav_button('\u25c0', self._prev_clicked)
            self._next_btn = self._nav_button('\u25b6', self._next_clicked)
            # The divider marks the boundary between the image section and
            # the chat section in the stacked VN layout.
            self._divider = QWidget(self)
            self._divider.setObjectName('gridSeparator')
            self._divider.setFixedHeight(DIVIDER_H)
            self._divider.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._divider.setVisible(False)
            self._image_label.clicked.connect(self.image_clicked.emit)
            self._apply_chrome_font()

        # ---- setup ----

        def _nav_button(self, text: str, slot) -> QPushButton:
            # Must be parented to the stage: an unparented button becomes a
            # top-level window of its own (a tiny floating window that
            # _relayout would move/raise in screen coordinates) instead of
            # chrome sitting on the image.
            btn = QPushButton(text, self)
            btn.setFixedSize(28, 48)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setStyleSheet(
                'QPushButton { background-color: rgba(0, 0, 0, 110);'
                ' color: #e0e0e0; border: 1px solid rgba(255, 255, 255, 40);'
                ' border-radius: 4px; font-size: 12px; }'
                'QPushButton:hover { background-color: rgba(0, 0, 0, 170); }'
            )
            btn.clicked.connect(slot)
            return btn

        def set_chat_widget(self, widget: QWidget | None) -> None:
            """Embed the chat area and start positioning it with the stage."""
            if widget is self._chat_widget:
                return
            if self._chat_widget is not None:
                self._chat_widget.setParent(None)
            self._chat_widget = widget
            if widget is not None:
                widget.setParent(self)
                widget.show()
                widget.raise_()
            self._apply_chat_style()
            self._relayout()

        # ---- configuration ----

        def set_mode(self, mode: str, chat_pct: int | None = None) -> None:
            self._mode = mode if mode in MODES else MODE_CHAT
            if chat_pct is not None:
                self._chat_pct = max(15, min(50, int(chat_pct)))
            self._image_label.setVisible(self._mode != MODE_CHAT)
            self._divider.setVisible(self._mode == MODE_STACKED)
            self._apply_chat_style()
            self._update_chrome()
            self._relayout()

        def set_chat_pct(self, chat_pct: int) -> None:
            self._chat_pct = max(15, min(50, int(chat_pct)))
            self._relayout()

        def set_chat_opacity(self, opacity_pct: int) -> None:
            self._chat_opacity = max(20, min(100, int(opacity_pct)))
            self._apply_chat_style()

        def set_fit_mode(self, mode: str) -> None:
            self._fit_mode = mode if mode in FIT_MODES else FIT_CONTAIN
            self._image_label.set_fit_mode(self._fit_mode)

        def set_show_caption(self, show: bool) -> None:
            self._show_caption = bool(show)
            self._relayout()

        def set_sprite_style(self, position: str, height_pct: int) -> None:
            self._sprite_position = (
                position if position in SPRITE_POSITIONS else 'center'
            )
            self._sprite_height_pct = max(40, min(100, int(height_pct)))
            self._relayout()

        def set_placeholder(self, text: str) -> None:
            """Text drawn in the empty backdrop area (e.g. the card name)."""
            self._image_label.set_placeholder(text)

        def refresh_font_size(self) -> None:
            self._apply_chrome_font()
            self._relayout()

        def _apply_chrome_font(self) -> None:
            from src.ui.styles import ui_font_px
            font = QFont(self.font())
            font.setPixelSize(ui_font_px(-3))
            self._caption.setFont(font)

        def _apply_chat_style(self) -> None:
            """Style the embedded chat for the active mode."""
            if self._chat_widget is None:
                return
            if self._mode == MODE_CHAT:
                self._chat_widget.setStyleSheet('')
                self._set_chat_inset(0)
            else:
                # Stacked: the panel sits below the image behind a divider,
                # so a light scrim keeps it distinct without hiding the theme
                # background.
                alpha = round(255 * self._chat_opacity / 100)
                self._chat_widget.setStyleSheet(
                    f'QScrollArea {{ background-color: rgba(30, 30, 30, {alpha});'
                    ' border: none; }'
                )
                self._set_chat_inset(CHAT_TOP_INSET)

        def _set_chat_inset(self, pixels: int) -> None:
            """Reserve space at the top of the chat viewport.

            Without this the first text line sits flush under the divider and
            looks clipped against the image section.  Scroll areas expose
            viewport margins; any other widget simply keeps its own layout.
            """
            set_margins = getattr(self._chat_widget, 'setViewportMargins', None)
            if callable(set_margins):
                set_margins(0, pixels, 0, 0)

        # ---- gallery images ----

        def set_images(self, paths: list[str], labels: list[str] | None = None,
                       index: int | None = 0) -> None:
            """Replace the gallery image list and show *index* within it."""
            self._paths = [str(p) for p in paths or []]
            self._labels = [str(x) for x in (labels or [])]
            if index is None:
                self.show_index(self._index)
            else:
                self.show_index(index)

        def paths(self) -> list[str]:
            return list(self._paths)

        def labels(self) -> list[str]:
            return list(self._labels)

        def current_index(self) -> int:
            return self._index

        def current_path(self) -> str:
            if 0 <= self._index < len(self._paths):
                return self._paths[self._index]
            return ''

        def show_index(self, index: int) -> None:
            if not self._paths:
                self._index = -1
                self._image_label.set_pixmap(None)
                self._update_chrome()
                self._relayout()
                return
            self._index = max(0, min(int(index), len(self._paths) - 1))
            self._load_current()
            self._update_chrome()
            self.index_changed.emit(self._index)

        def next_image(self) -> None:
            self.show_index(wrap_index(self._index, 1, len(self._paths)))

        def previous_image(self) -> None:
            self.show_index(wrap_index(self._index, -1, len(self._paths)))

        def _prev_clicked(self) -> None:
            self.previous_image()

        def _next_clicked(self) -> None:
            self.next_image()

        def _load_current(self) -> None:
            path = self.current_path()
            self._generation += 1
            if not path:
                self._image_label.set_pixmap(None)
                return
            self._request_image(
                path, self._generation, self._img_cache, self._image_label.set_pixmap,
            )

        def set_sprite(self, path: str | None) -> None:
            """Show an expression sprite (``None`` hides the current one)."""
            self._sprite_generation += 1
            if not path:
                self._sprite_label.set_pixmap(None)
                return
            self._request_image(
                str(path), self._sprite_generation, self._sprite_cache,
                self._sprite_label.set_pixmap,
            )

        def _request_image(self, path: str, generation: int,
                           cache: dict[str, QPixmap], apply) -> None:
            """Load *path* off-thread and apply it unless superseded.

            Stale results are dropped by generation counter: switching
            characters or images quickly queues several loads and only the
            newest may win.
            """
            from src.ui.widgets.async_image import AsyncImageLoader, cache_key
            target_px = self._target_px()
            key = cache_key(path, target_px)
            cached = cache.get(key)
            if cached is not None:
                apply(cached)
                return

            def on_loaded(qimage, _path, _px, context) -> None:
                if context != generation:
                    return
                if not self._widget_alive():
                    return
                pixmap = QPixmap.fromImage(qimage) if qimage is not None else None
                if pixmap is None or pixmap.isNull():
                    apply(None)
                    return
                cache[key] = pixmap
                while len(cache) > 12:
                    cache.pop(next(iter(cache)), None)
                apply(pixmap)

            loader = AsyncImageLoader(path, target_px, context=generation,
                                      owner='vn_backdrop')
            loader.signals.loaded.connect(on_loaded)
            loader.submit()

        def _target_px(self) -> int:
            return max(640, min(1600, max(self.width(), self.height(), 1)))

        def _widget_alive(self) -> bool:
            try:
                _ = self.isVisible()
                return True
            except RuntimeError:
                return False

        # ---- chrome + geometry ----

        def _update_chrome(self) -> None:
            count = len(self._paths)
            self._prev_btn.setVisible(self._mode != MODE_CHAT and count > 1)
            self._next_btn.setVisible(self._mode != MODE_CHAT and count > 1)
            show_caption = self._mode != MODE_CHAT and self._show_caption and count > 0
            self._caption.setVisible(show_caption)
            if show_caption and 0 <= self._index < count:
                label = (
                    self._labels[self._index]
                    if self._index < len(self._labels) and self._labels[self._index]
                    else Path(self._paths[self._index]).stem
                )
                self._caption.setText(f'{label} \u2014 {self._index + 1}/{count}')
            elif show_caption:
                self._caption.setText('')
            self._caption.adjustSize()
            # The sprite belongs to the VN view only; the pixmap decides
            # within it (set_pixmap toggles visibility).
            sprite = self._sprite_label.pixmap()
            if self._mode == MODE_CHAT:
                self._sprite_label.setVisible(False)
            elif sprite is not None and not sprite.isNull():
                self._sprite_label.setVisible(True)

        def _chat_height(self) -> int:
            h = self.height()
            want = round(h * self._chat_pct / 100)
            return max(60, min(want, max(60, h - 40)))

        def _image_rect(self) -> QRect:
            w, h = self.width(), self.height()
            if self._mode == MODE_STACKED:
                # Strictly disjoint sections: the image stops above the
                # divider, so it can never shave pixels off the chat text.
                return QRect(0, 0, w, max(0, h - self._chat_height() - DIVIDER_H))
            return QRect(0, 0, w, h)

        def _relayout(self) -> None:
            if not self._widget_alive():
                return
            w, h = self.width(), self.height()
            image_rect = self._image_rect()
            self._image_label.setGeometry(image_rect)
            self._image_label.raise_()

            # Sprite stands on the bottom edge of the *image* area so it is
            # not half-hidden behind the chat panel.
            sx, sy, sw, sh = sprite_rect(
                image_rect.width(), image_rect.height(),
                self._sprite_width_hint(), self._sprite_height_hint(),
                self._sprite_height_pct, self._sprite_position,
            )
            self._sprite_label.setGeometry(
                image_rect.x() + sx, image_rect.y() + sy, sw, sh,
            )
            self._sprite_label.raise_()

            btn_y = image_rect.y() + max(8, (image_rect.height() - self._prev_btn.height()) // 2)
            self._prev_btn.move(image_rect.x() + 8, btn_y)
            self._next_btn.move(
                image_rect.x() + image_rect.width() - self._next_btn.width() - 8, btn_y,
            )
            self._prev_btn.raise_()
            self._next_btn.raise_()

            self._caption.move(image_rect.x() + 10, image_rect.y() + 8)
            self._caption.raise_()

            if self._mode == MODE_STACKED:
                self._divider.setGeometry(
                    0, image_rect.y() + image_rect.height(), w, DIVIDER_H,
                )
                self._divider.raise_()

            if self._chat_widget is not None:
                chat_h = h if self._mode == MODE_CHAT else self._chat_height()
                self._chat_widget.setGeometry(0, h - chat_h, w, chat_h)
                self._chat_widget.raise_()

        def _sprite_width_hint(self) -> int:
            # The load is async; before it lands the rect uses the label's
            # current pixmap (or a portrait-ish default) so the slot holds
            # its shape for the incoming image.
            pixmap = self._sprite_label.pixmap()
            if pixmap is not None:
                return pixmap.width()
            return max(1, self._image_rect().width() // 3)

        def _sprite_height_hint(self) -> int:
            pixmap = self._sprite_label.pixmap()
            if pixmap is not None:
                return pixmap.height()
            return max(1, self._image_rect().height())

        def resizeEvent(self, event) -> None:
            super().resizeEvent(event)
            self._relayout()

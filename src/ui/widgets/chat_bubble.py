from __future__ import annotations

import base64
import html
from datetime import datetime
from urllib.parse import unquote, urlparse

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.chat_formatting import extract_image_urls, format_chat_message
from src.ui.styles import ui_font_px
from src.ui.widgets.chat_image_loader import (
    cached_url_pixmap,
    make_url_image_label,
)

_USER_COLOR = '#6cb6ff'
_ASSISTANT_COLOR = '#e0a060'


def _plain_html(text: str) -> str:
    """Escape *text* for safe plain display (newlines become <br>)."""
    return html.escape(text or '').replace('\n', '<br>')


def _image_name_from_url(url: str) -> str:
    """A sensible file name for an image URL (``image.png`` as a fallback)."""
    name = unquote(urlparse(url).path.rsplit('/', 1)[-1] or '')
    return name if '.' in name else (name or 'image') + '.png'


class MessageBubble(QFrame):
    """A single chat message rendered as a bubble.

    Holds a name header, optional attachments (image thumbnails / text chips),
    and rich-text content.  Supports appending streamed chunks, then finalizing
    with full colored formatting.  Emits action signals keyed by message index.
    """

    edit_requested = pyqtSignal(int)
    delete_requested = pyqtSignal(int)
    copy_requested = pyqtSignal(int)
    regenerate_requested = pyqtSignal(int)
    variant_back_requested = pyqtSignal(int)
    variant_forward_requested = pyqtSignal(int)
    # A chat image was right-clicked: (image bytes or None when not yet
    # fetched, suggested file name, global cursor position, loader callable
    # or None).  The Test tab shows the menu; it owns the gallery/DB access.
    image_context_requested = pyqtSignal(object, str, QPoint, object)

    def __init__(
        self,
        index: int,
        role: str,
        name: str,
        text: str,
        attachments: list[dict] | None = None,
        show_timestamp: bool = False,
        dialogue_color: str = '#9ad8ff',
        action_color: str = '#e0a060',
        emphasis_color: str = '#9be8a0',
        font_size: int = 13,
        show_regenerate: bool = False,
        show_variant_back: bool = False,
        show_variant_forward: bool = False,
        variant_forward_new: bool = False,
        timestamp: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._index = index
        self._role = role
        self._name = name
        self._text = text or ''
        # The plain (escaped) HTML of the text rendered so far.  A streamed
        # chunk only has to escape its own fragment and append it here, so
        # the accumulated reply is never re-escaped from scratch.
        self._stream_html = _plain_html(self._text)
        self._show_timestamp = show_timestamp
        self._timestamp = timestamp
        self._dialogue_color = dialogue_color
        self._action_color = action_color
        self._emphasis_color = emphasis_color
        self._font_size = font_size
        self._small_px = ui_font_px(-3)

        self.setObjectName('chat_bubble')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)

        header = QHBoxLayout()
        header.setSpacing(6)
        name_color = _USER_COLOR if role == 'user' else _ASSISTANT_COLOR
        self._name_label = QLabel()
        self._name_label.setTextFormat(Qt.TextFormat.RichText)
        self._set_name_html(name, name_color)
        header.addWidget(self._name_label)
        header.addStretch()
        layout.addLayout(header)

        att_widget = self._build_attachments(attachments or [])
        if att_widget is not None:
            layout.addWidget(att_widget)

        self._content_label = QLabel()
        self._content_label.setTextFormat(Qt.TextFormat.RichText)
        self._content_label.setWordWrap(True)
        self._content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._content_label.setOpenExternalLinks(False)
        layout.addWidget(self._content_label)

        self._images_container = QWidget()
        self._images_layout = QHBoxLayout(self._images_container)
        self._images_layout.setContentsMargins(0, 0, 0, 0)
        self._images_layout.setSpacing(4)
        self._images_container.setVisible(False)
        layout.addWidget(self._images_container)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self._buttons: list[QPushButton] = []
        # Regenerate/variant controls only; they ride the newest assistant
        # message and are retired in place when a newer reply arrives.
        self._action_buttons: list[QPushButton] = []
        footer.addStretch()

        # Variant navigation back-arrow sits directly left of Edit.
        if show_variant_back:
            back = self._make_button('◀')
            back.setToolTip('Show the previous response')
            back.clicked.connect(lambda: self.variant_back_requested.emit(self._index))
            footer.addWidget(back)
            self._buttons.append(back)
            self._action_buttons.append(back)

        for label, signal in (
            ('Edit', self.edit_requested),
            ('Copy', self.copy_requested),
            ('Delete', self.delete_requested),
        ):
            btn = self._make_button(label)
            btn.clicked.connect(lambda _=False, s=signal: s.emit(self._index))
            footer.addWidget(btn)
            self._buttons.append(btn)

        if role == 'assistant' and show_regenerate:
            regen = self._make_button('Regenerate')
            regen.setToolTip('Replace all responses with a new generation')
            regen.clicked.connect(lambda: self.regenerate_requested.emit(self._index))
            footer.addWidget(regen)
            self._buttons.append(regen)
            self._action_buttons.append(regen)

        # Right-most slot is always ▶: steps toward newer responses, or
        # labelled (New) when pressing it past the newest would generate
        # a fresh alternate.
        if role == 'assistant' and show_variant_forward:
            if variant_forward_new:
                fwd = self._make_button('(New) ▶')
                fwd.setToolTip('Generate a new response')
            else:
                fwd = self._make_button('▶')
                fwd.setToolTip('Show the next response')
            fwd.clicked.connect(lambda: self.variant_forward_requested.emit(self._index))
            footer.addWidget(fwd)
            self._buttons.append(fwd)
            self._action_buttons.append(fwd)

        layout.addLayout(footer)

        self._render_content()

    def _make_button(self, label: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setFlat(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f'QPushButton {{ color: #888; font-size: {self._small_px}px; padding: 1px 4px; }}'
            'QPushButton:hover { color: #e0e0e0; }'
        )
        return btn

    def _set_name_html(self, name: str, color: str) -> None:
        ts = ''
        if self._show_timestamp:
            stamp = self._format_timestamp()
            if stamp:
                ts = (
                    f'<span style="color:#777; font-size:{self._small_px}px;">'
                    f'[{stamp}]</span> '
                )
        self._name_label.setText(
            f'{ts}<b><span style="color:{color}">{html.escape(name)}</span></b>'
        )

    def _format_timestamp(self) -> str:
        """Format the message's creation-time stamp (empty when unknown).

        Render-time stamping (``datetime.now()``) rewrote every visible
        timestamp on each re-render - edit, delete, variant navigation -
        so the times never matched the messages.
        """
        raw = self._timestamp or ''
        if not raw:
            return ''
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return raw[:16]
        if dt.date() == datetime.now().date():
            return dt.strftime('%H:%M')
        return dt.strftime('%Y-%m-%d %H:%M')

    def _build_attachments(self, attachments: list[dict]) -> QWidget | None:
        if not attachments:
            return None
        container = QWidget()
        h = QHBoxLayout(container)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        for att in attachments:
            if att.get('kind') == 'image':
                w = self._image_thumb(att)
            else:
                w = self._text_chip(att)
            if w is not None:
                h.addWidget(w)
        h.addStretch()
        return container

    def _image_thumb(self, att: dict) -> QWidget | None:
        b64 = att.get('data', '')
        if not b64:
            return None
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return None
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):
            return None
        if pixmap.width() > 320 or pixmap.height() > 320:
            pixmap = pixmap.scaled(
                320, 320,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        label = QLabel()
        label.setPixmap(pixmap)
        label.setToolTip(att.get('name', 'image'))
        name = att.get('name') or 'image.png'
        label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        label.customContextMenuRequested.connect(
            lambda pos, data=raw, name=name, lbl=label: self.image_context_requested.emit(
                data, name, lbl.mapToGlobal(pos), None,
            )
        )
        return label

    def _text_chip(self, att: dict) -> QWidget:
        name = html.escape(att.get('name', 'file'))
        chip = QLabel(f'<span style="color:#9be8a0;">📄 {name}</span>')
        chip.setTextFormat(Qt.TextFormat.RichText)
        chip.setToolTip(att.get('name', 'file'))
        return chip

    def _render_content(self) -> None:
        self._content_label.setText(
            format_chat_message(
                self._text,
                dialogue_color=self._dialogue_color,
                action_color=self._action_color,
                emphasis_color=self._emphasis_color,
            )
        )
        self._render_url_images()

    def _render_url_images(self) -> None:
        while self._images_layout.count():
            item = self._images_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        urls = extract_image_urls(self._text)
        for url in urls:
            label = make_url_image_label(url)
            # Serve from cache when the same URL was already fetched this
            # session (re-renders must not re-download every image).
            cached = cached_url_pixmap(url)
            if cached is not None:
                label._loaded = True
                label.setPixmap(cached)
            else:
                label.setText('🖼 Click to load image')
            label.setStyleSheet(f'color: #777; font-size: {self._small_px}px;')
            label.setMinimumHeight(24)
            label.context_menu_requested.connect(
                lambda u, data, pos, lbl=label: self._on_url_image_menu(lbl, u, data, pos)
            )
            self._images_layout.addWidget(label)
        if urls:
            self._images_layout.addStretch()
        self._images_container.setVisible(bool(urls))

    def _on_url_image_menu(self, label, url: str, data, global_pos) -> None:
        """Forward a URL image's right-click to the Test tab.

        The label carries its own fetch routine, so an unfetched image
        passes that along as the loader and the menu can offer "Load Image".
        """
        loader = getattr(label, 'request_load', None)
        self.image_context_requested.emit(
            data, _image_name_from_url(url), global_pos, loader,
        )

    def append_stream(self, text: str) -> None:
        """Append a streamed chunk (plain rendering; no colored formatting).

        Only the *new* fragment is escaped and appended to the HTML that is
        already rendered.  Re-escaping (and re-parsing) the whole
        accumulated reply on every SSE chunk was O(n^2) on the GUI thread:
        a 16k-token answer meant ~800 full re-renders of a growing 64 KB
        document - the classic "typing lag".

        The result is identical to a full re-render: escaping is a
        per-character mapping, so ``_plain_html(a + b)`` equals
        ``_plain_html(a) + _plain_html(b)``.
        """
        self._text += text
        self._stream_html += _plain_html(text)
        self._content_label.setText(self._stream_html)

    def set_controls_enabled(self, enabled: bool) -> None:
        """Enable/disable the footer action buttons (edit/copy/delete/regen)."""
        for btn in self._buttons:
            btn.setEnabled(enabled)

    def clear_actions(self) -> None:
        """Hide the regenerate/variant controls in place.

        They belong to the newest assistant message only; when a newer reply
        lands, the previous holder drops them without a full rebuild.
        """
        for btn in self._action_buttons:
            btn.hide()

    def set_content(self, text: str) -> None:
        """Replace the message text and re-render with colored formatting."""
        self._text = text or ''
        # Keep the streamed-HTML buffer in step with the new text so a
        # later append_stream() continues from it.
        self._stream_html = _plain_html(self._text)
        self._render_content()

    @property
    def text(self) -> str:
        return self._text

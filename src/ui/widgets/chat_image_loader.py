from __future__ import annotations

import weakref
from functools import partial

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QMouseEvent
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PyQt6.QtWidgets import QLabel

# Never download more than this per image (hostile URLs must not be able
# to exhaust memory).
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
# Abort stalled transfers after this many milliseconds.
_TRANSFER_TIMEOUT_MS = 15000
# Cap concurrent fetches per manager burst is Qt's job; cap the cache so
# re-rendering history doesn't re-download everything every turn.
_CACHE_LIMIT = 64

_manager: QNetworkAccessManager | None = None
_pixmap_cache: dict[str, QPixmap] = {}


def _network_manager() -> QNetworkAccessManager:
    global _manager
    if _manager is None:
        _manager = QNetworkAccessManager()
    return _manager


def cached_url_pixmap(url: str) -> QPixmap | None:
    """Return a cached pixmap for *url*, or None (does not fetch)."""
    return _pixmap_cache.get(url)


def _cache_pixmap(url: str, pixmap: QPixmap) -> None:
    _pixmap_cache[url] = pixmap
    while len(_pixmap_cache) > _CACHE_LIMIT:
        _pixmap_cache.pop(next(iter(_pixmap_cache)))


class ClickableImageLabel(QLabel):
    """QLabel that opens its tooltip URL in the default browser when clicked."""

    clicked = pyqtSignal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._loaded = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(url)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._loaded:
            # Only open the browser once the image was actually fetched —
            # a stray click on the placeholder must not launch anything.
            from PyQt6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(self._url))
            self.clicked.emit(self._url)
        super().mousePressEvent(event)


class _UrlImageLabel(ClickableImageLabel):
    """Placeholder that fetches its image on click (lazy, user-initiated)."""

    def __init__(self, url: str, max_dim: int = 512, parent=None):
        super().__init__(url, parent)
        self._max_dim = max_dim
        self._fetching = False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self._loaded and not self._fetching:
            self._fetching = True
            self.setText('Loading image…')
            load_url_image(self._url, self, self._max_dim)
            # Don't call super: the first click loads, it must not open the browser.
            return
        super().mousePressEvent(event)


def load_url_image(url: str, label: QLabel, max_dim: int = 512) -> None:
    """Fetch *url* asynchronously and display the downscaled image in *label*.

    *label* is held via a weak reference so it is safe to delete the label
    before the response arrives.  When *url* cannot be fetched or is not a
    valid image the label is simply left unchanged (it should already display a
    placeholder before this call).
    """
    from PyQt6.QtNetwork import QNetworkReply
    ref = weakref.ref(label)

    def _on_finished(reply: QNetworkReply, dim: int) -> None:
        reply.deleteLater()
        lbl = ref()
        if lbl is None:
            return
        if reply.error() != QNetworkReply.NetworkError.NoError:
            lbl.setText('Image unavailable')
            return
        data = bytes(reply.readAll())
        image = QImage.fromData(data)
        if image.isNull():
            lbl.setText('Image unavailable')
            return
        if image.width() > dim or image.height() > dim:
            image = image.scaled(
                dim, dim,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        pixmap = QPixmap.fromImage(image)
        _cache_pixmap(url, pixmap)
        if isinstance(lbl, ClickableImageLabel):
            lbl._loaded = True
        lbl.setPixmap(pixmap)

    request = QNetworkRequest(QUrl(url))
    request.setAttribute(
        QNetworkRequest.Attribute.RedirectPolicyAttribute,
        QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
    )
    request.setTransferTimeout(_TRANSFER_TIMEOUT_MS)
    reply = _network_manager().get(request)
    reply.finished.connect(partial(_on_finished, reply, max_dim))


def make_url_image_label(url: str, max_dim: int = 512, parent=None) -> _UrlImageLabel:
    """Build a lazy placeholder label for *url* (click to fetch)."""
    return _UrlImageLabel(url, max_dim, parent)

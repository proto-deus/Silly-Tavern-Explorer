from __future__ import annotations

import os


def cache_key(path: str, target_px: int, mtime: float | None = None) -> str:
    """Build a cache key for a thumbnail at a given pixel size.

    Includes the file's mtime so that rewriting a thumbnail *in place*
    (image replacement, sync pulls) invalidates the cached pixmap instead
    of serving the previous image forever.  Pass ``mtime`` explicitly to
    guarantee the request and completion keys match.  Pure function so
    cache-key construction can be unit-tested without Qt.
    """
    if mtime is None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
    return f"{path}|{target_px}|{mtime}"


class LruCache:
    """Simple least-recently-used cache with a max entry count.

    Pure-Python so the eviction logic can be unit-tested without Qt.  Not
    thread-safe; intended for UI-thread access only (where the cached pixmap
    objects live).  ``dict`` preserves insertion order, so the front of the
    dict is the least-recently-used entry and is evicted first.
    """

    def __init__(self, max_entries: int = 256):
        if max_entries < 1:
            raise ValueError('max_entries must be >= 1')
        self._max = max_entries
        self._data: dict[str, object] = {}

    def get(self, key: str):
        """Return the cached value (moving it to most-recent) or ``None``."""
        if key not in self._data:
            return None
        value = self._data.pop(key)
        self._data[key] = value
        return value

    def put(self, key: str, value) -> None:
        """Insert/update *key*, evicting the LRU entry when over capacity."""
        if key in self._data:
            self._data.pop(key)
        self._data[key] = value
        while len(self._data) > self._max:
            self._data.pop(next(iter(self._data)))

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def clear(self) -> None:
        self._data.clear()


# Module-level pixmap cache shared by all CardThumbnail instances.  Accessed
# only on the UI thread (QPixmap is GUI-thread-affined).
_PIXMAP_CACHE = LruCache(256)


try:
    from PyQt6.QtCore import QObject, QRunnable, pyqtSignal
    from PyQt6.QtGui import QImage
except ImportError:
    QRunnable = None  # type: ignore[misc,assignment]


if QRunnable is not None:

    class _LoaderSignals(QObject):
        """Signals bridge for AsyncImageLoader (QRunnable is not a QObject)."""
        # QImage | None, source path, target pixel size, caller context tag.
        loaded = pyqtSignal(object, str, int, object)

    class AsyncImageLoader(QRunnable):
        """Load and down-scale an image off the UI thread.

        Reads the file with Pillow and produces a ``QImage`` (safe to construct
        on a worker thread, unlike ``QPixmap``).  The result is emitted to the
        UI thread, where it is converted to a QPixmap and cached.  A failed
        load emits ``None``.  The optional *context* object is echoed back
        through the signal so the receiver can correlate the result with the
        exact request that produced it (e.g. a cache key captured before the
        load started).
        """

        def __init__(self, path: str, target_px: int, context: object = None):
            super().__init__()
            self._path = path
            self._target_px = target_px
            self._context = context
            self.signals = _LoaderSignals()

        def run(self) -> None:
            qimg = None
            try:
                from PIL import Image
                img = Image.open(self._path).convert('RGBA')
                img.thumbnail((self._target_px, self._target_px))
                data = img.tobytes('raw', 'RGBA')
                stride = img.width * 4
                # .copy() detaches the QImage from the Python bytes buffer so
                # the image data outlives ``data`` (which may be garbage
                # collected once run() returns).
                qimg = QImage(
                    data, img.width, img.height, stride,
                    QImage.Format.Format_RGBA8888,
                ).copy()
            except Exception:
                qimg = None
            self.signals.loaded.emit(qimg, self._path, self._target_px, self._context)
            # The signals QObject lives on the UI thread; schedule its cleanup
            # there once the queued signal has been delivered.
            self.signals.deleteLater()

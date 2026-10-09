from __future__ import annotations

import os
import threading


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

# Default owner tag for thumbnail loads; callers that rebuild their own grid
# pass a distinct tag so cancelling one grid's queue leaves the others alone.
THUMBNAIL_LOADER_OWNER = 'thumbnails'


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

        def __init__(self, path: str, target_px: int, context: object = None,
                     owner: str = THUMBNAIL_LOADER_OWNER):
            super().__init__()
            self._path = path
            self._target_px = target_px
            self._context = context
            # Identifies the widget family that requested the load, so a rebuild
            # can cancel exactly its own queued work.
            self.owner = owner
            self._finished = False
            self.signals = _LoaderSignals()

        def run(self) -> None:
            qimg = None
            try:
                from src import vault
                img = vault.open_image(self._path).convert('RGBA')
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
            # Mark done and drop out of the registry: tryTake only works while
            # a runnable is still queued, and a finished one can never be
            # cancelled, so keeping it would just retain the object forever.
            self._finished = True
            self._unregister()

        def _unregister(self) -> None:
            with _PENDING_LOCK:
                pending = _PENDING_LOADERS.get(self.owner)
                if pending and self in pending:
                    pending.remove(self)

        def submit(self) -> None:
            """Register with the shared pool and start the load.

            Registration is what lets :func:`cancel_pending_image_loads` find
            this runnable again: ``QThreadPool`` exposes no way to enumerate
            its own queue in PyQt6.
            """
            with _PENDING_LOCK:
                _PENDING_LOADERS.setdefault(self.owner, []).append(self)
                _prune_pending(self.owner)
            from PyQt6.QtCore import QThreadPool
            QThreadPool.globalInstance().start(self)


# Live loaders per owner tag. Entries are removed once they have run.
_PENDING_LOADERS: dict[str, list] = {}
# run() completes on a worker thread while submit()/cancel() run on the UI
# thread, so the registry is shared mutable state.
_PENDING_LOCK = threading.Lock()


def _prune_pending(owner: str) -> None:
    """Drop finished loaders from *owner*'s registry.

    Uses the runnable's own ``_finished`` flag rather than probing Qt: the
    ``QRunnable`` API exposes ``autoDelete()`` (not ``isAutoDelete()``), and a
    registry that only holds Python references keeps the wrappers alive
    regardless, so the flag is both simpler and exact.
    """
    pending = _PENDING_LOADERS.get(owner)
    if not pending:
        return
    _PENDING_LOADERS[owner] = [loader for loader in pending
                               if not loader._finished]


def cancel_pending_image_loads(owner: str) -> int:
    """Drop queued-but-unstarted image loads belonging to *owner*.

    ``QThreadPool.start`` enqueues without bound and nothing ever drains the
    queue, so a grid rebuild (import, sync, filter change) queued a full set of
    PNG decodes whose results were discarded along with the widgets they
    belonged to. Only *queued* runnables are removed - ``tryTake`` returns False
    for one already executing - and only those tagged with *owner*, so
    unrelated loads (an open image viewer, say) are untouched.

    Returns the number cancelled.
    """
    from PyQt6.QtCore import QThreadPool
    pool = QThreadPool.globalInstance()
    cancelled = 0
    with _PENDING_LOCK:
        # Prune inside the lock: the registry is shared mutable state and
        # _prune_pending rewrites it in place.
        _prune_pending(owner)
        remaining = []
        for loader in _PENDING_LOADERS.get(owner, []):
            try:
                if pool.tryTake(loader):
                    # Discarded before it ever ran: mark it finished so it is
                    # not left in the never-executed state (callers check
                    # ``_finished`` to decide whether a loader terminated).
                    loader._finished = True
                    cancelled += 1
                    continue
            except RuntimeError:
                continue
            remaining.append(loader)
        _PENDING_LOADERS[owner] = remaining
    return cancelled

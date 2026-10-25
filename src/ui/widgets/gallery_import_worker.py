"""Background batch import of gallery / backdrop images.

Pure summary accumulation (no Qt) sits alongside a thin ``QThread`` runner so
the result bookkeeping can be unit-tested without an event loop — the same
split used by :mod:`src.ui.widgets.import_worker`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from src import gallery_store, vault

logger = logging.getLogger(__name__)


@dataclass
class GalleryImportSummary:
    """Accumulates the results of a batch gallery import."""

    imported: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def add_imported(self) -> None:
        self.imported += 1

    def add_skipped(self, path: str) -> None:
        self.skipped += 1

    def add_error(self, path: str, error: str) -> None:
        self.errors.append(f'{Path(path).name}: {error}')

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def message(self) -> str:
        """Short status-bar summary string."""
        msg = f'Added {self.imported} image(s).'
        if self.skipped:
            msg += f' Skipped {self.skipped} duplicate(s).'
        if self.errors:
            msg += f' {len(self.errors)} error(s).'
        return msg


def import_gallery_paths(
    char_id: int | None,
    paths: list[str],
    db,
    summary: GalleryImportSummary | None = None,
    progress_cb=None,
    cancel_cb=None,
) -> GalleryImportSummary:
    """Import each image into a gallery or the global backdrop library.

    *char_id* selects the target: a card id imports into that character's
    gallery, ``None`` imports into the shared VN backdrops.  Identical
    content (SHA-256) is never stored twice for one target: a re-dropped
    export counts as a skip, not a second copy.  *progress_cb* is called
    with ``(done, total, filename)``; *cancel_cb* is polled before each file
    so a long batch can be abandoned mid-way.  Exceptions are captured per
    file so one bad image never aborts the batch.
    """
    result = summary if summary is not None else GalleryImportSummary()
    total = len(paths)
    done = 0
    for path in paths:
        if cancel_cb is not None and cancel_cb():
            break
        if progress_cb is not None:
            progress_cb(done, total, Path(path).name)
        try:
            data_hash = gallery_store.content_hash(vault.read_bytes(path))
            if char_id is None:
                if db.find_backdrop_by_hash(data_hash) is not None:
                    result.add_skipped(path)
                    continue
                meta = gallery_store.import_backdrop_image(path)
                db.add_backdrop_image(meta['file_name'], meta['content_hash'])
            else:
                if db.find_gallery_by_hash(char_id, data_hash) is not None:
                    result.add_skipped(path)
                    continue
                meta = gallery_store.import_gallery_image(char_id, path)
                db.add_gallery_image(
                    char_id, meta['file_name'], meta['content_hash'],
                )
            result.add_imported()
        except Exception as e:
            logger.warning("Gallery import failed for %s: %s", path, e)
            result.add_error(path, str(e))
        done += 1
    return result


try:
    from PyQt6.QtCore import QThread, pyqtSignal
except ImportError:  # pragma: no cover - exercised only without Qt installed
    QThread = None  # type: ignore[misc,assignment]


if QThread is not None:

    class GalleryImportWorker(QThread):
        """Run :func:`import_gallery_paths` off the UI thread."""

        progress = pyqtSignal(int, int, str)   # done, total, filename
        completed = pyqtSignal(object)         # GalleryImportSummary
        error = pyqtSignal(str)

        def __init__(self, char_id: int | None, paths: list[str], db, parent=None):
            super().__init__(parent)
            self._char_id = char_id
            self._paths = list(paths)
            self._db = db
            self._cancelled = False

        def cancel(self) -> None:
            self._cancelled = True

        def run(self) -> None:
            try:
                summary = import_gallery_paths(
                    self._char_id,
                    self._paths,
                    self._db,
                    progress_cb=self._on_progress,
                    cancel_cb=lambda: self._cancelled,
                )
                self.completed.emit(summary)
            except Exception as e:
                logger.exception("Gallery import worker failed")
                self.error.emit(str(e))

        def _on_progress(self, done: int, total: int, name: str) -> None:
            self.progress.emit(done, total, name)

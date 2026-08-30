from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from src.card_models import CharacterCard
from src.card_parser import read_card_data
from src.token_counter import count_card_tokens

logger = logging.getLogger(__name__)


@dataclass
class FileImportResult:
    """Outcome of importing a single file.

    Exactly one of ``char_id`` (success) / ``error`` (failure) is set.  When
    the file was skipped because it duplicates an existing card,
    ``is_duplicate`` is True and ``error`` holds the human-readable reason.
    """

    path: str
    char_id: int | None = None
    error: str | None = None
    is_duplicate: bool = False

    @property
    def succeeded(self) -> bool:
        return self.char_id is not None


@dataclass
class ImportSummary:
    """Accumulates the results of a batch import.

    Pure logic (no Qt) so the result accumulation can be unit-tested without
    a running Qt event loop.  The :class:`ImportWorker` thread builds one of
    these as it iterates and emits it with its ``completed`` signal.
    """

    imported: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)

    def add_imported(self, char_id: int) -> None:
        self.imported += 1

    def add_skipped(self, path: str, reason: str | None = None) -> None:
        self.skipped += 1
        self.skipped_paths.append(path)

    def add_error(self, path: str, error: str) -> None:
        self.errors.append(f"{Path(path).name}: {error}")

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def message(self) -> str:
        """Build a short status-bar summary string.

        Mirrors :func:`src.ui.library_tab._build_import_summary` (kept inline
        here to avoid a circular import back into the tab that owns the worker).
        """
        msg = f"Imported {self.imported} card(s)."
        if self.skipped:
            msg += f" Skipped {self.skipped} duplicate(s)."
        if self.errors:
            msg += f" {len(self.errors)} error(s)."
        return msg


def import_single_file(
    path: str,
    db,
    force_duplicates: bool = False,
) -> FileImportResult:
    """Import a single card file into the library.

    *db* is expected to expose ``find_duplicate(card) -> dict | None`` and
    ``add_card(card) -> int`` (the real :class:`LibraryDatabase` does).  When
    *force_duplicates* is False and the card matches an existing entry, the
    file is skipped and the result is marked as a duplicate (no error).

    Returns a :class:`FileImportResult`; never raises — exceptions are
    captured into the result so the calling loop can continue with the next
    file.
    """
    try:
        raw = read_card_data(path)
        if raw is None:
            return FileImportResult(path=path, error='No character data found')
        card = CharacterCard.from_spec_dict(raw, source_path=str(path))
        card.token_count = count_card_tokens(card)
        if not force_duplicates:
            dup = db.find_duplicate(card)
            if dup:
                return FileImportResult(
                    path=path,
                    error=f"Duplicate of '{dup.get('name', card.name)}'",
                    is_duplicate=True,
                )
        char_id = db.add_card(card)
        if char_id:
            return FileImportResult(path=path, char_id=char_id)
        return FileImportResult(path=path, error='Import failed')
    except Exception as e:
        logger.exception("Import error for %s", path)
        return FileImportResult(path=path, error=str(e))


def run_import_loop(
    paths: list[str],
    db,
    force_duplicates: bool = False,
    is_cancelled=None,
    on_progress=None,
    on_result=None,
) -> ImportSummary:
    """Run the import loop over *paths* without any Qt dependencies.

    This is the testable core of :class:`ImportWorker.run`.  *is_cancelled*
    is a callable returning True to stop between files (cooperative cancel).
    *on_progress(current, total, name)* and *on_result(FileImportResult)* are
    optional callbacks (the worker wires them to Qt signals).

    Returns the accumulated :class:`ImportSummary`.
    """
    summary = ImportSummary()
    total = len(paths)
    for i, path in enumerate(paths):
        if is_cancelled is not None and is_cancelled():
            break
        if on_progress is not None:
            on_progress(i + 1, total, Path(path).name)
        res = import_single_file(path, db, force_duplicates)
        if res.succeeded:
            summary.add_imported(res.char_id)  # type: ignore[arg-type]
        elif res.is_duplicate:
            summary.add_skipped(res.path, res.error)
        else:
            summary.add_error(res.path, res.error or 'Unknown error')
        if on_result is not None:
            on_result(res)
    return summary


class ImportWorker(QThread):
    """Import card files off the UI thread with progress + cancellation.

    Signals:
        progress(current, total, name)  — per-file progress update.
        result(path, char_id, error)    — per-file outcome (char_id is None
                                          on skip/error; error is '' on
                                          success).
        completed(summary)              — ImportSummary once the loop ends
                                          (cancelled or complete).
    """

    progress = pyqtSignal(int, int, str)
    result = pyqtSignal(str, object, str)
    # Named ``completed`` so the built-in QThread.finished stays available
    # for lifetime management (deleteLater).
    completed = pyqtSignal(object)

    def __init__(self, db, paths: list[str], force_duplicates: bool = False, parent=None):
        super().__init__(parent)
        self.db = db
        self.paths = list(paths)
        self.force_duplicates = force_duplicates
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        summary = run_import_loop(
            self.paths,
            self.db,
            force_duplicates=self.force_duplicates,
            is_cancelled=lambda: self._cancel,
            on_progress=lambda c, t, n: self.progress.emit(c, t, n),
            on_result=lambda r: self.result.emit(
                r.path,
                r.char_id,
                r.error or ('Duplicate' if r.is_duplicate else ''),
            ),
        )
        self.completed.emit(summary)
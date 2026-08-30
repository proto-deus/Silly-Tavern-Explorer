from __future__ import annotations

import logging
import os

from PyQt6.QtCore import QThread, pyqtSignal

from src.sillytavern_sync import (
    build_explorer_entries,
    bulk_sync,
    compare_libraries,
    list_st_characters,
)

logger = logging.getLogger(__name__)


def _get_deleted_entries(db) -> list:
    """Fetch deletion tombstones; tolerate minimal/absent support."""
    try:
        return db.get_deleted_st_cards()
    except AttributeError:
        return []
    except Exception:
        logger.exception("Could not read deletion tombstones")
        return []


def _scan_pairs(db, characters_dir: str) -> list:
    """Compare both libraries, including deletion tombstones.

    Tombstones mark cards deleted on purpose in ST Explorer so their ST
    copies classify as ``DELETED_IN_EXPLORER`` instead of ``ONLY_ST``.
    """
    rows = db.get_all()
    explorer_entries = build_explorer_entries(rows)
    deleted_entries = _get_deleted_entries(db)
    st_entries = list_st_characters(characters_dir)
    return compare_libraries(explorer_entries, st_entries, deleted_entries)


class SyncWorker(QThread):
    """Run a sync plan off the UI thread, then rescan both libraries.

    Signals:
        progress(current, total, name) — per-item progress update.
        completed(result)              — tuple ``(SyncSummary, list[SyncPair])``
                                         once the plan + rescan complete.
    """

    progress = pyqtSignal(int, int, str)
    # Named ``completed`` (not ``finished``) so the built-in QThread.finished
    # signal remains available for lifetime management (deleteLater).
    completed = pyqtSignal(object)

    def __init__(self, db, plan, characters_dir, parent=None):
        super().__init__(parent)
        self.db = db
        self.plan = plan
        self.characters_dir = characters_dir
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from src.sillytavern_sync import SyncSummary
        try:
            summary = bulk_sync(
                self.plan,
                self.db,
                self.characters_dir,
                is_cancelled=lambda: self._cancel,
                on_progress=lambda c, t, n: self.progress.emit(c, t, n),
            )
        except Exception as e:
            logger.exception("SyncWorker crashed during bulk_sync")
            summary = SyncSummary(errors=[f"Unexpected error: {e}"])

        # Rescan both libraries so the UI can refresh the tree in one step.
        pairs: list = []
        try:
            if not self._cancel:
                pairs = _scan_pairs(self.db, self.characters_dir)
        except Exception:
            logger.exception("SyncWorker crashed during rescan")

        self.completed.emit((summary, pairs))


class ScanWorker(QThread):
    """Scan both libraries off the UI thread.

    Reading and hashing every card PNG from disk is expensive on large
    libraries; running it on the UI thread freezes the window.  This
    worker performs the scan in the background and emits the resulting
    list of :class:`SyncPair` objects.

    Signals:
        completed(pairs) — ``list[SyncPair]`` on success.
        failed(message)  — error message string on failure.
    """

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, db, characters_dir, parent=None):
        super().__init__(parent)
        self.db = db
        self.characters_dir = characters_dir
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            logger.info("ScanWorker: starting scan (dir=%s, cancel=%s)",
                         self.characters_dir, self._cancel)
            pairs = _scan_pairs(self.db, self.characters_dir)
            logger.info("ScanWorker: compare_libraries returned %d pairs", len(pairs))
            if self._cancel:
                logger.info("ScanWorker: cancelled after compare_libraries")
                return
            self.completed.emit(pairs)
        except Exception as e:
            logger.exception("Sync scan failed")
            self.failed.emit(str(e))


class PushPullWorker(QThread):
    """Push or pull a single card off the UI thread.

    Builds the necessary sync entries (reading ST PNGs as needed) and
    delegates to :func:`bulk_sync` with a one-item plan.  Used by the
    main window's "Push/Pull All" menu actions so the UI thread is
    never blocked by file I/O.

    Signals:
        completed(summary) — :class:`SyncSummary` on completion.
    """

    completed = pyqtSignal(object)

    def __init__(self, db, characters_dir, char_id, action, parent=None):
        super().__init__(parent)
        self.db = db
        self.characters_dir = characters_dir
        self.char_id = char_id
        self.action = action

    def run(self) -> None:
        from src.sillytavern_sync import (
            ExplorerSyncEntry,
            SyncAction,
            SyncCategory,
            SyncPair,
            SyncPlanItem,
            SyncSummary,
        )

        entry = self.db.get_by_id(self.char_id)
        if not entry:
            self.completed.emit(SyncSummary(errors=[f"Card {self.char_id} not found"]))
            return

        if self.action == SyncAction.PUSH:
            source = entry.get('source_path', '')
            ex = ExplorerSyncEntry(
                char_id=self.char_id,
                name=entry.get('name', ''),
                creator=entry.get('creator', ''),
                source_path=source,
                st_avatar_url=entry.get('st_avatar_url'),
                st_sync_hash=entry.get('st_sync_hash'),
                current_hash='',
            )
            pair = SyncPair(category=SyncCategory.ONLY_EXPLORER, explorer=ex)
        else:
            avatar_url = entry.get('st_avatar_url')
            if not avatar_url:
                self.completed.emit(SyncSummary(errors=['Card not linked to SillyTavern']))
                return
            st_entries = list_st_characters(self.characters_dir)
            # Case-insensitive match: comparison uses os.path.normcase, so a
            # case-only rename of the ST file must still resolve.
            wanted = os.path.normcase(avatar_url)
            st_entry = next(
                (e for e in st_entries if os.path.normcase(e.filename) == wanted), None,
            )
            if st_entry is None:
                self.completed.emit(SyncSummary(
                    errors=[f"File '{avatar_url}' not found in SillyTavern"],
                ))
                return
            pair = SyncPair(category=SyncCategory.ONLY_ST, st=st_entry)

        plan = [SyncPlanItem(pair, self.action)]
        summary = bulk_sync(plan, self.db, self.characters_dir)
        self.completed.emit(summary)

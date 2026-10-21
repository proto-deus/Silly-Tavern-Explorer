from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from src.lorebook_sync import (
    LorebookPlanItem,
    LorebookSyncCategory,
    LorebookSyncPair,
    bulk_lorebook_sync,
    compare_lorebooks,
    list_explorer_lorebooks,
    list_st_lorebooks,
    load_sync_state,
)
from src.sillytavern_sync import SyncSummary

logger = logging.getLogger(__name__)

#: The pair categories whose two sides can be compared entry by entry.
DIFFABLE_CATEGORIES = frozenset({
    LorebookSyncCategory.ST_CHANGED,
    LorebookSyncCategory.EXPLORER_CHANGED,
    LorebookSyncCategory.BOTH_CHANGED,
})


def diffable_pairs(pairs) -> list:
    """The lorebook pairs whose two copies should be compared.

    Only pairs that exist on both sides and are not in sync can show a
    difference.  Pure function so the selection is unit-testable without Qt.
    """
    out = []
    for pair in pairs or ():
        if pair.explorer is None or pair.st is None:
            continue
        if pair.category in DIFFABLE_CATEGORIES:
            out.append(pair)
    return out


class LorebookScanWorker(QThread):
    """Scan both lorebook libraries off the UI thread.

    Signals:
        completed(pairs) — ``list[LorebookSyncPair]`` on success.
        failed(message)  — error message string on failure.
    """

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, worlds_dir, parent=None):
        super().__init__(parent)
        self.worlds_dir = worlds_dir
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            explorer_entries = list_explorer_lorebooks()
            if self._cancel:
                return
            st_entries = list_st_lorebooks(self.worlds_dir)
            if self._cancel:
                return
            pairs = compare_lorebooks(explorer_entries, st_entries, load_sync_state())
            if self._cancel:
                return
            self.completed.emit(pairs)
        except Exception as e:
            logger.exception("Lorebook sync scan failed")
            self.failed.emit(str(e))


class LorebookDiffWorker(QThread):
    """Compare the Explorer and SillyTavern copies of the given lorebooks.

    Reading and parsing both copies of every book is too slow for the UI
    thread, so the field-level diff is computed in the background.

    Signals:
        completed(diffs) — ``list[CardDiff]`` on success.
        failed(message)  — error message string on failure.
    """

    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, pairs, parent=None):
        super().__init__(parent)
        self.pairs = list(pairs or ())
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from src.lorebook_store import parse_book_json
        from src.safe_json import loads_safe
        from src.sync_diff import diff_lorebooks
        from src.fs_utils import decode_text
        from src import vault

        def _read_book(path: str):
            """Parse the lorebook stored at *path* (vault-aware)."""
            try:
                raw_bytes = vault.read_bytes(Path(path))
                return parse_book_json(loads_safe(decode_text(raw_bytes)))
            except (OSError, ValueError, vault.VaultError) as exc:
                logger.warning("Could not read the lorebook %s: %s", path, exc)
                return None

        diffs = []
        try:
            for pair in self.pairs:
                if self._cancel:
                    return
                explorer = pair.explorer
                st = pair.st
                if explorer is None or st is None:
                    continue
                local = _read_book(explorer.path)
                remote = _read_book(st.path)
                if local is None or remote is None:
                    continue
                diffs.append(diff_lorebooks(local, remote))
        except Exception as e:
            logger.exception("LorebookDiffWorker crashed while comparing the books")
            self.failed.emit(str(e))
            return
        self.completed.emit(diffs)


class LorebookActionWorker(QThread):
    """Execute a lorebook sync plan off the UI thread, then rescan.

    Signals:
        progress(current, total, name) — per-item progress update.
        completed(result)              — ``(SyncSummary, list[LorebookSyncPair])``
                                         once the plan + rescan complete.
    """

    progress = pyqtSignal(int, int, str)
    completed = pyqtSignal(object)

    def __init__(self, plan: list[LorebookPlanItem], worlds_dir, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.worlds_dir = worlds_dir
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            summary = bulk_lorebook_sync(
                self.plan,
                self.worlds_dir,
                is_cancelled=lambda: self._cancel,
                on_progress=lambda c, t, n: self.progress.emit(c, t, n),
            )
        except Exception as e:
            logger.exception("LorebookActionWorker crashed during bulk sync")
            summary = SyncSummary(errors=[f"Unexpected error: {e}"])

        # Rescan so the dialog can refresh its tree in one step.
        pairs: list[LorebookSyncPair] = []
        try:
            if not self._cancel:
                pairs = compare_lorebooks(
                    list_explorer_lorebooks(),
                    list_st_lorebooks(self.worlds_dir),
                    load_sync_state(),
                )
        except Exception:
            logger.exception("LorebookActionWorker crashed during rescan")

        self.completed.emit((summary, pairs))

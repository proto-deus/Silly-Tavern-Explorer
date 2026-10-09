"""Snapshot-based undo/redo for the character editor form.

The edit form is a set of independent Qt widgets, so Qt's own per-widget undo
stack cannot undo a change across fields (editing the description then the name
would need two undos, in reverse order, with no way to interleave). This keeps
whole-form snapshots instead: each committed change pushes the previous state,
and undo/redo swaps the full field set back.

Deliberately Qt-free so the snapshot/restore logic can be unit-tested without an
event loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FormSnapshot:
    """The editable state of the card form at one point in time."""

    values: dict[str, Any] = field(default_factory=dict)
    character_book: Any = None
    extensions: Any = None
    label: str = ''


class UndoStack:
    """Bounded snapshot history with undo and redo.

    *limit* bounds memory: a snapshot of a long description is not free, and a
    user editing for an hour should not accumulate thousands of them. The oldest
    entry is dropped first.
    """

    def __init__(self, limit: int = 50):
        if limit < 1:
            raise ValueError('limit must be >= 1')
        self._limit = limit
        self._undo: list[FormSnapshot] = []
        self._redo: list[FormSnapshot] = []
        self._present: FormSnapshot | None = None
        self._baseline: FormSnapshot | None = None

    # ---- state ----

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ''

    @property
    def redo_label(self) -> str:
        return self._redo[-1].label if self._redo else ''

    def __len__(self) -> int:
        return len(self._undo)

    # ---- recording ----

    def commit(self, snapshot: FormSnapshot) -> None:
        """Record *snapshot* as the state after a committed change.

        The first commit establishes the baseline and is not undoable; every
        later commit pushes the previous state onto the undo stack and clears
        the redo stack (the usual linear-history rule).
        """
        if self._present is None:
            self._present = snapshot
            return
        if self._same(self._present, snapshot):
            # Nothing actually changed (e.g. a focus/blur round-trip).
            return
        self._undo.append(self._present)
        if len(self._undo) > self._limit:
            self._undo.pop(0)
        self._present = snapshot
        self._redo.clear()

    def reset(self, snapshot: FormSnapshot) -> None:
        """Start a new history at *snapshot* (after loading a card)."""
        self._undo.clear()
        self._redo.clear()
        self._present = snapshot
        self._baseline = snapshot

    @property
    def is_at_baseline(self) -> bool:
        """True when the current state equals the last-loaded (pristine) state.

        Undoing back to the pristine state must not leave the form flagged
        as having unsaved changes.
        """
        return (
            self._baseline is not None
            and self._present is not None
            and self._same(self._baseline, self._present)
        )

    # ---- navigation ----

    def undo(self) -> FormSnapshot | None:
        """Step back one change and return the state to restore, or None."""
        if not self._undo:
            return None
        current = self._present
        self._redo.append(current)
        self._present = self._undo.pop()
        return self._present

    def redo(self) -> FormSnapshot | None:
        """Step forward one change and return the state to restore, or None."""
        if not self._redo:
            return None
        self._undo.append(self._present)
        if len(self._undo) > self._limit:
            self._undo.pop(0)
        self._present = self._redo.pop()
        return self._present

    @staticmethod
    def _same(a: FormSnapshot, b: FormSnapshot) -> bool:
        return (a.values == b.values
                and a.character_book == b.character_book
                and a.extensions == b.extensions)
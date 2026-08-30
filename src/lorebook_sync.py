"""Two-way sync between ST Explorer lorebooks and SillyTavern world-info files.

SillyTavern keeps world-info books as JSON files under
``data/<user>/worlds/``.  This module compares that directory against the
ST Explorer lorebook library (``~/.st-explorer/lorebooks/``), matching books
by *filename* — the identity SillyTavern itself uses when a chat references
a world — and classifies each pair using a semantic content hash plus a
baseline state file, mirroring the card-sync approach in
:mod:`src.sillytavern_sync`.

Because both sides are compared through their parsed :class:`CharacterBook`
form, format differences do not matter: an Explorer-schema book and its
ST-native conversion compare equal.  Push converts to ST's native shape via
``lorebook_store.book_to_st_world_info``; pull parses either format via
``lorebook_store.parse_book_json``.  Pure filesystem logic — no Qt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from src.fs_utils import atomic_write_bytes
from src import lorebook_store
from src.sillytavern_sync import SyncAction, SyncSummary

logger = logging.getLogger(__name__)

STATE_FILENAME = 'lorebook_sync_state.json'

# Sentinel semantic hash for files that exist but cannot be parsed.
_UNREADABLE = '<unreadable>'


class LorebookSyncCategory(Enum):
    """Classification of a lorebook pair during comparison."""
    ONLY_EXPLORER = 'only_explorer'
    ONLY_ST = 'only_st'
    IN_SYNC = 'in_sync'
    EXPLORER_CHANGED = 'explorer_changed'
    ST_CHANGED = 'st_changed'
    BOTH_CHANGED = 'both_changed'


_LOREBOOK_CATEGORY_LABELS: dict[LorebookSyncCategory, str] = {
    LorebookSyncCategory.ONLY_EXPLORER: 'Only in ST Explorer',
    LorebookSyncCategory.ONLY_ST: 'Only in SillyTavern',
    LorebookSyncCategory.IN_SYNC: 'In sync',
    LorebookSyncCategory.EXPLORER_CHANGED: 'Changed in ST Explorer',
    LorebookSyncCategory.ST_CHANGED: 'Changed in SillyTavern',
    LorebookSyncCategory.BOTH_CHANGED: 'Conflict (both changed)',
}


def lorebook_category_label(cat: LorebookSyncCategory) -> str:
    """Return a human-readable label for *cat*."""
    return _LOREBOOK_CATEGORY_LABELS.get(cat, cat.value)


@dataclass
class LorebookFileEntry:
    """A lorebook JSON file found on one side of the sync."""
    filename: str          # e.g. 'my_world.json' (the ST identity)
    name: str              # book display name ('' when unset)
    entry_count: int
    path: str
    content_hash: str      # sha256 of the raw file bytes
    semantic_hash: str     # sha256 of the canonical parsed book


@dataclass
class LorebookSyncPair:
    """A comparison result pairing explorer and/or ST lorebooks."""
    category: LorebookSyncCategory
    explorer: LorebookFileEntry | None = None
    st: LorebookFileEntry | None = None


@dataclass
class LorebookPlanItem:
    """A single item in a lorebook sync plan: pair + action."""
    pair: LorebookSyncPair
    action: SyncAction


# ---------------------------------------------------------------------------
# Paths / state
# ---------------------------------------------------------------------------

def resolve_worlds_dir(characters_dir: str | Path) -> Path:
    """Derive the ST worlds directory from the configured characters dir.

    ST lays these out as siblings under ``data/<user>/``:
    ``.../characters`` <-> ``.../worlds``.  For any configured path the
    ``worlds`` sibling inside the same parent directory is used.
    """
    p = Path(characters_dir)
    return p.parent / 'worlds'


def _state_path() -> Path:
    return lorebook_store._base_data_dir() / STATE_FILENAME


def load_sync_state() -> dict[str, dict[str, str]]:
    """Load the baseline state: ``{normcase(filename): {'explorer': h, 'st': h}}``."""
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            result[key] = {
                str(k): str(v) for k, v in value.items() if k in ('explorer', 'st')
            }
    return result


def save_sync_state(state: dict[str, dict[str, str]]) -> None:
    """Persist the baseline state atomically."""
    atomic_write_bytes(
        _state_path(),
        json.dumps(state, ensure_ascii=False, indent=2).encode('utf-8'),
    )


def forget_pair(filename: str) -> None:
    """Drop the baseline for *filename* (e.g. after an unlink/delete)."""
    state = load_sync_state()
    state.pop(os.path.normcase(filename), None)
    save_sync_state(state)


def _mark_pair_synced(filename: str, explorer_semantic: str, st_semantic: str) -> None:
    state = load_sync_state()
    state[os.path.normcase(filename)] = {
        'explorer': explorer_semantic,
        'st': st_semantic,
    }
    save_sync_state(state)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _normalize_book(book):
    """Return *book* mapped through the ST conversion and back.

    The ST-native shape normalizes several fields (``selective``,
    ``probability``, ``uid`` assignment, …), so two files representing the
    same logical book can serialize differently depending on which side
    wrote them.  Round-tripping through ``book_to_st_world_info`` ->
    ``st_world_info_to_book`` is idempotent, giving both sides a shared
    canonical form for comparison.
    """
    return lorebook_store.st_world_info_to_book(
        lorebook_store.book_to_st_world_info(book),
    )


def semantic_hash_of_book(book) -> str:
    """Content hash of a book's ST-normalized canonical form.

    Format-agnostic: an Explorer-schema book and its ST-native conversion
    hash identically.
    """
    canonical = json.dumps(
        _normalize_book(book).to_dict(),
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _entry_from_file(path: Path) -> LorebookFileEntry | None:
    try:
        raw_bytes = path.read_bytes()
        raw = json.loads(raw_bytes.decode('utf-8'))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Skipping unreadable lorebook file %s: %s", path.name, exc)
        return None
    book = lorebook_store.parse_book_json(raw)
    return LorebookFileEntry(
        filename=path.name,
        name=book.name or path.stem,
        entry_count=len(book.entries),
        path=str(path),
        content_hash=hashlib.sha256(raw_bytes).hexdigest(),
        semantic_hash=semantic_hash_of_book(book),
    )


def list_explorer_lorebooks() -> list[LorebookFileEntry]:
    """List every readable lorebook in the Explorer library."""
    entries: list[LorebookFileEntry] = []
    directory = lorebook_store.get_lorebooks_dir()
    for path in sorted(directory.glob('*.json'), key=lambda p: p.name.lower()):
        entry = _entry_from_file(path)
        if entry is not None:
            entries.append(entry)
    return entries


def list_st_lorebooks(worlds_dir: str | Path) -> list[LorebookFileEntry]:
    """List every readable world-info JSON in the ST worlds directory."""
    cdir = Path(worlds_dir)
    if not cdir.is_dir():
        return []
    entries: list[LorebookFileEntry] = []
    for path in sorted(cdir.glob('*.json'), key=lambda p: p.name.lower()):
        entry = _entry_from_file(path)
        if entry is not None:
            entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _classify_pair(
    ex: LorebookFileEntry,
    st: LorebookFileEntry,
    baseline: dict[str, str],
) -> LorebookSyncCategory:
    if ex.semantic_hash == st.semantic_hash:
        return LorebookSyncCategory.IN_SYNC
    base_ex = baseline.get('explorer')
    base_st = baseline.get('st')
    ex_known = bool(base_ex)
    st_known = bool(base_st)
    ex_changed = ex_known and ex.semantic_hash != base_ex
    st_changed = st_known and st.semantic_hash != base_st
    if not ex_known and not st_known:
        # No baseline: identical content was handled above; anything else
        # is indistinguishable from a two-sided change until first sync.
        return LorebookSyncCategory.BOTH_CHANGED
    if ex_changed and not st_changed:
        return LorebookSyncCategory.EXPLORER_CHANGED
    if st_changed and not ex_changed:
        return LorebookSyncCategory.ST_CHANGED
    return LorebookSyncCategory.BOTH_CHANGED


def compare_lorebooks(
    explorer_entries: list[LorebookFileEntry],
    st_entries: list[LorebookFileEntry],
    state: dict[str, dict[str, str]] | None = None,
) -> list[LorebookSyncPair]:
    """Compare both libraries and classify each lorebook pair.

    Matching is by filename (case-insensitive via ``os.path.normcase``).
    Change detection uses each side's semantic hash against the baseline
    recorded at the last successful push/pull.
    """
    state = state if state is not None else {}
    st_by_filename: dict[str, LorebookFileEntry] = {
        os.path.normcase(e.filename): e for e in st_entries
    }
    pairs: list[LorebookSyncPair] = []
    consumed: set[str] = set()

    for ex in explorer_entries:
        key = os.path.normcase(ex.filename)
        st = st_by_filename.get(key)
        if st is None:
            pairs.append(LorebookSyncPair(
                category=LorebookSyncCategory.ONLY_EXPLORER, explorer=ex,
            ))
            continue
        consumed.add(key)
        pairs.append(LorebookSyncPair(
            category=_classify_pair(ex, st, state.get(key, {})),
            explorer=ex, st=st,
        ))

    for st in st_entries:
        if os.path.normcase(st.filename) not in consumed:
            pairs.append(LorebookSyncPair(
                category=LorebookSyncCategory.ONLY_ST, st=st,
            ))
    return pairs


def summarize_lorebook_pairs(pairs: list[LorebookSyncPair]) -> dict[str, int]:
    """Count pairs by category. Returns ``{category_value: count}``."""
    counts: dict[str, int] = {}
    for p in pairs:
        counts[p.category.value] = counts.get(p.category.value, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Push / Pull / bulk
# ---------------------------------------------------------------------------

def pull_lorebook(st_entry: LorebookFileEntry) -> str | None:
    """Pull an ST world-info file into the Explorer library.

    Keeps ST's filename so future comparisons match.  Returns ``None`` on
    success or an error string.
    """
    try:
        raw = json.loads(Path(st_entry.path).read_text(encoding='utf-8'))
        book = lorebook_store.parse_book_json(raw)
        lorebook_store.save_lorebook(st_entry.filename, book)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Lorebook pull failed for %s: %s", st_entry.filename, exc)
        return str(exc)
    sem = semantic_hash_of_book(book)
    _mark_pair_synced(st_entry.filename, sem, sem)
    logger.info("Pulled lorebook '%s' from SillyTavern", st_entry.filename)
    return None


def push_lorebook(ex_entry: LorebookFileEntry, worlds_dir: str | Path) -> str | None:
    """Push an Explorer lorebook to the ST worlds directory (ST-native format).

    Writes atomically into *worlds_dir* under the same filename.  Returns
    ``None`` on success or an error string.
    """
    try:
        book = lorebook_store.load_lorebook(ex_entry.filename)
        if book is None:
            return f"Lorebook '{ex_entry.filename}' could not be loaded"
        data = lorebook_store.book_to_st_world_info(book)
        target = Path(worlds_dir) / Path(ex_entry.filename).name
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        atomic_write_bytes(target, payload)
    except OSError as exc:
        logger.warning("Lorebook push failed for %s: %s", ex_entry.filename, exc)
        return str(exc)
    sem = semantic_hash_of_book(book)
    _mark_pair_synced(ex_entry.filename, sem, sem)
    logger.info("Pushed lorebook '%s' to SillyTavern", ex_entry.filename)
    return None


def build_lorebook_pull_all_plan(
    pairs: list[LorebookSyncPair],
) -> list[LorebookPlanItem]:
    """Plan that pulls all new/changed-in-ST books (non-destructive).

    Conflicts and locally-changed books are skipped so unpushed edits can
    never be overwritten by a bulk operation.
    """
    plan: list[LorebookPlanItem] = []
    for pair in pairs:
        if pair.category in (LorebookSyncCategory.ONLY_ST,
                             LorebookSyncCategory.ST_CHANGED):
            plan.append(LorebookPlanItem(pair, SyncAction.PULL))
        else:
            plan.append(LorebookPlanItem(pair, SyncAction.SKIP))
    return plan


def build_lorebook_push_all_plan(
    pairs: list[LorebookSyncPair],
) -> list[LorebookPlanItem]:
    """Plan that pushes all new/changed-in-Explorer books (non-destructive)."""
    plan: list[LorebookPlanItem] = []
    for pair in pairs:
        if pair.category in (LorebookSyncCategory.ONLY_EXPLORER,
                             LorebookSyncCategory.EXPLORER_CHANGED):
            plan.append(LorebookPlanItem(pair, SyncAction.PUSH))
        else:
            plan.append(LorebookPlanItem(pair, SyncAction.SKIP))
    return plan


def build_lorebook_sync_plan(
    pairs: list[LorebookSyncPair],
) -> list[LorebookPlanItem]:
    """Default bulk plan: safe pulls + pushes, conflicts left for manual resolution."""
    plan: list[LorebookPlanItem] = []
    for pair in pairs:
        cat = pair.category
        if cat in (LorebookSyncCategory.ONLY_ST, LorebookSyncCategory.ST_CHANGED):
            plan.append(LorebookPlanItem(pair, SyncAction.PULL))
        elif cat in (LorebookSyncCategory.ONLY_EXPLORER,
                     LorebookSyncCategory.EXPLORER_CHANGED):
            plan.append(LorebookPlanItem(pair, SyncAction.PUSH))
        else:
            plan.append(LorebookPlanItem(pair, SyncAction.SKIP))
    return plan


def active_plan_items(plan: list[LorebookPlanItem]) -> list[LorebookPlanItem]:
    """Return only the items whose action is not SKIP (for progress sizing)."""
    return [item for item in plan if item.action != SyncAction.SKIP]


def bulk_lorebook_sync(
    plan: list[LorebookPlanItem],
    worlds_dir: str | Path,
    is_cancelled=None,
    on_progress=None,
) -> SyncSummary:
    """Execute a lorebook sync plan. Pure logic (no Qt).

    Skipped items count toward progress but are not actioned.  Returns the
    accumulated :class:`SyncSummary` (reuses the card-sync summary type).
    """
    summary = SyncSummary()
    total = len(plan)
    for i, item in enumerate(plan):
        if is_cancelled is not None and is_cancelled():
            break
        if on_progress is not None:
            pair = item.pair
            name = (
                pair.explorer.filename if pair.explorer
                else (pair.st.filename if pair.st else '?')
            )
            on_progress(i + 1, total, name)

        if item.action == SyncAction.SKIP:
            summary.skipped += 1
        elif item.action == SyncAction.PULL:
            if item.pair.st is None:
                summary.errors.append("Cannot pull: no ST lorebook")
                continue
            error = pull_lorebook(item.pair.st)
            if error is None:
                summary.pulled += 1
            else:
                summary.errors.append(f"{item.pair.st.filename}: {error}")
        elif item.action == SyncAction.PUSH:
            if item.pair.explorer is None:
                summary.errors.append("Cannot push: no Explorer lorebook")
                continue
            error = push_lorebook(item.pair.explorer, worlds_dir)
            if error is None:
                summary.pushed += 1
            else:
                summary.errors.append(f"{item.pair.explorer.filename}: {error}")
    return summary

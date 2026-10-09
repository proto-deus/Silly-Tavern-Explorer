from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from src.card_models import CharacterCard
from src.card_parser import read_card_data, read_chara_card, write_chara_card_dual
from src.fs_utils import atomic_replace, unique_temp_path
from src.token_counter import count_card_tokens

logger = logging.getLogger(__name__)


_ST_ILLEGAL_CHARS = re.compile(r'[\\?*:/|"<>]')
# Windows reserved device names (case-insensitive), per sanitize-filename.
_ST_RESERVED_NAMES = re.compile(
    # Windows also reserves clock$ and the superscript forms com1-com9 /
    # lpt1-lpt9, which the original pattern missed.
    r'(?i)^(con|prn|aux|nul|clock\$|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$'
)

def sanitize_st_filename(name: str) -> str:
    """Sanitize a character name for use as a SillyTavern PNG filename.

    Mirrors the npm ``sanitize-filename`` package that ST uses: removes
    ``\\ ? * : / | " < >`` and non-printable characters, then trims
    whitespace and leading/trailing dots.
    """
    cleaned = _ST_ILLEGAL_CHARS.sub('', name)
    cleaned = ''.join(c for c in cleaned if c.isprintable())
    cleaned = cleaned.strip().strip('.')
    if _ST_RESERVED_NAMES.match(cleaned):
        cleaned = ''
    return cleaned[:200] if cleaned else 'untitled'


def st_filename_for(card: CharacterCard) -> str:
    """Return the ST-conventional PNG filename for *card*."""
    base = sanitize_st_filename(card.name) or 'untitled'
    return f"{base}.png"


def _fs_key(name: str) -> str:
    """Case-folded key for comparing filenames on the target filesystem.

    ``os.path.normcase`` is a **no-op on POSIX**, yet the default macOS volume
    (APFS/HFS+) is case-insensitive. Comparing with it there meant ``card.png``
    looked free when ``Card.png`` already existed, and the push silently
    overwrote an unrelated character. Case-fold unconditionally: over-resolving
    on a case-sensitive Linux volume only costs a harmless ``_1`` suffix,
    whereas under-resolving destroys a card.
    """
    return (name or '').casefold()


def resolve_st_filename(
    base_name: str,
    existing: set[str],
    linked_name: Optional[str] = None,
) -> str:
    """Resolve a unique filename in the ST characters directory.

    If *linked_name* is given (the card is already linked to a specific ST
    file), reuse it so pushes overwrite the same file - but only when nothing
    else in the directory claims that name; otherwise the link is stale (a
    case-only rename, or the file was replaced) and reusing it would clobber
    that card.  Otherwise, if *base_name* collides with an *existing*
    filename, append ``_1``, ``_2``, ... suffixes until unique, matching ST's
    ``getPngName`` behavior.
    """
    existing_norm = {_fs_key(f) for f in existing}
    if linked_name:
        # The link names the file this card owns. Reuse it unless a
        # *different* existing file would be overwritten.
        if _fs_key(linked_name) not in existing_norm or linked_name in existing:
            return linked_name
        logger.info(
            "ST link '%s' collides with an existing file; resolving a unique name",
            linked_name,
        )
    candidate = base_name
    if _fs_key(candidate) not in existing_norm:
        return candidate
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    i = 1
    while True:
        candidate = f"{stem}_{i}{suffix}"
        if _fs_key(candidate) not in existing_norm:
            return candidate
        i += 1


def _canonical_json_value(value):
    """Recursively normalise a JSON value for hashing.

    Two semantically-identical cards must hash the same even when a round-trip
    through another tool changed an int to a float or an int to its string form
    (``1`` vs ``1.0`` vs ``"1"``); otherwise every ST re-save looks like a change
    on both sides and the pair is permanently reported as a conflict.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            num = float(value)
        except ValueError:
            return value
        if num.is_integer() and value.strip() == str(int(num)):
            return int(num)
        return value
    if isinstance(value, dict):
        return {str(k): _canonical_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(v) for v in value]
    return value


def compute_card_hash(card_data: dict) -> str:
    """Compute a stable content hash of a character card spec dict.

    Uses canonical JSON (sorted keys) so semantically-identical cards
    produce the same hash regardless of key ordering.  The ``spec`` and
    ``spec_version`` fields are stripped because both V2 and V3 chunks
    represent the same content — only the format marker differs.
    """
    normalized = {k: v for k, v in card_data.items() if k not in ('spec', 'spec_version')}
    canonical = json.dumps(
        _canonical_json_value(normalized), sort_keys=True,
        ensure_ascii=False, default=str, allow_nan=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


class SyncCategory(Enum):
    """Classification of a card pair during library comparison."""
    ONLY_EXPLORER = 'only_explorer'
    ONLY_ST = 'only_st'
    IN_SYNC = 'in_sync'
    ST_CHANGED = 'st_changed'
    EXPLORER_CHANGED = 'explorer_changed'
    BOTH_CHANGED = 'both_changed'
    UNLINKED_MATCH = 'unlinked_match'
    DELETED_IN_EXPLORER = 'deleted_in_explorer'


_CATEGORY_LABELS: dict[SyncCategory, str] = {
    SyncCategory.ONLY_EXPLORER: 'Only in ST Explorer',
    SyncCategory.ONLY_ST: 'Only in SillyTavern',
    SyncCategory.IN_SYNC: 'In sync',
    SyncCategory.ST_CHANGED: 'Changed in SillyTavern',
    SyncCategory.EXPLORER_CHANGED: 'Changed in ST Explorer',
    SyncCategory.BOTH_CHANGED: 'Conflict (both changed)',
    SyncCategory.UNLINKED_MATCH: 'Unlinked match',
    SyncCategory.DELETED_IN_EXPLORER: 'Deleted in ST Explorer',
}


def category_label(cat: SyncCategory) -> str:
    """Return a human-readable label for *cat*."""
    return _CATEGORY_LABELS.get(cat, cat.value)


@dataclass
class StCardEntry:
    """A character card found in the SillyTavern characters directory."""
    filename: str
    path: Path
    name: str
    creator: str
    card_hash: str
    mtime: float


@dataclass
class ExplorerSyncEntry:
    """An ST Explorer library card prepared for comparison."""
    char_id: int
    name: str
    creator: str
    source_path: str
    st_avatar_url: Optional[str]
    st_sync_hash: Optional[str]
    current_hash: str
    thumbnail_path: str = ''


@dataclass
class DeletedCardEntry:
    """A tombstone for a card deleted in ST Explorer while linked to ST.

    Recorded when a linked card is deleted so the sync process deletes the
    SillyTavern copy too (and never re-imports it).  ``st_sync_hash`` is the
    content baseline at the time of the last successful sync, used to detect
    whether the ST copy was edited after the deletion.
    """
    st_avatar_url: str
    name: str = ''
    creator: str = ''
    st_sync_hash: Optional[str] = None
    deleted_at: str = ''


@dataclass
class SyncPair:
    """A comparison result pairing explorer and/or ST cards."""
    category: SyncCategory
    explorer: Optional[ExplorerSyncEntry] = None
    st: Optional[StCardEntry] = None
    tombstone: Optional[DeletedCardEntry] = None


def list_st_characters(characters_dir: str | Path) -> list[StCardEntry]:
    """List all character cards in a SillyTavern characters directory.

    Reads every ``.png`` file and parses its embedded card data.  Files
    without valid card data are skipped (with a debug log).  Returns
    entries sorted by filename for stable display.
    """
    cdir = Path(characters_dir)
    if not cdir.is_dir():
        return []
    entries: list[StCardEntry] = []
    # iterdir + suffix compare rather than glob('*.png'): glob is
    # case-sensitive on Linux, so a '.PNG' card was invisible to both the scan
    # and collision detection (and could then be overwritten).
    for p in sorted(cdir.iterdir()):
        if not p.is_file() or p.suffix.lower() != '.png':
            continue
        if p.name.startswith('.ste-'):
            # Our own staging files from a crashed push; never a real card.
            continue
        try:
            raw = read_chara_card(p)
            if raw is None:
                logger.debug("Skipping ST file with no card data: %s", p.name)
                continue
            card = CharacterCard.from_spec_dict(raw, str(p))
            stat = p.stat()
            entries.append(StCardEntry(
                filename=p.name,
                path=p,
                name=card.name,
                creator=card.creator,
                card_hash=compute_card_hash(raw),
                mtime=stat.st_mtime,
            ))
        except Exception as e:
            logger.debug("Could not parse ST card %s: %s", p, e)
    return entries


def build_explorer_entries(db_rows: list[dict]) -> list[ExplorerSyncEntry]:
    """Build :class:`ExplorerSyncEntry` objects from DB rows, computing hashes.

    Reads each card's PNG/JSON from disk to compute the content hash.
    Entries whose source file is missing or unreadable are skipped.
    """
    entries: list[ExplorerSyncEntry] = []
    for row in db_rows:
        source = row.get('source_path', '')
        if not source or not Path(source).exists():
            logger.warning("Skipping card %s: source missing", row.get('id'))
            continue
        raw = read_card_data(source)
        if raw is None:
            logger.warning("Skipping card %s: no card data in %s", row.get('id'), source)
            continue
        entries.append(ExplorerSyncEntry(
            char_id=row['id'],
            name=row.get('name', ''),
            creator=row.get('creator', ''),
            source_path=source,
            st_avatar_url=row.get('st_avatar_url'),
            st_sync_hash=row.get('st_sync_hash'),
            current_hash=compute_card_hash(raw),
            thumbnail_path=row.get('thumbnail_path') or '',
        ))
    return entries


def _classify_linked(ex: ExplorerSyncEntry, st: StCardEntry) -> SyncCategory:
    """Classify a linked explorer/ST pair using the baseline hash."""
    baseline = ex.st_sync_hash
    if not baseline:
        if ex.current_hash == st.card_hash:
            return SyncCategory.IN_SYNC
        return SyncCategory.BOTH_CHANGED
    ex_changed = ex.current_hash != baseline
    st_changed = st.card_hash != baseline
    if not ex_changed and not st_changed:
        return SyncCategory.IN_SYNC
    if ex_changed and not st_changed:
        return SyncCategory.EXPLORER_CHANGED
    if not ex_changed and st_changed:
        return SyncCategory.ST_CHANGED
    return SyncCategory.BOTH_CHANGED


def compare_libraries(
    explorer_entries: list[ExplorerSyncEntry],
    st_entries: list[StCardEntry],
    deleted_entries: list[dict] | list[DeletedCardEntry] = (),
) -> list[SyncPair]:
    """Compare ST Explorer and SillyTavern libraries and classify each card.

    Matching priority:
    1. Linked cards (``explorer.st_avatar_url == st.filename``).
    2. Unlinked cards matched by name+creator (case-insensitive).
    3. Remaining explorer cards → ``ONLY_EXPLORER``.
    4. Remaining ST cards → ``ONLY_ST``, or ``DELETED_IN_EXPLORER`` when a
       deletion tombstone exists for the file (the card was deleted on
       purpose in ST Explorer and must not be re-imported).

    *deleted_entries* holds the deletion tombstones (DB rows or
    :class:`DeletedCardEntry` objects).  Matching against them uses
    ``os.path.normcase`` like every other filename comparison.

    For linked pairs, change detection uses the baseline hash
    (``explorer.st_sync_hash``) compared to each side's current hash.
    """
    st_by_filename: dict[str, StCardEntry] = {_fs_key(e.filename): e for e in st_entries}
    st_by_name_creator: dict[tuple[str, str], list[StCardEntry]] = {}
    for e in st_entries:
        key = (e.name.lower(), e.creator.lower())
        st_by_name_creator.setdefault(key, []).append(e)

    deleted_by_filename: dict[str, DeletedCardEntry] = {}
    for d in deleted_entries or ():
        if isinstance(d, DeletedCardEntry):
            entry = d
        else:
            entry = DeletedCardEntry(
                st_avatar_url=d.get('st_avatar_url', ''),
                name=d.get('name', ''),
                creator=d.get('creator', ''),
                st_sync_hash=d.get('st_sync_hash'),
                deleted_at=d.get('deleted_at', ''),
            )
        if entry.st_avatar_url:
            deleted_by_filename[_fs_key(entry.st_avatar_url)] = entry

    pairs: list[SyncPair] = []
    consumed_st: set[str] = set()

    for ex in explorer_entries:
        if ex.st_avatar_url:
            st = st_by_filename.get(_fs_key(ex.st_avatar_url))
            if st is None:
                pairs.append(SyncPair(category=SyncCategory.ONLY_EXPLORER, explorer=ex))
                continue
            consumed_st.add(_fs_key(st.filename))
            pairs.append(SyncPair(
                category=_classify_linked(ex, st),
                explorer=ex,
                st=st,
            ))

    unmatched_explorer = [ex for ex in explorer_entries if not ex.st_avatar_url]
    for ex in unmatched_explorer:
        key = (ex.name.lower(), ex.creator.lower())
        candidates = st_by_name_creator.get(key, [])
        st_match = None
        for st in candidates:
            if _fs_key(st.filename) not in consumed_st:
                st_match = st
                break
        if st_match is not None:
            consumed_st.add(_fs_key(st_match.filename))
            if ex.current_hash == st_match.card_hash:
                cat = SyncCategory.IN_SYNC
            else:
                cat = SyncCategory.UNLINKED_MATCH
            pairs.append(SyncPair(category=cat, explorer=ex, st=st_match))
        else:
            pairs.append(SyncPair(category=SyncCategory.ONLY_EXPLORER, explorer=ex))

    for st in st_entries:
        key = _fs_key(st.filename)
        if key in consumed_st:
            continue
        tombstone = deleted_by_filename.get(key)
        if tombstone is not None:
            # A tombstone exists for this file: the card was deleted on
            # purpose in ST Explorer.  Live explorer links/unlinked matches
            # always win over tombstones (they consumed the file above), so
            # reaching this point means no card claims the file any more.
            pairs.append(SyncPair(
                category=SyncCategory.DELETED_IN_EXPLORER,
                st=st,
                tombstone=tombstone,
            ))
        else:
            pairs.append(SyncPair(category=SyncCategory.ONLY_ST, st=st))

    return pairs


def summarize_pairs(pairs: list[SyncPair]) -> dict[str, int]:
    """Count pairs by category. Returns ``{category_value: count}``."""
    counts: dict[str, int] = {}
    for p in pairs:
        key = p.category.value
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Push / Pull / Bulk sync
# ---------------------------------------------------------------------------

class SyncAction(Enum):
    """Action to perform on a sync pair."""
    PULL = 'pull'
    PUSH = 'push'
    LINK = 'link'
    SKIP = 'skip'
    DELETE_ST = 'delete_st'


@dataclass
class SyncPlanItem:
    """A single item in a sync plan: a pair + the action to take.

    ``confirmed`` marks items whose destructive action the user explicitly
    approved (e.g. the sync dialog's "Delete in ST" button). Unconfirmed
    deletions re-verify the file content at execution time, because a plan
    scanned minutes earlier can go stale before it runs.
    """
    pair: SyncPair
    action: SyncAction
    confirmed: bool = False


@dataclass
class PushResult:
    """Outcome of pushing a single card to ST."""
    char_id: int
    avatar_url: Optional[str] = None
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.avatar_url is not None


@dataclass
class PullResult:
    """Outcome of pulling a single card from ST."""
    avatar_url: str
    char_id: Optional[int] = None
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.char_id is not None


@dataclass
class SyncSummary:
    """Accumulates the results of a bulk sync. Pure logic (no Qt)."""
    pulled: int = 0
    pushed: int = 0
    linked: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def message(self) -> str:
        parts: list[str] = []
        if self.pulled:
            parts.append(f"Pulled {self.pulled} card(s)")
        if self.pushed:
            parts.append(f"Pushed {self.pushed} card(s)")
        if self.linked:
            parts.append(f"Linked {self.linked} card(s)")
        if self.deleted:
            parts.append(f"Deleted {self.deleted} card(s)")
        if self.skipped:
            parts.append(f"Skipped {self.skipped}")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return '. '.join(parts) + '.' if parts else 'Nothing to sync.'


def build_sync_plan(pairs: list[SyncPair]) -> list[SyncPlanItem]:
    """Build a default sync plan from comparison results.

    Defaults:
    - ``ONLY_ST`` / ``ST_CHANGED`` → PULL
    - ``ONLY_EXPLORER`` / ``EXPLORER_CHANGED`` → PUSH
    - ``BOTH_CHANGED`` → SKIP (user must resolve)
    - ``UNLINKED_MATCH`` → SKIP (user must decide direction)
    - ``IN_SYNC`` (linked) → SKIP
    - ``IN_SYNC`` (unlinked match) → LINK
    - ``DELETED_IN_EXPLORER`` → DELETE_ST when the ST copy is unchanged
      since the last sync (the deletion propagates); SKIP when the ST copy
      was edited after the deletion — surfaced for a manual decision so
      those edits are not silently destroyed.
    """
    plan: list[SyncPlanItem] = []
    for pair in pairs:
        cat = pair.category
        if cat in (SyncCategory.ONLY_ST, SyncCategory.ST_CHANGED):
            plan.append(SyncPlanItem(pair, SyncAction.PULL))
        elif cat in (SyncCategory.ONLY_EXPLORER, SyncCategory.EXPLORER_CHANGED):
            plan.append(SyncPlanItem(pair, SyncAction.PUSH))
        elif cat == SyncCategory.BOTH_CHANGED:
            plan.append(SyncPlanItem(pair, SyncAction.SKIP))
        elif cat == SyncCategory.UNLINKED_MATCH:
            plan.append(SyncPlanItem(pair, SyncAction.SKIP))
        elif cat == SyncCategory.DELETED_IN_EXPLORER:
            plan.append(SyncPlanItem(pair, _deleted_pair_action(pair)))
        elif cat == SyncCategory.IN_SYNC:
            if pair.explorer and pair.explorer.st_avatar_url:
                plan.append(SyncPlanItem(pair, SyncAction.SKIP))
            elif pair.explorer and pair.st:
                plan.append(SyncPlanItem(pair, SyncAction.LINK))
            else:
                plan.append(SyncPlanItem(pair, SyncAction.SKIP))
    return plan


def _deleted_pair_action(pair: SyncPair) -> SyncAction:
    """Choose DELETE_ST vs SKIP for a tombstoned (deleted-in-Explorer) pair.

    Deleting is safe when the ST copy still matches the content baseline
    recorded at the last sync — nothing exists on the ST side that the
    Explorer library ever saw.  If the ST copy changed afterwards, the
    pair is left for the user to resolve (e.g. via the sync dialog).

    With no baseline the safety check is impossible, so the previous
    unconditional DELETE_ST destroyed ST edits of a card that was linked but
    never successfully synced. Surface it as a conflict instead.
    """
    tombstone = pair.tombstone
    if pair.st is None:
        return SyncAction.SKIP
    if tombstone is None:
        return SyncAction.SKIP
    if not tombstone.st_sync_hash:
        logger.info(
            "No sync baseline for deleted card '%s'; not deleting the ST copy",
            pair.st.filename,
        )
        return SyncAction.SKIP
    if pair.st.card_hash == tombstone.st_sync_hash:
        return SyncAction.DELETE_ST
    return SyncAction.SKIP


def build_push_all_plan(pairs: list[SyncPair]) -> list[SyncPlanItem]:
    """Build a plan that pushes all new/changed Explorer cards to ST.

    Non-destructive by design: cards that exist only on the ST side may
    simply not have been imported yet, so they are never deleted here.
    (Use the sync dialog's per-card "Delete in ST" action for that, or
    Sync All, which propagates deliberate Explorer deletions via
    tombstones.)

    - ``ONLY_EXPLORER`` → PUSH
    - ``EXPLORER_CHANGED`` → PUSH (overwrite ST with the newer Explorer copy)
    - Everything else → SKIP (including ``DELETED_IN_EXPLORER`` — deletion
      is not a push concern)
    """
    plan: list[SyncPlanItem] = []
    for pair in pairs:
        cat = pair.category
        if cat in (SyncCategory.ONLY_EXPLORER, SyncCategory.EXPLORER_CHANGED):
            plan.append(SyncPlanItem(pair, SyncAction.PUSH))
        else:
            plan.append(SyncPlanItem(pair, SyncAction.SKIP))
    return plan


def build_pull_all_plan(pairs: list[SyncPair]) -> list[SyncPlanItem]:
    """Build a plan that pulls all new/changed ST cards into Explorer.

    Non-destructive by design:

    - ``ONLY_ST`` → PULL (import new cards)
    - ``ST_CHANGED`` → PULL (ST changed and Explorer did not — safe overwrite)
    - ``EXPLORER_CHANGED`` → SKIP (the Explorer copy has local edits; pulling
      would silently destroy them — resolve these individually)
    - ``DELETED_IN_EXPLORER`` → SKIP (the card was deleted on purpose in
      Explorer, so it must never be copied back; Sync All deletes the ST
      copy instead)
    - Everything else → SKIP
    """
    plan: list[SyncPlanItem] = []
    for pair in pairs:
        cat = pair.category
        if cat in (SyncCategory.ONLY_ST, SyncCategory.ST_CHANGED):
            plan.append(SyncPlanItem(pair, SyncAction.PULL))
        else:
            plan.append(SyncPlanItem(pair, SyncAction.SKIP))
    return plan


def _sweep_staging_files(cdir: Path, max_age_seconds: float = 3600.0) -> None:
    """Delete our own stale ``.ste-*.tmp`` staging files from *cdir*.

    A crash or hard kill skips the ``finally`` cleanup.  Only files older than
    *max_age_seconds* are removed, so a concurrent push's staging file is never
    deleted out from under it.
    """
    cutoff = time.time() - max_age_seconds
    try:
        stale = list(cdir.glob('.ste-*.tmp'))
    except OSError:
        return
    for path in stale:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                logger.info("Removed stale sync staging file %s", path)
        except OSError:
            pass


def _write_placeholder_png(dest: Path, thumb_path: str = '') -> str:
    """Write a usable base PNG at *dest* and return its path.

    Used when a card has no PNG on disk (e.g. it was imported from JSON).
    Without this the push wrote a blank 400x600 grey rectangle, so such a card
    lost its avatar in SillyTavern permanently.  The cached thumbnail is used
    when available so the pushed card still shows the character; otherwise a
    neutral placeholder is drawn.
    """
    from PIL import Image, ImageDraw
    img = None
    if thumb_path:
        try:
            from src import vault
            img = vault.open_image(thumb_path).convert('RGBA')
        except Exception:
            img = None
    if img is None:
        img = Image.new('RGBA', (400, 600), (60, 60, 60, 255))
        try:
            draw = ImageDraw.Draw(img)
            bbox = draw.textbbox((0, 0), 'No image')
            draw.text(
                ((img.width - bbox[2]) // 2, (img.height - bbox[3]) // 2),
                'No image', fill=(200, 200, 200, 255),
            )
        except (AttributeError, OSError):
            pass   # a font backend without textbbox; the flat fill is fine
    else:
        resample = getattr(Image, 'LANCZOS', Image.BICUBIC)
        img = img.resize((400, 600), resample)
    # Vault policy applies: staging temps outside the data dir stay plaintext
    # (SillyTavern must be able to read them), library copies get sealed.
    from src import vault
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    vault.write_bytes(dest, buf.getvalue())
    return str(dest)


def push_card_to_st(
    explorer_row: dict,
    card_data: dict,
    characters_dir: str | Path,
    existing_filenames: set[str],
    linked_name: Optional[str] = None,
) -> PushResult:
    """Push an ST Explorer card to the SillyTavern characters directory.

    Writes the card as a PNG (with both V2 and V3 tEXt chunks) using the
    explorer card's image as the base.  Uses *linked_name* to overwrite an
    existing linked file, or resolves a unique filename for new pushes.
    Uses an atomic write (temp file + ``os.replace``) for safety.
    """
    char_id = explorer_row['id']
    source_path = explorer_row.get('source_path', '')

    card = CharacterCard.from_spec_dict(card_data, source_path)
    base_name = st_filename_for(card)
    target_name = resolve_st_filename(base_name, existing_filenames, linked_name)
    cdir = Path(characters_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    target_path = cdir / target_name
    # Clear staging files left behind by a hard kill (a crash skips the
    # finally-cleanup) so the directory doesn't accumulate debris.
    _sweep_staging_files(cdir)
    # The staging file lives in the same directory so os.replace stays atomic,
    # but uses a .tmp suffix: a .png there is a real card as far as
    # SillyTavern is concerned, so a hard kill mid-push used to leave a phantom
    # character in the library.
    temp_target = unique_temp_path(cdir, '.ste-push-', '.tmp')
    temp_source: Optional[Path] = None

    try:
        if source_path and Path(source_path).suffix.lower() == '.png' and Path(source_path).exists():
            source_png = source_path
        else:
            # No PNG to use as the image base: fall back to the cached
            # thumbnail, or a placeholder card. A bare grey rectangle here
            # permanently lost the avatar of every JSON-imported card.
            temp_source = unique_temp_path(cdir, '.ste-src-', '.tmp')
            source_png = _write_placeholder_png(
                temp_source, explorer_row.get('thumbnail_path', ''))

        write_chara_card_dual(source_png, temp_target, card_data)
        atomic_replace(temp_target, target_path)

        return PushResult(char_id=char_id, avatar_url=target_name)
    except Exception as e:
        logger.exception("Push failed for card %s", char_id)
        return PushResult(char_id=char_id, avatar_url=None, error=str(e))
    finally:
        if temp_source is not None:
            try:
                temp_source.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            if temp_target.exists():
                temp_target.unlink()
        except OSError:
            pass


def pull_card_from_st(st_entry: StCardEntry, db) -> PullResult:
    """Pull a card from the SillyTavern directory into ST Explorer.

    For linked cards: copies the ST PNG over the explorer source file
    atomically (avoiding re-serialization so the content hash baseline
    stays valid), regenerates the thumbnail, and refreshes DB fields.
    For unlinked cards: imports as a new card, links it, and marks synced.
    The baseline hash recorded is computed from the freshly read card data,
    not from the scan-time snapshot.
    """
    try:
        raw = read_chara_card(st_entry.path)
        if raw is None:
            return PullResult(st_entry.filename, None, 'No card data in ST file')
        fresh_hash = compute_card_hash(raw)

        card = CharacterCard.from_spec_dict(raw, str(st_entry.path))
        card.token_count = count_card_tokens(card)

        existing = db.find_by_st_avatar_url(st_entry.filename)
        if existing is None:
            existing = _find_by_st_avatar_url_nocase(db, st_entry.filename)
        if existing:
            char_id = existing['id']
            source = existing.get('source_path', '')
            if source and Path(source).suffix.lower() == '.png' and Path(source).exists():
                # Atomic vault-aware copy: never leaves a truncated PNG at the
                # library path, and seals the pulled copy when encryption is on.
                from src import vault
                vault.import_external(str(st_entry.path), source)
                db.update_card(char_id, card, regen_thumbnail=True, skip_file_write=True)
            else:
                # Import the replacement FIRST; only remove the old row once
                # the import has succeeded, so a failure can't destroy the
                # original card.
                new_id = db.import_card(str(st_entry.path))
                if not new_id:
                    return PullResult(st_entry.filename, None, 'Re-import failed')
                # Carry over the Explorer-local metadata the fresh import
                # cannot know about (ratings, private notes, collections are
                # stored in the DB only — never in the PNG).
                try:
                    if existing.get('rating'):
                        db.set_rating(new_id, int(existing['rating']))
                    if existing.get('user_notes'):
                        db.set_user_notes(new_id, str(existing['user_notes']))
                    collection_ids = db.get_card_collection_ids(char_id)
                    if collection_ids:
                        db.set_card_collections(new_id, collection_ids)
                except Exception:
                    logger.warning(
                        "Could not migrate metadata from superseded card %s", char_id,
                    )
                try:
                    # The chat history belongs to the character, not the row:
                    # move it to the new id before the old row goes away.
                    migrate = getattr(db, 'migrate_chat_sessions', None)
                    if migrate is not None:
                        migrate(char_id, new_id)
                except Exception:
                    logger.warning(
                        "Could not migrate chat sessions from superseded card %s", char_id,
                    )
                try:
                    db.remove_card(
                        char_id,
                        delete_files=False,
                        record_tombstone=False,
                        delete_sessions=False,
                    )
                except Exception:
                    logger.warning("Could not remove superseded card %s", char_id)
                char_id = new_id
                db.link_to_st(char_id, st_entry.filename)
            db.mark_st_synced(char_id, fresh_hash)
            return PullResult(st_entry.filename, char_id)
        else:
            char_id = db.import_card(str(st_entry.path))
            if char_id:
                db.link_to_st(char_id, st_entry.filename)
                db.mark_st_synced(char_id, fresh_hash)
                return PullResult(st_entry.filename, char_id)
            return PullResult(st_entry.filename, None, 'Import failed')
    except Exception as e:
        logger.exception("Pull failed for %s", st_entry.filename)
        return PullResult(st_entry.filename, None, str(e))


def _find_by_st_avatar_url_nocase(db, filename: str):
    """Case-insensitive fallback lookup for an ST link.

    The DB lookup is byte-exact, so a case-only rename on the ST side would
    otherwise classify as a pull with no matching link.  ``_fs_key`` is used
    rather than ``os.path.normcase`` so this also works on macOS, whose default
    volume is case-insensitive.
    """
    try:
        rows = db.get_all()
    except AttributeError:
        return None
    wanted = _fs_key(filename)
    for row in rows:
        url = row.get('st_avatar_url')
        if url and _fs_key(url) == wanted:
            return row
    return None


def link_pair(pair: SyncPair, db) -> bool:
    """Link an unlinked explorer/ST pair. Returns True on success."""
    if pair.explorer is None or pair.st is None:
        return False
    db.link_to_st(pair.explorer.char_id, pair.st.filename)
    if pair.explorer.current_hash == pair.st.card_hash:
        db.mark_st_synced(pair.explorer.char_id, pair.st.card_hash)
    return True


def bulk_sync(
    plan: list[SyncPlanItem],
    db,
    characters_dir: str | Path,
    is_cancelled=None,
    on_progress=None,
) -> SyncSummary:
    """Execute a sync plan. Pure logic (no Qt) for testability.

    *db* must expose: ``find_by_st_avatar_url``, ``import_card``,
    ``update_card``, ``remove_card``, ``link_to_st``, ``mark_st_synced``
    (plus ``clear_deleted_st_card`` for DELETE_ST items).
    *is_cancelled* is a callable returning True to stop (cooperative cancel).
    *on_progress(current, total, name)* is an optional progress callback.

    Returns the accumulated :class:`SyncSummary`.
    """
    summary = SyncSummary()
    total = len(plan)
    # Collision resolution must consider EVERY png in the directory —
    # including files with no parseable card data (plain images ST keeps
    # there). On case-insensitive filesystems a name that only matches an
    # unparsed file would silently overwrite it. ``glob('*.png')`` is not
    # enough: its matching is case-sensitive even on macOS's default
    # case-insensitive volume, so ``Foo.PNG`` was invisible and ``Foo.png``
    # was handed out over it.
    cdir = Path(characters_dir)
    existing_st = (
        {
            p.name for p in cdir.iterdir()
            if p.is_file() and p.suffix.lower() == '.png'
        }
        if cdir.is_dir() else set()
    )

    for i, item in enumerate(plan):
        if is_cancelled is not None and is_cancelled():
            break
        if on_progress is not None:
            name = (
                item.pair.explorer.name if item.pair.explorer
                else (item.pair.st.name if item.pair.st else '?')
            )
            on_progress(i + 1, total, name)

        action = item.action
        pair = item.pair

        if action == SyncAction.SKIP:
            summary.skipped += 1
            continue

        if action == SyncAction.PULL:
            if pair.st is None:
                summary.errors.append("Cannot pull: no ST card")
                continue
            result = pull_card_from_st(pair.st, db)
            if result.succeeded:
                summary.pulled += 1
            else:
                summary.errors.append(f"{pair.st.filename}: {result.error}")

        elif action == SyncAction.PUSH:
            if pair.explorer is None:
                summary.errors.append("Cannot push: no explorer card")
                continue
            source = pair.explorer.source_path
            raw = read_card_data(source)
            if raw is None:
                summary.errors.append(f"{pair.explorer.name}: no card data")
                continue
            result = push_card_to_st(
                {
                    'id': pair.explorer.char_id,
                    'source_path': source,
                    # Used as the image base for JSON-imported cards.
                    'thumbnail_path': pair.explorer.thumbnail_path,
                },
                raw,
                characters_dir,
                existing_st,
                linked_name=pair.explorer.st_avatar_url,
            )
            if result.succeeded:
                existing_st.add(result.avatar_url)
                db.link_to_st(pair.explorer.char_id, result.avatar_url)
                db.mark_st_synced(pair.explorer.char_id, compute_card_hash(raw))
                summary.pushed += 1
            else:
                summary.errors.append(f"{pair.explorer.name}: {result.error}")

        elif action == SyncAction.LINK:
            if link_pair(pair, db):
                summary.linked += 1
            else:
                summary.errors.append("Cannot link: missing pair")

        elif action == SyncAction.DELETE_ST:
            if pair.st is None:
                summary.errors.append("Cannot delete: no ST card")
                continue
            try:
                st_path = Path(pair.st.path)
                if st_path.exists():
                    # An unconfirmed (bulk-plan) delete runs on a hash taken
                    # at scan time. If the ST file changed since, re-verify it
                    # against the sync baseline before destroying anything.
                    tombstone = pair.tombstone
                    baseline = getattr(tombstone, 'st_sync_hash', None) if tombstone else None
                    if not item.confirmed and baseline:
                        fresh = read_card_data(st_path)
                        if fresh is None or compute_card_hash(fresh) != baseline:
                            summary.errors.append(
                                f"{pair.st.filename}: ST file changed since the last "
                                "sync; not deleted (delete it from the SillyTavern "
                                "folder directly if that is intended)"
                            )
                            continue
                    st_path.unlink()
                    logger.info("Deleted ST file: %s", st_path)
                # Unlink any explorer card that was linked to this file
                existing = db.find_by_st_avatar_url(pair.st.filename)
                if existing:
                    db.unlink_from_st(existing['id'])
                # The deletion intent (tombstone) has been fulfilled.
                clear_tombstone = getattr(db, 'clear_deleted_st_card', None)
                if clear_tombstone is not None:
                    clear_tombstone(pair.st.filename)
                summary.deleted += 1
            except Exception as e:
                summary.errors.append(f"{pair.st.filename}: delete failed: {e}")

    return summary


def detect_st_installs() -> list[Path]:
    """Auto-detect SillyTavern character directories on this machine.

    Scans common install locations for a ``data/default-user/characters``
    subdirectory.  Returns candidates (most likely first).  The user can
    always browse manually if detection fails.
    """
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _check(p: Path) -> None:
        chars = p / 'data' / 'default-user' / 'characters'
        try:
            if chars.is_dir() and chars not in seen:
                seen.add(chars)
                candidates.append(chars)
        except OSError:
            pass

    home = Path.home()
    _check(home / 'SillyTavern')
    _check(home / 'Documents' / 'SillyTavern')
    _check(home / 'Documents' / 'GitHub' / 'SillyTavern')

    if os.name == 'nt':
        for drive_letter in 'CDEFGH':
            _check(Path(f'{drive_letter}:') / 'SillyTavern')
    elif sys.platform == 'darwin':
        # Typical mac install locations (git clone into /Applications or
        # the user's Applications folder).
        _check(Path('/Applications') / 'SillyTavern')
        _check(home / 'Applications' / 'SillyTavern')
    else:
        _check(Path('/opt/SillyTavern'))
        _check(Path('/srv/SillyTavern'))
        _check(home / '.local' / 'share' / 'SillyTavern')

    _check(home.parent / 'SillyTavern')

    return candidates

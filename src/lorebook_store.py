"""Standalone lorebook (world info) storage.

Lorebooks live as JSON files under ``~/.st-explorer/lorebooks/`` using the
V2 character-book schema (the same structure embedded in card
``character_book`` fields), so a book can be moved between a card and the
standalone library losslessly.

Import additionally accepts SillyTavern's native world-info format
(``entries`` keyed by numeric uid, ``key``/``comment``/``order`` fields),
NovelAI Lorebook exports, and character-card files with an embedded
``character_book``; unknown ST entry fields are preserved inside each
BookEntry's ``extensions`` so an import -> export round-trip keeps them.

Pure filesystem/dict logic: no Qt imports.
"""
from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from src.card_models import BookEntry, CharacterBook
from src import vault

logger = logging.getLogger(__name__)


def _base_data_dir() -> Path:
    """Root app-data dir; ``ST_EXPLORER_HOME`` overrides for tests/portability."""
    from src.app_paths import data_dir
    return data_dir()


def get_lorebooks_dir() -> Path:
    d = _base_data_dir() / 'lorebooks'
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_book_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '_', name or '')
    name = name.strip('. ')
    return name[:80] if name else 'untitled'


def unique_filename(name: str) -> str:
    """Return a non-colliding ``<sanitized>.json`` filename for *name*."""
    base = sanitize_book_filename(name)
    candidate = f'{base}.json'
    n = 1
    directory = get_lorebooks_dir()
    while (directory / candidate).exists():
        candidate = f'{base}_{n}.json'
        n += 1
    return candidate


def list_lorebooks() -> list[dict]:
    """Return metadata dicts for every stored lorebook, sorted by name.

    Each dict has ``name``, ``filename``, ``entries`` (count) and ``path``.
    Corrupted files are skipped with a warning rather than breaking listing.
    """
    result: list[dict] = []
    directory = get_lorebooks_dir()
    for path in sorted(directory.glob('*.json'), key=lambda p: p.name.lower()):
        try:
            raw = json.loads(vault.read_text(path))
            book = parse_book_json(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError,
                TypeError, vault.VaultError) as exc:
            logger.warning("Skipping unreadable lorebook %s: %s", path.name, exc)
            continue
        result.append({
            'name': book.name or path.stem,
            'filename': path.name,
            'entries': len(book.entries),
            'path': str(path),
        })
    return result


def load_lorebook(filename: str) -> CharacterBook | None:
    """Load a lorebook by filename, returning None when missing/corrupted."""
    if not filename:
        return None
    path = get_lorebooks_dir() / Path(filename).name
    if not path.exists():
        return None
    try:
        raw = json.loads(vault.read_text(path))
        return parse_book_json(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError,
            TypeError, vault.VaultError) as exc:
        logger.warning("Failed to load lorebook %s: %s", filename, exc)
        return None


def save_lorebook(filename: str, book: CharacterBook) -> str:
    """Persist *book* under *filename* (atomic write). Returns the filename."""
    safe = Path(filename).name
    if not safe.endswith('.json'):
        safe += '.json'
    data = json.dumps(book.to_dict(), ensure_ascii=False, indent=2)
    vault.write_bytes(get_lorebooks_dir() / safe, data.encode('utf-8'))
    return safe


def delete_lorebook(filename: str) -> bool:
    """Delete a lorebook file.

    Also drops its sync baseline: leaving it behind grew the state file without
    bound, and a book later recreated under the same filename would inherit a
    stale baseline and show as permanently changed.
    """
    path = get_lorebooks_dir() / Path(filename).name
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to delete lorebook %s: %s", filename, exc)
        return False
    try:
        from src.lorebook_sync import forget_pair
        forget_pair(filename)
    except Exception:
        logger.debug("Could not clear the sync baseline for %s", filename,
                     exc_info=True)
    return True


# ---------------------------------------------------------------------------
# Format conversion
# ---------------------------------------------------------------------------

# SillyTavern's native ``position`` values. 0/1 are the classic before/after
# character placements; 2/3/4 are the extended ones and MUST round-trip, or a
# pull-then-push silently relocates every entry in the book.
_ST_POSITION_TO_SPEC = {
    0: 'before_char',
    1: 'after_char',
    2: 'before_EM',
    3: 'after_EM',
    4: 'at_depth',
}
_SPEC_POSITION_TO_ST = {
    'before_char': 0,
    'after_char': 1,
    'before_em': 2,
    'after_em': 3,
    'at_depth': 4,
}
# Canonical (lowercase) spelling -> stored spelling, used to normalise the
# several equivalent spellings a card or an ST export can carry.
_POSITION_CANONICAL = {
    'before_char': 'before_char',
    'after_char': 'after_char',
    'before_em': 'before_EM',
    'after_em': 'after_EM',
    'at_depth': 'at_depth',
}


def _canonical_position(position: str) -> str:
    """Normalise a stored position string to its canonical spec spelling.

    Comparison is case-insensitive and tolerant of separators so that values
    written by older versions (or by ST, which spells them 'before_EM') all
    map onto the same canonical form.  Unknown values are passed through
    unchanged rather than silently rewritten.
    """
    p = (position or '').strip()
    if not p:
        return 'before_char'
    key = p.lower().replace('-', '_').replace(' ', '_')
    return _POSITION_CANONICAL.get(key, p)


def _entry_items(entries) -> list[dict]:
    """Normalize a book's ``entries`` value into a list of dicts.

    Accepts a list of entry dicts or a mapping keyed by uid (SillyTavern's
    native shape); anything else yields an empty list.
    """
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)]
    if isinstance(entries, dict):
        def _key_order(item: tuple) -> tuple:
            k = str(item[0])
            try:
                return (0, float(k), '')
            except ValueError:
                return (1, 0.0, k)
        return [v for _, v in sorted(entries.items(), key=_key_order)
                if isinstance(v, dict)]
    return []


def _safe_int(val: Any, default: int = 0) -> int:
    """Coerce *val* to int without raising.

    Hand-edited or corrupt world-info files can carry ``"order": "abc"`` or
    ``NaN``/``Infinity`` (which ``json.loads`` accepts); one bad entry must
    not abort parsing of the whole book.
    """
    if isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        try:
            return int(val)
        except (ValueError, OverflowError):
            return default
    if isinstance(val, str):
        try:
            return int(float(val.strip()))
        except (ValueError, OverflowError):
            return default
    return default


def _opt_book_int(val: Any) -> int | None:
    """Like ``_safe_int`` but returns None for absent or non-coercible values.

    A corrupt ``"scan_depth": "auto"`` must round-trip as absent rather than
    being silently rewritten to ``0``.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val) if math.isfinite(val) else None
    if isinstance(val, str):
        try:
            return int(float(val.strip()))
        except (ValueError, OverflowError):
            return None
    return None


def _st_entry_to_book_entry(raw: dict) -> BookEntry:
    """Convert one SillyTavern world-info entry into a :class:`BookEntry`.

    Recognized ST fields map onto modeled BookEntry attributes; every other
    key is preserved verbatim inside ``extensions`` so nothing is lost.
    """
    known = {
        'uid', 'key', 'keysecondary', 'comment', 'content', 'constant',
        'disable', 'order', 'position', 'depth', 'caseSensitive',
        'matchWholeWords', 'role',
    }
    extensions = {k: v for k, v in raw.items() if k not in known}

    keys = [str(k) for k in (raw.get('key') or []) if str(k).strip()]
    secondary = [str(k) for k in (raw.get('keysecondary') or []) if str(k).strip()]
    if secondary:
        extensions['keysecondary'] = secondary

    position_raw = raw.get('position')
    if isinstance(position_raw, bool):
        position_raw = int(position_raw)
    if isinstance(position_raw, (int, float)):
        position = _ST_POSITION_TO_SPEC.get(_safe_int(position_raw, 0), 'before_char')
    elif isinstance(position_raw, str) and position_raw.strip():
        position = _canonical_position(position_raw)
    else:
        position = 'before_char'

    depth = _safe_int(raw.get('depth'), 4)

    uid = raw.get('uid')
    if isinstance(uid, bool):
        pass
    elif isinstance(uid, int):
        extensions['uid'] = uid
    elif isinstance(uid, float) and math.isfinite(uid):
        extensions['uid'] = int(uid)

    return BookEntry(
        name=str(raw.get('comment') or ''),
        keys=keys,
        content=str(raw.get('content') or ''),
        extensions=extensions,
        enabled=not bool(raw.get('disable', False)),
        insertion_order=_safe_int(raw.get('order'), 0),
        case_sensitive=bool(raw.get('caseSensitive', False)),
        match_whole_words=bool(raw.get('matchWholeWords', False)),
        depth=depth,
        position=position,
        # ST roles: 0 = system, 1 = user, 2 = assistant.
        role=_safe_int(raw.get('role'), 0),
    )


def st_world_info_to_book(raw: dict) -> CharacterBook:
    """Convert a SillyTavern world-info JSON dict into a :class:`CharacterBook`."""
    if not isinstance(raw, dict):
        return CharacterBook()
    items = _entry_items(raw.get('entries'))
    entries: list[BookEntry] = []
    for item in items:
        if isinstance(item, dict):
            entries.append(_st_entry_to_book_entry(item))
    # Keep ST's uid order when present so exports round-trip stably.
    def _uid_key(entry: BookEntry) -> tuple[int, int]:
        uid = entry.extensions.get('uid')
        return (0, int(uid)) if isinstance(uid, int) else (1, 0)
    entries.sort(key=_uid_key)
    # Preserve book-level fields the model doesn't know about (and any
    # ``extensions`` dict) so a pull -> edit -> push round-trip never strips
    # keys from the user's ST world file.
    known = {
        'name', 'description', 'scan_depth', 'token_budget',
        'recursive_scanning', 'extensions', 'entries',
    }
    ext = raw.get('extensions')
    extensions = dict(ext) if isinstance(ext, dict) else {}
    extra = {k: v for k, v in raw.items() if k not in known}
    return CharacterBook(
        name=str(raw.get('name') or ''),
        description=str(raw.get('description') or ''),
        scan_depth=_opt_book_int(raw.get('scan_depth')),
        token_budget=_opt_book_int(raw.get('token_budget')),
        recursive_scanning=bool(raw.get('recursive_scanning', False)),
        extensions=extensions,
        entries=entries,
        extra_data=extra,
    )


def book_to_st_world_info(book: CharacterBook) -> dict:
    """Serialize *book* into SillyTavern's native world-info JSON shape."""
    entries_out: dict[str, dict] = {}
    # Pass 1: resolve the explicit uid of every entry. A uid is required (ST
    # keys its entry map by it), so entries the Explorer created carry none.
    # Falling back to the *list index* would collide with a real ST uid the
    # moment the user inserts or reorders an entry, and because entries_out is
    # a dict the colliding entry would silently overwrite (and lose) another.
    resolved: list[tuple[BookEntry, dict, int | None]] = []
    claimed: set[int] = set()
    for entry in book.entries:
        ext = dict(entry.extensions)
        raw_uid = ext.pop('uid', None)
        uid: int | None = None
        if isinstance(raw_uid, bool):
            uid = None
        elif isinstance(raw_uid, int):
            uid = raw_uid
        elif isinstance(raw_uid, float) and raw_uid.is_integer():
            uid = int(raw_uid)
        elif isinstance(raw_uid, str) and raw_uid.strip().lstrip('-').isdigit():
            uid = int(raw_uid.strip())
        if uid is not None:
            claimed.add(uid)
        resolved.append((entry, ext, uid))

    # Pass 2: keep explicit uids in order, allocate the lowest free uid to
    # everything else.
    used: set[int] = set()
    spare = 0
    final: list[tuple[BookEntry, dict, int]] = []
    for entry, ext, uid in resolved:
        if uid is None or uid in used:
            if uid is not None:
                logger.warning(
                    "Duplicate lorebook entry uid %s in book %r; reassigning",
                    uid, book.name)
            while spare in claimed or spare in used:
                spare += 1
            uid = spare
        used.add(uid)
        final.append((entry, ext, uid))

    for entry, ext, uid in final:
        secondary = ext.pop('keysecondary', [])
        out: dict = {
            'uid': uid,
            'key': [str(k) for k in entry.keys],
            'keysecondary': [str(k) for k in secondary],
            'comment': entry.name,
            'content': entry.content,
            'constant': bool(ext.pop('constant', False)),
            'selective': True,
            'order': entry.insertion_order,
            'position': _SPEC_POSITION_TO_ST.get(
                _canonical_position(entry.position).lower(), 0),
            'disable': not entry.enabled,
            'excludeRecursion': bool(ext.pop('excludeRecursion', False)),
            'preventRecursion': bool(ext.pop('preventRecursion', False)),
            'probability': ext.pop('probability', 100),
            'useProbability': True,
            'depth': entry.depth,
            'caseSensitive': entry.case_sensitive,
            'matchWholeWords': entry.match_whole_words,
            'role': int(entry.role or 0),
            'addMemo': True,
            'group': '',
        }
        ext.pop('role', None)
        out.update(ext)
        entries_out[str(uid)] = out
    data: dict = dict(book.extra_data)
    data.update({'entries': entries_out})
    if book.name:
        data['name'] = book.name
    if book.description:
        data['description'] = book.description
    if book.scan_depth is not None:
        data['scan_depth'] = book.scan_depth
    if book.token_budget is not None:
        data['token_budget'] = book.token_budget
    if book.recursive_scanning:
        data['recursive_scanning'] = book.recursive_scanning
    if book.extensions:
        data['extensions'] = dict(book.extensions)
    return data


def looks_like_st_world_info(raw: dict) -> bool:
    """True when *raw* uses ST's world-info shape rather than the V2 spec."""
    items = _entry_items(raw.get('entries') if isinstance(raw, dict) else None)
    if not items:
        return False
    first = items[0]
    return 'keys' not in first and any(
        marker in first
        for marker in ('key', 'keysecondary', 'comment', 'disable', 'uid')
    )


def looks_like_nai_lorebook(raw: dict) -> bool:
    """True when *raw* is a NovelAI Lorebook export (``lorebookVersion``)."""
    if not isinstance(raw, dict) or 'lorebookVersion' not in raw:
        return False
    items = _entry_items(raw.get('entries'))
    return bool(items) and 'keys' in items[0]


def _nai_entry_to_book_entry(raw: dict, index: int) -> BookEntry:
    known = {'text', 'displayName', 'keys', 'enabled', 'id', 'displayIndex'}
    extensions = {k: v for k, v in raw.items() if k not in known}
    order_raw = raw.get('displayIndex')
    order = (order_raw if isinstance(order_raw, int)
             and not isinstance(order_raw, bool) else index * 100)
    return BookEntry(
        name=str(raw.get('displayName') or ''),
        keys=[str(k) for k in (raw.get('keys') or []) if str(k).strip()],
        content=str(raw.get('text') or ''),
        extensions=extensions,
        enabled=bool(raw.get('enabled', True)),
        insertion_order=order,
        position='before_char',
    )


def nai_lorebook_to_book(raw: dict) -> CharacterBook:
    """Convert a NovelAI Lorebook export into a :class:`CharacterBook`."""
    entries = [
        _nai_entry_to_book_entry(item, i)
        for i, item in enumerate(_entry_items(raw.get('entries')))
    ]
    return CharacterBook(
        name=str(raw.get('name') or ''),
        description='',
        scan_depth=None,
        token_budget=None,
        recursive_scanning=False,
        extensions={},
        entries=entries,
    )


def parse_book_json(raw: dict) -> CharacterBook:
    """Parse a lorebook JSON dict in any supported shape.

    Supported inputs: SillyTavern world-info (entries keyed by uid or as a
    list), NovelAI Lorebook exports, V2 character-book JSON (entries list or
    keyed mapping), and character-card JSON with an embedded (``data.``)
    ``character_book``.
    """
    if not isinstance(raw, dict):
        return CharacterBook()
    if looks_like_st_world_info(raw):
        return st_world_info_to_book(raw)
    if looks_like_nai_lorebook(raw):
        return nai_lorebook_to_book(raw)
    book = CharacterBook.from_dict(raw)
    if not book.entries:
        # Fall back to an embedded book inside a character card file.
        data = raw.get('data') if isinstance(raw.get('data'), dict) else None
        containers = [raw.get('character_book'),
                      data.get('character_book') if data else None]
        for container in containers:
            if isinstance(container, dict):
                inner = parse_book_json(container)
                if inner.entries:
                    inner.name = (inner.name
                                  or str((data or raw).get('name') or ''))
                    return inner
    return book


# ---------------------------------------------------------------------------
# AI-output parsing
# ---------------------------------------------------------------------------

_FENCE_START_RE = re.compile(r'^```[a-zA-Z0-9_-]*\s*')
_FENCE_END_RE = re.compile(r'\s*```$')


def strip_code_fences(text: str) -> str:
    text = (text or '').strip()
    while text.startswith('```'):
        text = _FENCE_START_RE.sub('', text)
        text = _FENCE_END_RE.sub('', text, count=1)
        text = text.strip()
    return text


def _extract_json_object_or_array(text: str):
    cleaned = strip_code_fences(text)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, (dict, list)):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r'[\{[].*[\}\]]', cleaned, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group())
            if isinstance(obj, (dict, list)):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _normalize_generated_keys(value) -> list[str]:
    if isinstance(value, str):
        parts = value.split(',')
    elif isinstance(value, list):
        parts = [str(k) for k in value]
    else:
        return []
    seen: list[str] = []
    for part in parts:
        key = part.strip()
        if key and key.lower() not in [s.lower() for s in seen]:
            seen.append(key)
    return seen


def _generated_item_to_entry(item: dict, index: int) -> BookEntry | None:
    content = item.get('content')
    if not isinstance(content, str) or not content.strip():
        return None
    name = item.get('name') or item.get('comment') or item.get('title') or ''
    if not isinstance(name, str):
        name = str(name)
    order_raw = item.get('insertion_order', item.get('order'))
    order = _safe_int(order_raw, index * 100)
    return BookEntry(
        name=name.strip(),
        keys=_normalize_generated_keys(item.get('keys')),
        content=content.strip(),
        enabled=True,
        insertion_order=int(order),
        case_sensitive=False,
        match_whole_words=False,
        depth=4,
        position='before_char',
    )


def parse_generated_book(text: str) -> CharacterBook | None:
    """Parse AI output into a :class:`CharacterBook`.

    Accepts ``{"name": ..., "description": ..., "entries": [...]}``,
    a bare ``[...]`` entry array, or the same wrapped in markdown code
    fences. Returns None when no usable entries were found. Entries without
    content are skipped; string keys are split on commas.
    """
    obj = _extract_json_object_or_array(text)
    if obj is None:
        return None
    if isinstance(obj, list):
        items = obj
        name = ''
        description = ''
    elif isinstance(obj, dict):
        items = obj.get('entries')
        if isinstance(items, dict):
            items = list(items.values())
        if not isinstance(items, list):
            items = []
        raw_name = obj.get('name')
        raw_desc = obj.get('description')
        name = raw_name.strip() if isinstance(raw_name, str) else ''
        description = raw_desc.strip() if isinstance(raw_desc, str) else ''
    else:
        return None

    entries: list[BookEntry] = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            entry = _generated_item_to_entry(item, i)
            if entry is not None:
                entries.append(entry)
    if not entries:
        return None
    return CharacterBook(
        name=name,
        description=description,
        scan_depth=None,
        token_budget=None,
        recursive_scanning=False,
        extensions={},
        entries=entries,
    )


def clean_generated_text(text: str) -> str:
    """Strip fences/surrounding quotes from single-text generation output."""
    cleaned = strip_code_fences(text or '').strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in '"\'':
        inner = cleaned[1:-1].strip()
        if inner:
            cleaned = inner
    return cleaned

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from src.app_paths import data_dir
from src import vault
from src import safe_json

logger = logging.getLogger(__name__)


# Written with every session file.  Version 2 is the first one that persists
# the auto-summarize / auto-update toggles faithfully: older files (no key at
# all) never did — the auto-update flag was always written as False no matter
# what the UI showed — so their stored flags are ignored (see session_flags).
FLAGS_VERSION = 2

# Session ids are uuid hexes; anything else (e.g. an 'id' field read from a
# crafted/corrupt session file) must never reach a filesystem path.
_SAFE_SESSION_ID = re.compile(r'^[A-Za-z0-9_-]+$')

# Spellings of a boolean flag as they appear in hand-edited session files.
# ``bool("false")`` is True, so the string has to be read explicitly.
_TRUE_STRINGS = frozenset({'true', '1', 'yes', 'on'})
_FALSE_STRINGS = frozenset({'false', '0', 'no', 'off'})

#: The top-level keys of a session file that carry its metadata.
_SESSION_META_KEYS = ('id', 'title', 'created_at', 'updated_at')


def _scan_session_metadata(path: Path) -> dict | None:
    """The metadata of the session file *path*, without parsing it fully.

    The session files are written by this module with a fixed layout (two
    spaces of indentation, the keys in a known order), so the metadata can
    be read with a line scan and the message array counted without
    materialising every message.  ``None`` means "this file does not have
    the expected layout" and the caller parses it normally.

    Only files that were written by this module qualify: a hand-edited
    session is parsed in full instead, so the listing can never report a
    wrong count for it.
    """
    try:
        raw = vault.read_bytes(path)
    except (OSError, vault.VaultError):
        return None
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return None
    if not text.startswith('{\n'):
        return None
    lines = text.split('\n')
    meta: dict = {}
    counts: dict[str, int] = {}
    current_array: str | None = None
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(' '))
        if current_array is not None:
            if indent <= 2 and stripped.startswith(']'):
                current_array = None
            elif indent == 4 and stripped.startswith('{'):
                counts[current_array] = counts.get(current_array, 0) + 1
            continue
        if indent != 2 or not stripped.startswith('"'):
            continue
        key, _, rest = stripped.partition('":')
        key = key.lstrip('"')
        if key in ('messages', 'memories'):
            value = rest.strip()
            if value.startswith('[]'):
                # An empty array is written on one line: nothing to count.
                counts.setdefault(key, 0)
                continue
            if value.startswith('['):
                current_array = key
                counts.setdefault(key, 0)
            continue
        if key in _SESSION_META_KEYS:
            value = rest.rstrip(',')
            try:
                meta[key] = json.loads(value)
            except (ValueError, TypeError):
                return None
    if current_array is not None:
        # The file was truncated: it does not have the expected layout.
        return None
    if 'id' not in meta or 'title' not in meta:
        return None
    meta['message_count'] = counts.get('messages', 0)
    meta['memory_count'] = counts.get('memories', 0)
    return meta


def coerce_bool(value, default: bool = True) -> bool:
    """Coerce a stored value to a bool, understanding the string spellings.

    Session files are on-disk data that may have been hand-edited or written
    by another tool, and ``"enabled": "false"`` is a common way to store a
    disabled flag.  ``bool("false")`` is True (the string is truthy), which
    silently re-enabled every flag the user had turned off.

    Booleans and numbers are used as they are; ``'false'``/``'0'``/``'no'``/
    ``'off'`` are False, ``'true'``/``'1'``/``'yes'``/``'on'`` are True, and
    anything unrecognised falls back to *default*.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        return default
    return default


def _memory_source(value) -> str:
    """Return the source label of a memory entry (``'manual'`` when unset).

    ``str(None)`` is the literal string ``'None'``, so a missing source used
    to be stored as the source name "None" instead of the default.
    """
    if value is None:
        return 'manual'
    return str(value)


def _safe_session_id(session_id: str) -> str:
    """Return a filesystem-safe version of *session_id*."""
    sid = Path(str(session_id)).name
    if not _SAFE_SESSION_ID.match(sid):
        # Fall back to a hex digest of the raw value so existing files with
        # unusual-but-benign ids remain addressable without traversal risk.
        sid = uuid.uuid5(uuid.NAMESPACE_URL, str(session_id)).hex
    return sid


def sessions_dir() -> Path:
    """Return (and create) the default sessions directory."""
    d = data_dir() / 'sessions'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stored_flag(existing: dict | None, key: str) -> bool:
    """The flag value to keep when the caller passes ``None``.

    Pre-|FLAGS_VERSION| files never persisted these toggles faithfully, so
    their stored values are ignored and the ON default applies instead.
    """
    if not isinstance(existing, dict) or existing.get('flags_version') is None:
        return True
    return bool(existing.get(key, True))


def session_flags(data: dict | None) -> tuple[bool, bool]:
    """Return ``(auto_summarize, auto_update_attributes)`` for a session.

    Files written before :data:`FLAGS_VERSION` existed come back with both
    toggles ON regardless of what they store (that data was never a real
    user choice); stamped files honour their values, defaulting to ON when
    a key is missing.  Pure function.
    """
    data = data if isinstance(data, dict) else {}
    if data.get('flags_version') is None:
        return True, True
    return (
        bool(data.get('auto_summarize', True)),
        bool(data.get('auto_update_attributes', True)),
    )


class ChatSessionStore:
    """Persist per-character chat sessions as JSON files on disk.

    Each session lives at ``<base>/<char_id>/<session_id>.json``.  The store is
    deliberately free of Qt so it can be unit-tested with a ``tmp_path`` base
    directory.
    """

    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            self._base = sessions_dir()
        else:
            self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def _char_dir(self, char_id: int) -> Path:
        d = self._base / str(char_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, char_id: int, session_id: str) -> Path:
        return self._char_dir(char_id) / f'{_safe_session_id(session_id)}.json'

    @staticmethod
    def new_session_id() -> str:
        return uuid.uuid4().hex

    def _read(self, path: Path) -> dict | None:
        try:
            if not path.exists():
                return None
            # vault.read_bytes + decode rather than read_text: a file with
            # invalid UTF-8 raises UnicodeDecodeError, which is a ValueError
            # and NOT an OSError, so read_text's OSError guard would not catch
            # it.  The vault helper transparently decrypts sealed sessions.
            from src.fs_utils import decode_text
            data = safe_json.loads_safe(decode_text(vault.read_bytes(path)))
            if isinstance(data, dict):
                return data
            logger.warning("Session file %s is not a JSON object", path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError, vault.VaultError) as e:
            logger.warning("Could not read session file %s: %s", path, e)
        return None

    def list_sessions(self, char_id: int) -> list[dict]:
        """Return metadata for every saved session of *char_id*, newest first.

        Only the metadata is needed here, so the session files are *scanned*
        rather than parsed: a character with a long chat history has
        sessions of megabytes each, and loading all of them to list their
        titles is what made the Chats window slow.
        """
        results: list[dict] = []
        for path in sorted(self._char_dir(char_id).glob('*.json')):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            meta = self._read_metadata(path)
            if meta is None:
                continue
            results.append({
                'id': meta.get('id') or path.stem,
                'char_id': char_id,
                'title': meta.get('title', ''),
                'message_count': meta.get('message_count', 0),
                'memory_count': meta.get('memory_count', 0),
                'created_at': meta.get('created_at', ''),
                'updated_at': meta.get('updated_at', ''),
                '_sort_key': (mtime, path.name),
            })
        results.sort(key=lambda r: r['_sort_key'], reverse=True)
        for r in results:
            r.pop('_sort_key', None)
        return results

    def _read_metadata(self, path: Path) -> dict | None:
        """The metadata of the session file *path* (None when unreadable)."""
        scanned = _scan_session_metadata(path)
        if scanned is not None:
            return scanned
        # The file does not have the expected layout: parse it normally.
        data = self._read(path)
        if not data:
            return None
        messages = data.get('messages')
        return {
            'id': data.get('id', path.stem),
            'title': data.get('title', ''),
            'message_count': len(messages) if isinstance(messages, list) else 0,
            'memory_count': len(_normalize_memories_raw(data)),
            'created_at': data.get('created_at', ''),
            'updated_at': data.get('updated_at', ''),
        }

    def save_session(
        self,
        char_id: int,
        session_id: str,
        title: str,
        messages: list[dict],
        memories: list[dict] | None = None,
        auto_summarize: bool | None = None,
        memory: str = '',
        author_note: dict | None = None,
        jailbreak: str | None = None,
        persona_id: str | None = None,
        system_template: str | None = None,
        context_template: str | None = None,
        attributes: list[dict] | None = None,
        auto_update_attributes: bool | None = None,
    ) -> None:
        """Write a session, preserving its original ``created_at``.

        ``memories=None`` means "keep whatever the file already has" (with a
        legacy ``memory`` string migrated to the list form); ``auto_summarize``
        follows the same convention so the flag can be turned off explicitly
        without also wiping the memory list.  ``author_note`` / ``jailbreak`` /
        ``persona_id`` / ``system_template`` / ``context_template`` /
        ``attributes`` / ``auto_update_attributes`` are likewise kept
        unchanged when ``None``, so callers that don't manage them (import
        paths, older code) can't wipe them.
        """
        path = self._path(char_id, session_id)
        created_at = None
        existing = self._read(path)
        if auto_summarize is None:
            auto_summarize = _stored_flag(existing, 'auto_summarize')
        if auto_update_attributes is None:
            auto_update_attributes = _stored_flag(existing, 'auto_update_attributes')
        if existing:
            created_at = existing.get('created_at')
            if memories is None:
                # normalize_memories handles both the modern ``memories``
                # list and the legacy ``memory`` string field, so a
                # legacy-only file keeps its text across saves.
                memories = normalize_memories(existing)
            if author_note is None:
                author_note = normalize_author_note(existing.get('author_note'))
            if jailbreak is None:
                stored_jb = existing.get('jailbreak')
                jailbreak = stored_jb if isinstance(stored_jb, str) else ''
            if persona_id is None:
                stored_pid = existing.get('persona_id')
                persona_id = stored_pid if isinstance(stored_pid, str) else ''
            if system_template is None:
                stored_tpl = existing.get('system_template')
                system_template = stored_tpl if isinstance(stored_tpl, str) else ''
            if context_template is None:
                stored_ctx = existing.get('context_template')
                context_template = stored_ctx if isinstance(stored_ctx, str) else ''
            if attributes is None:
                attributes = normalize_attributes(existing)
        now = datetime.now().isoformat(timespec='seconds')
        data = {
            'id': session_id,
            'char_id': char_id,
            'title': title,
            'flags_version': FLAGS_VERSION,
            'messages': [
                dict(m) for m in messages or [] if isinstance(m, dict)
            ],
            'memories': [c for c in (_clean_memory(m) for m in (memories or [])) if c is not None],
            'auto_summarize': bool(auto_summarize),
            'attributes': [
                c for c in (_clean_attribute(a) for a in (attributes or []))
                if c is not None
            ],
            'auto_update_attributes': bool(auto_update_attributes),
            'author_note': normalize_author_note(author_note),
            'jailbreak': jailbreak or '',
            'persona_id': persona_id or '',
            'system_template': system_template or '',
            'context_template': context_template or '',
            'created_at': created_at or now,
            'updated_at': now,
        }
        # Atomic write: a crash mid-save must never truncate the session file.
        vault.write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))

    def load_session(self, char_id: int, session_id: str) -> dict | None:
        return self._read(self._path(char_id, session_id))

    def delete_session(self, char_id: int, session_id: str) -> bool:
        path = self._path(char_id, session_id)
        try:
            if path.exists():
                path.unlink()
                return True
        except OSError as e:
            logger.warning("Could not delete session file %s: %s", path, e)
        return False


def auto_title(messages: list[dict], fallback: str = 'New chat') -> str:
    """Derive a session title from the first user message, else *fallback*."""
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get('role') == 'user':
            text = str(msg.get('content', '')).strip()
            if text:
                return text[:60]
    return fallback


def new_memory_entry(
    content: str,
    source: str = 'manual',
    end_index: int | None = None,
) -> dict:
    """Build a dict for a single memory entry.

    *end_index* optionally records the last message index the entry
    summarizes, so it can be dropped when that range is deleted/regenerated.
    """
    entry = {
        'id': uuid.uuid4().hex,
        'content': content,
        'source': source,
        'created_at': datetime.now().isoformat(timespec='seconds'),
    }
    if end_index is not None:
        entry['end_index'] = int(end_index)
    return entry


def _coerce_end_index(value) -> int | None:
    """Coerce a stored ``end_index`` to an int, or None if it isn't numeric.

    Session files are on-disk data that may have been hand-edited or written by
    an older build, so a non-numeric value must not raise out of a listing.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("Ignoring non-numeric memory end_index: %r", value)
        return None


def drop_memories_from(memories: list[dict], index: int) -> list[dict]:
    """Return *memories* without entries that summarize messages at/after *index*.

    Entries carrying an ``end_index`` >= *index* are dropped because their
    source messages no longer exist after a delete/regenerate truncation.
    Manual entries (no usable ``end_index``) are kept.  Pure function.
    """
    result: list[dict] = []
    for m in memories or []:
        if not isinstance(m, dict):
            result.append(m)
            continue
        end = _coerce_end_index(m.get('end_index'))
        if end is not None and end >= index:
            continue
        result.append(m)
    return result


DEFAULT_AUTHOR_NOTE_DEPTH = 4
_AUTHOR_NOTE_ROLES = ('system', 'user', 'assistant')


def normalize_author_note(raw) -> dict:
    """Coerce a stored ``author_note`` value into ``{text, depth, role}``.

    Session files are on-disk data that may have been hand-edited or written
    by an older build: a bare string is treated as the note text, out-of-range
    depths fall back to :data:`DEFAULT_AUTHOR_NOTE_DEPTH`, and unknown roles
    become ``system``.  Pure function.
    """
    if isinstance(raw, str):
        raw = {'text': raw}
    if not isinstance(raw, dict):
        raw = {}
    text = raw.get('text', '')
    text = text if isinstance(text, str) else str(text or '')
    try:
        depth = int(raw.get('depth', DEFAULT_AUTHOR_NOTE_DEPTH))
    except (TypeError, ValueError):
        depth = DEFAULT_AUTHOR_NOTE_DEPTH
    depth = max(0, min(99, depth))
    role = raw.get('role', 'system')
    role = role if role in _AUTHOR_NOTE_ROLES else 'system'
    return {'text': text, 'depth': depth, 'role': role}


def normalize_memories(data: dict | None) -> list[dict]:
    """Return a list of memory entries from session *data*.

    Handles both the modern ``memories`` list and the legacy ``memory``
    string field (migrating it to a single-entry list).  Pure function
    so it can be unit-tested without Qt.
    """
    return _normalize_memories_raw(data)


def _normalize_memories_raw(data: dict | None) -> list[dict]:
    if not isinstance(data, dict):
        return []
    raw = data.get('memories')
    if isinstance(raw, list):
        result: list[dict] = []
        for m in raw:
            if isinstance(m, dict) and m.get('content'):
                entry = {
                    'id': m.get('id') or uuid.uuid4().hex,
                    'content': str(m.get('content', '')),
                    'source': _memory_source(m.get('source')),
                    'created_at': str(m.get('created_at', '')),
                }
                if _coerce_end_index(m.get('end_index')) is not None:
                    entry['end_index'] = _coerce_end_index(m.get('end_index'))
                result.append(entry)
        return result
    legacy = data.get('memory')
    if isinstance(legacy, str) and legacy.strip():
        return [new_memory_entry(legacy.strip(), source='manual')]
    return []


def _clean_memory(m) -> dict | None:
    """Return a cleaned copy of a memory entry, or None for garbage input."""
    if not isinstance(m, dict):
        return None
    cleaned = {
        'id': m.get('id', uuid.uuid4().hex),
        'content': str(m.get('content', '')),
        'source': _memory_source(m.get('source')),
        'created_at': str(m.get('created_at', '')),
    }
    end = _coerce_end_index(m.get('end_index'))
    if end is not None:
        cleaned['end_index'] = end
    return cleaned


# ---- persistent attributes (tracked story state) ----

DEFAULT_ATTRIBUTE_NAMES: tuple[str, ...] = (
    'Setting',
    '{{char}} mood',
    '{{char}} clothing',
    'Time & weather',
    '{{char}} physical state',
    '{{user}}-{{char}} relationship',
    'Current activity',
    'Who is present',
)

DEFAULT_ATTRIBUTE_DESCRIPTIONS: dict[str, str] = {
    'Setting': (
        'Where the scene currently takes place and the relevant surroundings.'
    ),
    '{{char}} mood': "{{char}}'s current emotional state.",
    '{{char}} clothing': (
        "What {{char}} is currently wearing, including its "
        'condition (wet, torn, ...).'
    ),
    'Time & weather': (
        'The current time of day and weather; update when either changes.'
    ),
    '{{char}} physical state': (
        "{{char}}'s physical condition: injuries, exhaustion, "
        'hunger, illness.'
    ),
    '{{user}}-{{char}} relationship': (
        'How {{char}} and {{user}} stand with each other '
        '(strangers, friends, tension, ...).'
    ),
    'Current activity': (
        'What is happening right now and what {{char}} is doing.'
    ),
    'Who is present': 'Which characters are present in the scene.',
}


def default_attributes() -> list[dict]:
    """The persistent attributes a new chat starts watching."""
    return [
        new_attribute_entry(
            name, description=DEFAULT_ATTRIBUTE_DESCRIPTIONS.get(name, ''),
        )
        for name in DEFAULT_ATTRIBUTE_NAMES
    ]


def new_attribute_entry(
    name: str,
    value: str = '',
    description: str = '',
    enabled: bool = True,
) -> dict:
    """Build a dict for one watched persistent attribute.

    ``value`` is the current state (e.g. the character's mood),
    ``description`` tells the model what the attribute tracks and what
    counts as a change, ``enabled`` is the per-attribute on/off switch
    (a disabled attribute is neither tracked nor sent in the prompt),
    and ``updated_at`` records when the value last changed.
    """
    return {
        'id': uuid.uuid4().hex,
        'name': str(name or '').strip(),
        'value': str(value or ''),
        'description': str(description or ''),
        'enabled': coerce_bool(enabled, True),
        'updated_at': '',
    }


def normalize_attributes(data: dict | None) -> list[dict]:
    """Return the persistent-attribute list stored in session *data*.

    Handles on-disk data that may have been hand-edited or written by an
    older build: malformed entries are skipped and duplicate names collapse
    to their first occurrence.  Pure function.
    """
    return _normalize_attributes_raw(data)


def _normalize_attributes_raw(data: dict | None) -> list[dict]:
    if not isinstance(data, dict):
        return []
    raw = data.get('attributes')
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for a in raw:
        entry = _clean_attribute(a)
        if entry is None:
            continue
        key = entry['name'].casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _clean_attribute(a) -> dict | None:
    """Return a cleaned copy of an attribute entry, or None for garbage input."""
    if not isinstance(a, dict):
        return None
    name = str(a.get('name', '') or '').strip()
    if not name:
        return None
    return {
        'id': str(a.get('id') or uuid.uuid4().hex),
        'name': name,
        'value': str(a.get('value', '') or ''),
        'description': str(a.get('description', '') or ''),
        'enabled': coerce_bool(a.get('enabled'), True),
        'updated_at': str(a.get('updated_at', '') or ''),
    }


_NO_CHANGES_RE = re.compile(
    r'^\s*(?:no\s+changes?|none|unchanged)\s*\.?\s*$', re.IGNORECASE,
)


def parse_attribute_updates(text: str, names: list[str]) -> dict[str, str]:
    """Parse an attribute-update LLM reply into ``{name: new value}``.

    Expected format is one ``Name: value`` line per changed attribute, or a
    ``NO CHANGES`` reply.  Only names on the watched *names* list are
    accepted (matched case-insensitively; the canonical name is returned);
    unknown names and empty values are ignored.  Markdown fences around the
    reply are tolerated.  Pure function.
    """
    if not text or not str(text).strip():
        return {}
    body = str(text).strip()
    if body.startswith('```'):
        body = re.sub(r'^```[A-Za-z0-9_-]*\s*', '', body)
        body = re.sub(r'\s*```$', '', body)
        body = body.strip()
    if _NO_CHANGES_RE.match(body):
        return {}
    canonical = {n.strip().casefold(): n.strip() for n in (names or []) if n and n.strip()}
    updates: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip().lstrip('-*').strip()
        if not line or ':' not in line:
            continue
        key, _, value = line.partition(':')
        name = canonical.get(key.strip().casefold())
        value = value.strip()
        if name and value:
            updates[name] = value
    return updates


def apply_attribute_updates(attributes: list[dict], updates: dict[str, str]) -> list[dict]:
    """Return a copy of *attributes* with *updates* applied by name.

    Matching is case-insensitive on the attribute name; names in *updates*
    that no watched attribute carries are ignored.  Updated entries get a
    fresh ``updated_at`` timestamp.  Pure function.
    """
    by_key = {str(k).strip().casefold(): str(v) for k, v in (updates or {}).items()}
    now = datetime.now().isoformat(timespec='seconds')
    result: list[dict] = []
    for a in attributes or []:
        if not isinstance(a, dict):
            result.append(a)
            continue
        entry = dict(a)
        value = by_key.get(str(entry.get('name', '') or '').strip().casefold())
        if value is not None:
            entry['value'] = value
            entry['updated_at'] = now
        result.append(entry)
    return result

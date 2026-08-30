from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from src.app_paths import data_dir
from src.fs_utils import atomic_write_bytes

logger = logging.getLogger(__name__)

# Session ids are uuid hexes; anything else (e.g. an 'id' field read from a
# crafted/corrupt session file) must never reach a filesystem path.
_SAFE_SESSION_ID = re.compile(r'^[A-Za-z0-9_-]+$')


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
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not read session file %s: %s", path, e)
        return None

    def list_sessions(self, char_id: int) -> list[dict]:
        """Return metadata for every saved session of *char_id*, newest first."""
        results: list[dict] = []
        for path in sorted(self._char_dir(char_id).glob('*.json')):
            data = self._read(path)
            if not data:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            results.append({
                'id': data.get('id', path.stem),
                'char_id': char_id,
                'title': data.get('title', ''),
                'message_count': len(data.get('messages', [])),
                'memory_count': len(_normalize_memories_raw(data)),
                'created_at': data.get('created_at', ''),
                'updated_at': data.get('updated_at', ''),
                '_sort_key': (mtime, path.name),
            })
        results.sort(key=lambda r: r['_sort_key'], reverse=True)
        for r in results:
            r.pop('_sort_key', None)
        return results

    def save_session(
        self,
        char_id: int,
        session_id: str,
        title: str,
        messages: list[dict],
        memories: list[dict] | None = None,
        auto_summarize: bool = False,
        memory: str = '',
    ) -> None:
        """Write a session, preserving its original ``created_at``."""
        path = self._path(char_id, session_id)
        created_at = None
        existing = self._read(path)
        if existing:
            created_at = existing.get('created_at')
            # Keep existing memories/auto flag if the caller didn't provide
            # them and the old file used the legacy ``memory`` string field.
            if memories is None and not auto_summarize and existing.get('memories') is not None:
                memories = existing.get('memories')
                auto_summarize = bool(existing.get('auto_summarize', False))
        now = datetime.now().isoformat(timespec='seconds')
        data = {
            'id': session_id,
            'char_id': char_id,
            'title': title,
            'messages': [dict(m) for m in messages],
            'memories': [_clean_memory(m) for m in (memories or [])],
            'auto_summarize': bool(auto_summarize),
            'created_at': created_at or now,
            'updated_at': now,
        }
        # Atomic write: a crash mid-save must never truncate the session file.
        atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))

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


def auto_title(messages: list[dict], fallback: str = 'New session') -> str:
    """Derive a session title from the first user message, else *fallback*."""
    for msg in messages or []:
        if msg.get('role') == 'user':
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


def drop_memories_from(memories: list[dict], index: int) -> list[dict]:
    """Return *memories* without entries that summarize messages at/after *index*.

    Entries carrying an ``end_index`` >= *index* are dropped because their
    source messages no longer exist after a delete/regenerate truncation.
    Manual entries (no ``end_index``) are kept.  Pure function.
    """
    result: list[dict] = []
    for m in memories or []:
        if not isinstance(m, dict):
            result.append(m)
            continue
        end = m.get('end_index')
        if end is not None and int(end) >= index:
            continue
        result.append(m)
    return result


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
                    'source': str(m.get('source', 'manual')),
                    'created_at': str(m.get('created_at', '')),
                }
                if m.get('end_index') is not None:
                    entry['end_index'] = int(m['end_index'])
                result.append(entry)
        return result
    legacy = data.get('memory')
    if isinstance(legacy, str) and legacy.strip():
        return [new_memory_entry(legacy.strip(), source='manual')]
    return []


def _clean_memory(m: dict) -> dict:
    cleaned = {
        'id': m.get('id', uuid.uuid4().hex),
        'content': str(m.get('content', '')),
        'source': str(m.get('source', 'manual')),
        'created_at': str(m.get('created_at', '')),
    }
    if m.get('end_index') is not None:
        cleaned['end_index'] = int(m['end_index'])
    return cleaned

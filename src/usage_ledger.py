"""Token/cost ledger.

Records what every generation cost: which provider and model served it,
which card and session it belonged to, and how many tokens went in and
out.  The ledger is the data behind the usage statistics (tokens per
card, per provider, per day) and behind the cost estimates.

Storage: a single JSON file under ``<data dir>/usage-ledger.json``,
written through :mod:`src.vault` so it is encrypted when the vault is
on.  The file is capped at :data:`MAX_ENTRIES` entries (the oldest are
dropped) so it cannot grow without limit.

Pure logic (no Qt) so it is unit-testable and safe to call from the
generation worker thread.  :func:`record_usage` never raises: a failed
ledger write must not break a chat.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src import safe_json
from src import vault

logger = logging.getLogger(__name__)

#: Name of the ledger file inside the app data dir.
LEDGER_FILENAME = 'usage-ledger.json'

#: Upper bound on the number of stored entries.  The oldest entries are
#: dropped once the limit is reached, so the file cannot grow forever.
MAX_ENTRIES = 5000

#: Version of the on-disk format (bumped when the layout changes).
LEDGER_VERSION = 1

ROLE_PROMPT = 'prompt'
ROLE_COMPLETION = 'completion'


@dataclass
class UsageEntry:
    """One recorded API call (or one side of it)."""

    timestamp: str        # ISO 8601
    provider: str
    model: str
    card_id: int | None
    card_name: str
    session_id: str
    role: str             # 'prompt' | 'completion'
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        """The input plus the output tokens of this entry."""
        return max(0, int(self.input_tokens or 0)) + max(0, int(self.output_tokens or 0))

    def to_dict(self) -> dict[str, Any]:
        """The entry as a plain dict (the on-disk shape)."""
        return {
            'timestamp': self.timestamp,
            'provider': self.provider,
            'model': self.model,
            'card_id': self.card_id,
            'card_name': self.card_name,
            'session_id': self.session_id,
            'role': self.role,
            'input_tokens': int(self.input_tokens or 0),
            'output_tokens': int(self.output_tokens or 0),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> UsageEntry:
        """Build an entry from a stored dict, tolerating missing fields.

        Args:
            raw: The stored dict (as written by :meth:`to_dict`).

        Returns:
            The entry; unreadable numbers become 0 and a missing card id
            becomes ``None``.
        """
        data = raw if isinstance(raw, dict) else {}
        card_id = data.get('card_id')
        try:
            card_id = int(card_id) if card_id is not None else None
        except (TypeError, ValueError):
            card_id = None
        return cls(
            timestamp=str(data.get('timestamp', '') or ''),
            provider=str(data.get('provider', '') or ''),
            model=str(data.get('model', '') or ''),
            card_id=card_id,
            card_name=str(data.get('card_name', '') or ''),
            session_id=str(data.get('session_id', '') or ''),
            role=str(data.get('role', '') or ''),
            input_tokens=_safe_int(data.get('input_tokens')),
            output_tokens=_safe_int(data.get('output_tokens')),
        )


def _safe_int(value: Any) -> int:
    """Coerce *value* to a non-negative int (0 when it is not a number)."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def ledger_path() -> Path:
    """Path of the ledger file inside the app data dir."""
    from src.app_paths import data_dir

    return data_dir() / LEDGER_FILENAME


def _decode(data: Any) -> list[UsageEntry]:
    """Turn a parsed ledger document into a list of entries."""
    if isinstance(data, dict):
        raw_entries = data.get('entries')
    elif isinstance(data, list):
        raw_entries = data
    else:
        raw_entries = []
    if not isinstance(raw_entries, list):
        return []
    return [
        UsageEntry.from_dict(raw)
        for raw in raw_entries
        if isinstance(raw, dict)
    ]


def load_entries() -> list[UsageEntry]:
    """Every recorded usage entry, oldest first.

    A missing, unreadable or corrupt ledger yields an empty list; the
    ledger is a statistic, never a reason to fail a chat.
    """
    path = ledger_path()
    try:
        if not path.exists():
            return []
        raw = vault.read_bytes(path)
    except OSError as exc:
        logger.warning('Could not read the usage ledger: %s', exc)
        return []
    except vault.VaultError as exc:
        logger.warning('The usage ledger could not be decrypted: %s', exc)
        return []
    try:
        data = safe_json.loads_safe(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning('The usage ledger is corrupt and was ignored: %s', exc)
        return []
    return _decode(data)


def _write_entries(entries: list[UsageEntry]) -> None:
    """Write *entries* to the ledger file (encrypted when the vault is on)."""
    path = ledger_path()
    document = {
        'version': LEDGER_VERSION,
        'entries': [entry.to_dict() for entry in entries],
    }
    payload = json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')
    vault.write_bytes(path, payload)


#: The ledger is a read-modify-write: the Generate tab's worker thread and
#: the Test tab's worker thread both append to it, and without a lock the
#: slower write wins and the other entry is lost.
_LEDGER_LOCK = threading.Lock()


def record_usage(entry: UsageEntry) -> None:
    """Append *entry* to the ledger.

    The stored entries are capped at :data:`MAX_ENTRIES`; the oldest ones
    are dropped.  This function never raises: a failed ledger write must
    not break the chat that produced the entry.
    """
    try:
        with _LEDGER_LOCK:
            entries = load_entries()
            entries.append(entry)
            if len(entries) > MAX_ENTRIES:
                entries = entries[-MAX_ENTRIES:]
            _write_entries(entries)
    except Exception:
        logger.warning('Could not record the usage entry', exc_info=True)


def record_generation(
    provider: str,
    model: str,
    card_id: int | None,
    card_name: str,
    session_id: str,
    prompt_text: str = '',
    reply_text: str = '',
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Record one API call in the ledger.

    The prompt and the completion are stored as two entries (one per
    role), so the "By role" breakdown of the usage dialog shows what the
    request cost and what the answer cost instead of a single row.  The
    totals are unaffected: the prompt entry carries the input tokens and
    the completion entry the output tokens.

    *input_tokens* / *output_tokens* override the counting when the caller
    already knows the exact numbers.

    Never raises.
    """
    try:
        from src.token_counter import count_tokens

        stamp = datetime.now().isoformat(timespec='seconds')
        if input_tokens is None:
            input_tokens = count_tokens(prompt_text or '')
        if output_tokens is None:
            output_tokens = count_tokens(reply_text or '')
        if input_tokens:
            record_usage(UsageEntry(
                timestamp=stamp,
                provider=provider or '',
                model=model or '',
                card_id=card_id,
                card_name=card_name or '',
                session_id=session_id or '',
                role=ROLE_PROMPT,
                input_tokens=input_tokens,
                output_tokens=0,
            ))
        if output_tokens:
            record_usage(UsageEntry(
                timestamp=stamp,
                provider=provider or '',
                model=model or '',
                card_id=card_id,
                card_name=card_name or '',
                session_id=session_id or '',
                role=ROLE_COMPLETION,
                input_tokens=0,
                output_tokens=output_tokens,
            ))
    except Exception:
        logger.warning('Could not record the usage of the generation', exc_info=True)


def _coerce(entry: Any) -> Optional[UsageEntry]:
    """Return *entry* as a :class:`UsageEntry` (None when it is not one)."""
    if isinstance(entry, UsageEntry):
        return entry
    if isinstance(entry, dict):
        return UsageEntry.from_dict(entry)
    return None


def _entries(entries: Any = None) -> list[UsageEntry]:
    """The entries to aggregate (the stored ones when *entries* is None)."""
    if entries is None:
        return load_entries()
    if isinstance(entries, (UsageEntry, dict)):
        entries = [entries]
    out: list[UsageEntry] = []
    for item in entries or []:
        coerced = _coerce(item)
        if coerced is not None:
            out.append(coerced)
    return out


def total_tokens(entries: Any = None) -> int:
    """The total number of tokens (input plus output) across *entries*."""
    return sum(entry.total_tokens for entry in _entries(entries))


def total_by_role(entries: Any = None) -> dict[str, int]:
    """The tokens per role (``'prompt'`` / ``'completion'``)."""
    out: dict[str, int] = {}
    for entry in _entries(entries):
        key = entry.role or 'unknown'
        out[key] = out.get(key, 0) + entry.total_tokens
    return out


def total_by_card(entries: Any = None) -> dict[str, int]:
    """The tokens per card id.

    Entries without a card id (a chat that is not bound to a card) are
    not counted - they belong to no card.
    """
    out: dict[int, int] = {}
    for entry in _entries(entries):
        if entry.card_id is None:
            continue
        out[entry.card_id] = out.get(entry.card_id, 0) + entry.total_tokens
    return out


def total_by_provider(entries: Any = None) -> dict[str, int]:
    """The tokens per provider."""
    out: dict[str, int] = {}
    for entry in _entries(entries):
        key = entry.provider or 'unknown'
        out[key] = out.get(key, 0) + entry.total_tokens
    return out


def _entry_date(entry: UsageEntry) -> str:
    """The ``'YYYY-MM-DD'`` day of *entry* ('' when it has no date)."""
    stamp = (entry.timestamp or '').strip()
    if len(stamp) >= 10 and stamp[4] == '-' and stamp[7] == '-':
        return stamp[:10]
    try:
        return datetime.fromisoformat(stamp).date().isoformat()
    except (TypeError, ValueError):
        return ''


def daily_totals(entries: Any = None) -> dict[str, int]:
    """The tokens per calendar day (``'YYYY-MM-DD'`` -> tokens)."""
    out: dict[str, int] = {}
    for entry in _entries(entries):
        day = _entry_date(entry)
        if not day:
            continue
        out[day] = out.get(day, 0) + entry.total_tokens
    return out


def session_stats(entries: Any = None) -> list[dict[str, Any]]:
    """Per-session statistics, most recently active first.

    Every session gets a dict with ``session_id``, ``card_name``,
    ``messages`` (the number of recorded usage entries), ``tokens``,
    ``first_seen`` and ``last_seen``.
    """
    stats: dict[str, dict[str, Any]] = {}
    for entry in _entries(entries):
        key = entry.session_id or ''
        row = stats.get(key)
        if row is None:
            row = {
                'session_id': key,
                'card_name': entry.card_name,
                'messages': 0,
                'tokens': 0,
                'first_seen': '',
                'last_seen': '',
            }
            stats[key] = row
        row['messages'] += 1
        row['tokens'] += entry.total_tokens
        stamp = entry.timestamp or ''
        if stamp:
            if not row['first_seen'] or stamp < row['first_seen']:
                row['first_seen'] = stamp
            if not row['last_seen'] or stamp > row['last_seen']:
                row['last_seen'] = stamp
    return sorted(
        stats.values(),
        key=lambda row: (row['last_seen'], row['session_id']),
        reverse=True,
    )


def estimate_cost(
    entry: UsageEntry,
    price_per_1k_input: float,
    price_per_1k_output: float,
) -> float:
    """The price of *entry* in the currency of the given per-1k prices.

    Args:
        entry: The recorded usage.
        price_per_1k_input: Price of 1 000 input tokens.
        price_per_1k_output: Price of 1 000 output tokens.

    Returns:
        The estimated cost (0.0 for an entry without tokens).
    """
    if not isinstance(entry, UsageEntry):
        entry = UsageEntry.from_dict(entry if isinstance(entry, dict) else {})
    try:
        input_price = float(price_per_1k_input)
        output_price = float(price_per_1k_output)
    except (TypeError, ValueError):
        return 0.0
    cost = (
        max(0, int(entry.input_tokens or 0)) / 1000.0 * input_price
        + max(0, int(entry.output_tokens or 0)) / 1000.0 * output_price
    )
    return cost

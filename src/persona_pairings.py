"""Saved card + persona pairings.

A pairing remembers that a certain character is played with a certain user
persona: the card, the persona and a free-form note.  Pairings make it easy
to switch between "the same character, different me" setups without
re-typing the persona, and they seed a new chat with the persona's greeting
line.

The store is a single JSON file (``pairings.json``) in the app-data
directory.  It is written through :mod:`src.vault`, so it is encrypted with
the vault when encryption is enabled - the persona text is user content and
must not end up in a plaintext file next to the library.

Pure logic: no Qt, so the module can run on a worker thread.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.app_paths import data_dir
from src import safe_json
from src import vault

logger = logging.getLogger(__name__)

#: Name of the store inside the app-data directory.
PAIRINGS_FILENAME = 'pairings.json'


@dataclass
class Pairing:
    """A saved card + persona combination."""

    id: str
    card_id: int
    card_name: str
    persona_name: str
    persona_text: str
    created_at: str
    notes: str = ''

    def to_dict(self) -> dict:
        """The persisted form of the pairing."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> Optional['Pairing']:
        """Build a pairing from stored data (None when the data is unusable)."""
        if not isinstance(raw, dict):
            return None
        pairing_id = str(raw.get('id', '') or '').strip()
        if not pairing_id:
            return None
        try:
            card_id = int(raw.get('card_id', 0) or 0)
        except (TypeError, ValueError):
            card_id = 0
        return cls(
            id=pairing_id,
            card_id=card_id,
            card_name=str(raw.get('card_name', '') or ''),
            persona_name=str(raw.get('persona_name', '') or ''),
            persona_text=str(raw.get('persona_text', '') or ''),
            created_at=str(raw.get('created_at', '') or ''),
            notes=str(raw.get('notes', '') or ''),
        )


def _pairings_path() -> Path:
    """The path of the pairing store."""
    return data_dir() / PAIRINGS_FILENAME


def make_pairing(
    card_id: int,
    card_name: str,
    persona_name: str,
    persona_text: str,
    notes: str = '',
) -> Pairing:
    """Build a new pairing with a fresh id and a creation timestamp."""
    return Pairing(
        id=uuid.uuid4().hex,
        card_id=int(card_id or 0),
        card_name=str(card_name or ''),
        persona_name=str(persona_name or ''),
        persona_text=str(persona_text or ''),
        created_at=datetime.now().isoformat(timespec='seconds'),
        notes=str(notes or ''),
    )


def _read_stored() -> list[dict]:
    """Read the raw pairing records ([] when the store is missing or broken)."""
    path = _pairings_path()
    try:
        raw = vault.read_bytes(path)
    except FileNotFoundError:
        return []
    except (OSError, vault.VaultError) as exc:
        logger.warning('Could not read the pairing store: %s', exc)
        return []
    try:
        data = safe_json.loads_safe(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning('Could not parse the pairing store: %s', exc)
        return []
    if isinstance(data, dict):
        data = data.get('pairings')
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _write_stored(records: list[dict]) -> bool:
    """Write the pairing records (False when the write failed)."""
    payload = json.dumps(records, ensure_ascii=False, indent=2).encode('utf-8')
    try:
        vault.write_bytes(_pairings_path(), payload)
    except (OSError, vault.VaultError) as exc:
        logger.warning('Could not write the pairing store: %s', exc)
        return False
    return True


def list_pairings() -> list[Pairing]:
    """Return every saved pairing, newest first."""
    pairings = [
        pairing
        for pairing in (Pairing.from_dict(raw) for raw in _read_stored())
        if pairing is not None
    ]
    pairings.sort(key=lambda p: (p.created_at, p.id), reverse=True)
    return pairings


def get_pairing(pairing_id: str) -> Optional[Pairing]:
    """Return the pairing with *pairing_id* (None when there is none)."""
    wanted = str(pairing_id or '').strip()
    if not wanted:
        return None
    for pairing in list_pairings():
        if pairing.id == wanted:
            return pairing
    return None


def save_pairing(pairing: Pairing) -> bool:
    """Save *pairing*, replacing an existing pairing with the same id.

    Returns:
        True when the store was written, False when the write failed.
    """
    if pairing is None or not str(pairing.id or '').strip():
        return False
    records = _read_stored()
    record = pairing.to_dict()
    replaced = False
    for index, existing in enumerate(records):
        if str(existing.get('id', '') or '') == pairing.id:
            records[index] = record
            replaced = True
            break
    if not replaced:
        records.append(record)
    return _write_stored(records)


def delete_pairing(pairing_id: str) -> bool:
    """Delete the pairing with *pairing_id*.

    Returns:
        True when a pairing was removed, False when there was none (or the
        store could not be written).
    """
    wanted = str(pairing_id or '').strip()
    if not wanted:
        return False
    records = _read_stored()
    remaining = [
        record for record in records
        if str(record.get('id', '') or '') != wanted
    ]
    if len(remaining) == len(records):
        return False
    return _write_stored(remaining)


def export_pairings() -> str:
    """Return every pairing as JSON text (for a backup or a hand-off)."""
    records = [pairing.to_dict() for pairing in list_pairings()]
    return json.dumps(records, ensure_ascii=False, indent=2)


def import_pairings(text: str) -> int:
    """Import pairings from JSON text.

    Args:
        text: The JSON payload - a list of pairing records or an object with
            a ``pairings`` list.

    Returns:
        The number of pairings imported.  A pairing whose id is already
        known replaces the stored one.  Malformed input is ignored: this
        function never raises.
    """
    if not (text or '').strip():
        return 0
    try:
        data = safe_json.loads_safe(text)
    except ValueError as exc:
        logger.warning('Could not parse the imported pairings: %s', exc)
        return 0
    if isinstance(data, dict):
        data = data.get('pairings')
    if not isinstance(data, list):
        return 0

    records = _read_stored()
    known = {str(record.get('id', '') or '') for record in records}
    imported = 0
    for raw in data:
        pairing = Pairing.from_dict(raw)
        if pairing is None:
            continue
        record = pairing.to_dict()
        if pairing.id in known:
            records = [
                item for item in records
                if str(item.get('id', '') or '') != pairing.id
            ]
        records.append(record)
        known.add(pairing.id)
        imported += 1
    if imported and not _write_stored(records):
        return 0
    return imported


def pairing_greeting(pairing: Pairing) -> str:
    """The greeting line of a pairing's persona.

    The persona text's first non-empty line, used to seed a new chat with
    the persona.  An empty string when the pairing or its persona text is
    blank.
    """
    if pairing is None:
        return ''
    for line in (pairing.persona_text or '').splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ''


def pairing_summary(pairing: Pairing) -> str:
    """A one-line description of a pairing (for a list row)."""
    if pairing is None:
        return ''
    persona = pairing.persona_name or 'Persona'
    return f'{pairing.card_name or "Card"} — {persona}'

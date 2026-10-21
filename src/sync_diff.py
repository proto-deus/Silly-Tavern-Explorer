"""Field-level diff between the local library and its remote counterpart.

The SillyTavern sync classifies a card pair as "changed" or "in sync" from a
content hash, which is enough to drive the bulk push/pull plan but says
nothing about *what* differs.  This module answers that question: it
compares a local card with its remote (SillyTavern) copy field by field and
reports the differences, so a diff view can show the user exactly which
prompt fields would be overwritten by a pull or a push.

The same machinery compares two lorebooks: their name and their entries
(key, content, comment, order, position, depth, role).

Pure logic: no Qt, no filesystem, no network.  The comparison is textual -
every field is reduced to a canonical string (lists and the character book
are serialised to sorted JSON) so that a re-ordered JSON object is not
reported as a change.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from src.card_models import BookEntry, CharacterBook, CharacterCard, parse_character_book

logger = logging.getLogger(__name__)

#: The card fields compared by default.
CARD_FIELDS: tuple[str, ...] = (
    'name',
    'description',
    'personality',
    'scenario',
    'first_mes',
    'mes_example',
    'creator_notes',
    'system_prompt',
    'post_history_instructions',
    'creator',
    'character_version',
    'talkativeness',
    'tags',
    'character_book',
)

#: The lorebook sections compared by default.
LOREBOOK_FIELDS: tuple[str, ...] = ('name', 'entries')

#: The entry attributes compared for a lorebook entry.
ENTRY_ATTRIBUTES: tuple[str, ...] = (
    'key',
    'content',
    'comment',
    'order',
    'position',
    'depth',
    'role',
)


@dataclass
class FieldDiff:
    """One compared field and its two values."""

    field: str
    local: str
    remote: str
    changed: bool


@dataclass
class CardDiff:
    """The differences between a local item and its remote counterpart."""

    card_id: int
    card_name: str
    fields: list[FieldDiff] = field(default_factory=list)
    local_only: bool = False
    remote_only: bool = False

    @property
    def has_changes(self) -> bool:
        """True when anything differs between the two sides."""
        if self.local_only or self.remote_only:
            return True
        return any(f.changed for f in self.fields)

    @property
    def changed_fields(self) -> list[FieldDiff]:
        """The fields that actually differ."""
        return [f for f in self.fields if f.changed]


def _canonical_json(value: Any) -> str:
    """Serialise *value* to a stable JSON string ('' when it is empty)."""
    if value is None:
        return ''
    if isinstance(value, (dict, list, tuple)) and not value:
        return ''
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _as_card(value: Any) -> Optional[CharacterCard]:
    """Return *value* as a :class:`CharacterCard` (None when it is absent)."""
    if value is None:
        return None
    if isinstance(value, CharacterCard):
        return value
    if isinstance(value, dict):
        return CharacterCard.from_spec_dict(value)
    return None


def _card_id(value: Any) -> int:
    """The database id of a card (0 when it is unknown)."""
    raw = value.get('id', 0) if isinstance(value, dict) else getattr(value, 'id', 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _field_value(card: Optional[CharacterCard], name: str) -> str:
    """The comparable text of one card field ('' when the side is absent)."""
    if card is None:
        return ''
    if name == 'character_book':
        return _canonical_json(card.character_book)
    if name == 'tags':
        return _canonical_json(list(card.tags or []))
    if name == 'talkativeness':
        try:
            return f'{float(card.talkativeness):.4f}'
        except (TypeError, ValueError):
            return ''
    value = getattr(card, name, '')
    return str(value or '')


def diff_cards(
    local_card: Any,
    remote_card: Any,
    *,
    fields: Optional[list[str]] = None,
) -> CardDiff:
    """Compare a local card with a remote (SillyTavern) card.

    Args:
        local_card: The local :class:`~src.card_models.CharacterCard` (a
            card dict is accepted as well).
        remote_card: The remote card - a SillyTavern card dict in either the
            V2/V3 spec shape or the flat shape.
        fields: The fields to compare; defaults to :data:`CARD_FIELDS`.

    Returns:
        A :class:`CardDiff` with one :class:`FieldDiff` per compared field.
        A missing side is reported through ``local_only`` / ``remote_only``.
    """
    names = tuple(fields) if fields else CARD_FIELDS
    local = _as_card(local_card)
    remote = _as_card(remote_card)
    diffs: list[FieldDiff] = []
    for name in names:
        local_value = _field_value(local, name)
        remote_value = _field_value(remote, name)
        diffs.append(FieldDiff(
            field=name,
            local=local_value,
            remote=remote_value,
            changed=local_value != remote_value,
        ))
    card_name = ''
    for candidate in (local, remote):
        if candidate is not None and candidate.name:
            card_name = candidate.name
            break
    return CardDiff(
        card_id=_card_id(local_card) or _card_id(remote_card),
        card_name=card_name,
        fields=diffs,
        local_only=local is not None and remote is None,
        remote_only=remote is not None and local is None,
    )


def _entry_attribute(entry: BookEntry, attribute: str) -> str:
    """The comparable text of one lorebook entry attribute."""
    if attribute == 'key':
        return _canonical_json(list(entry.keys or []))
    if attribute == 'content':
        return str(entry.content or '')
    if attribute == 'comment':
        return str(entry.name or '')
    if attribute == 'order':
        return str(int(entry.insertion_order or 0))
    if attribute == 'position':
        return str(entry.position or '')
    if attribute == 'depth':
        return str(int(entry.depth or 0))
    if attribute == 'role':
        return str(int(entry.role or 0))
    return ''


def _entry_label(entry: BookEntry, index: int) -> str:
    """A short, human-readable label for an entry."""
    name = (entry.name or '').strip()
    return name or f'#{index + 1}'


def _entry_summary(entry: BookEntry) -> str:
    """A one-line summary of an entry (used for unmatched entries)."""
    parts = [
        f'{attribute}={_entry_attribute(entry, attribute)}'
        for attribute in ENTRY_ATTRIBUTES
    ]
    return ', '.join(parts)


def _match_entries(
    local_entries: list[BookEntry],
    remote_entries: list[BookEntry],
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Pair the entries of two books.

    Entries with a comment (their name) are matched by that comment first;
    the remaining, unnamed entries are paired in order.  Returns the matched
    index pairs plus the local-only and remote-only indices.
    """
    pairs: list[tuple[int, int]] = []
    used_local: set[int] = set()
    used_remote: set[int] = set()

    local_by_comment: dict[str, int] = {}
    for index, entry in enumerate(local_entries):
        comment = (entry.name or '').strip()
        if comment and comment not in local_by_comment:
            local_by_comment[comment] = index

    for remote_index, entry in enumerate(remote_entries):
        comment = (entry.name or '').strip()
        if not comment:
            continue
        local_index = local_by_comment.get(comment)
        if local_index is None or local_index in used_local:
            continue
        pairs.append((local_index, remote_index))
        used_local.add(local_index)
        used_remote.add(remote_index)

    free_local = [
        i for i, entry in enumerate(local_entries)
        if i not in used_local and not (entry.name or '').strip()
    ]
    free_remote = [
        j for j, entry in enumerate(remote_entries)
        if j not in used_remote and not (entry.name or '').strip()
    ]
    for local_index, remote_index in zip(free_local, free_remote, strict=False):
        pairs.append((local_index, remote_index))
        used_local.add(local_index)
        used_remote.add(remote_index)

    local_only = [i for i in range(len(local_entries)) if i not in used_local]
    remote_only = [j for j in range(len(remote_entries)) if j not in used_remote]
    return pairs, local_only, remote_only


def _entry_diffs(
    local_entries: list[BookEntry],
    remote_entries: list[BookEntry],
) -> list[FieldDiff]:
    """Compare the entries of two books, attribute by attribute."""
    diffs: list[FieldDiff] = []
    pairs, local_only, remote_only = _match_entries(local_entries, remote_entries)

    for local_index, remote_index in pairs:
        local_entry = local_entries[local_index]
        remote_entry = remote_entries[remote_index]
        label = _entry_label(local_entry, local_index) or _entry_label(remote_entry, remote_index)
        for attribute in ENTRY_ATTRIBUTES:
            local_value = _entry_attribute(local_entry, attribute)
            remote_value = _entry_attribute(remote_entry, attribute)
            diffs.append(FieldDiff(
                field=f'entry[{label}].{attribute}',
                local=local_value,
                remote=remote_value,
                changed=local_value != remote_value,
            ))

    for index in local_only:
        entry = local_entries[index]
        diffs.append(FieldDiff(
            field=f'entry[{_entry_label(entry, index)}]',
            local=_entry_summary(entry),
            remote='',
            changed=True,
        ))
    for index in remote_only:
        entry = remote_entries[index]
        diffs.append(FieldDiff(
            field=f'entry[{_entry_label(entry, index)}]',
            local='',
            remote=_entry_summary(entry),
            changed=True,
        ))
    return diffs


def diff_lorebooks(
    local_book: Any,
    remote_book: Any,
    *,
    fields: Optional[list[str]] = None,
) -> CardDiff:
    """Compare two lorebooks.

    Args:
        local_book: The local :class:`~src.card_models.CharacterBook` (a raw
            book dict is accepted as well).
        remote_book: The remote book (a ``CharacterBook`` or a book dict).
        fields: The sections to compare; defaults to
            :data:`LOREBOOK_FIELDS` (``'name'`` and ``'entries'``).

    Returns:
        A :class:`CardDiff` whose fields describe the book name and every
        differing entry attribute.
    """
    names = tuple(fields) if fields else LOREBOOK_FIELDS
    local = (
        local_book if isinstance(local_book, CharacterBook)
        else parse_character_book(local_book if isinstance(local_book, dict) else None)
    )
    remote = (
        remote_book if isinstance(remote_book, CharacterBook)
        else parse_character_book(remote_book if isinstance(remote_book, dict) else None)
    )
    local_present = local_book is not None
    remote_present = remote_book is not None

    diffs: list[FieldDiff] = []
    if 'name' in names:
        local_name = (local.name or '') if local_present else ''
        remote_name = (remote.name or '') if remote_present else ''
        diffs.append(FieldDiff(
            field='name', local=local_name, remote=remote_name,
            changed=local_name != remote_name,
        ))
    if 'entries' in names:
        local_entries = local.entries if local_present and local else []
        remote_entries = remote.entries if remote_present and remote else []
        diffs.extend(_entry_diffs(local_entries, remote_entries))

    card_name = ''
    for candidate in (local if local_present else None, remote if remote_present else None):
        if candidate is not None and candidate.name:
            card_name = candidate.name
            break
    return CardDiff(
        card_id=0,
        card_name=card_name,
        fields=diffs,
        local_only=local_present and not remote_present,
        remote_only=remote_present and not local_present,
    )


def summarize_diffs(diffs: list[CardDiff]) -> str:
    """Summarise a list of diffs as a one-line, human-readable report.

    Args:
        diffs: The diffs to summarise.

    Returns:
        A sentence describing how many items were compared, how many differ,
        how many exist on only one side, and how many fields differ in
        total.  ``'No differences.'`` for an empty list.
    """
    items = list(diffs or [])
    if not items:
        return 'No differences.'
    changed = [d for d in items if d.has_changes and not d.local_only and not d.remote_only]
    local_only = [d for d in items if d.local_only]
    remote_only = [d for d in items if d.remote_only]
    field_count = sum(1 for d in items for f in d.fields if f.changed)
    return (
        f'{len(items)} item(s) compared: {len(changed)} with changes, '
        f'{len(local_only)} local only, {len(remote_only)} remote only '
        f'({field_count} differing field(s)).'
    )

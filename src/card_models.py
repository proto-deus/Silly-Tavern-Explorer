from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional


def _str(val: Any, default: str = '') -> str:
    if isinstance(val, str):
        return val
    if val is None:
        return default
    return str(val)


def _list(val: Any) -> list:
    if isinstance(val, list):
        return list(val)
    return []


def _finite_float(val: Any) -> float | None:
    """Coerce *val* to a finite float, or None if it isn't one.

    Python's ``json`` accepts the bare tokens ``NaN``/``Infinity`` and
    ``float('nan')`` parses from a string, but neither survives a round-trip
    through a strict JSON parser: ``json.dumps`` emits the same non-standard
    tokens, which SillyTavern (and every other consumer) rejects. Anything
    non-finite is treated as absent.
    """
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
    elif isinstance(val, str):
        try:
            f = float(val)
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _float(val: Any, default: float = 0.5) -> float:
    f = _finite_float(val)
    return default if f is None else f


def _bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    # Some toolchains serialize booleans as 0/1 ints.
    if val in (0, 1):
        return bool(val)
    return default


def _int(val: Any, default: int = 0) -> int:
    f = _finite_float(val)
    if f is None:
        return default
    try:
        return int(f)
    except (ValueError, OverflowError):
        return default


def _opt_int(val: Any) -> Optional[int]:
    """Like ``_int`` but returns None for values that don't coerce to an int.

    A corrupt ``"scan_depth": "auto"`` must round-trip as absent rather than
    being silently rewritten to ``0``.
    """
    if val is None:
        return None
    f = _finite_float(val)
    if f is None:
        return None
    try:
        return int(f)
    except (ValueError, OverflowError):
        return None


@dataclass
class BookEntry:
    """A single entry in a character book (lorebook)."""

    name: str = ''
    keys: list[str] = field(default_factory=list)
    content: str = ''
    extensions: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    insertion_order: int = 0
    case_sensitive: bool = False
    match_whole_words: bool = False
    depth: int = 4
    position: str = 'before_char'

    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'keys': list(self.keys),
            'content': self.content,
            'extensions': dict(self.extensions),
            'enabled': self.enabled,
            'insertion_order': self.insertion_order,
            'case_sensitive': self.case_sensitive,
            'match_whole_words': self.match_whole_words,
            'depth': self.depth,
            'position': self.position,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> BookEntry:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            name=_str(raw.get('name', '')),
            keys=_list(raw.get('keys', [])),
            content=_str(raw.get('content', '')),
            extensions=dict(raw.get('extensions')) if isinstance(raw.get('extensions'), dict) else {},
            enabled=_bool(raw.get('enabled', True), default=True),
            insertion_order=_int(raw.get('insertion_order', 0)),
            case_sensitive=_bool(raw.get('case_sensitive', False)),
            match_whole_words=_bool(raw.get('match_whole_words', False)),
            depth=_int(raw.get('depth', 4), default=4),
            position=_str(raw.get('position', 'before_char')),
        )


@dataclass
class CharacterBook:
    """A character book (lorebook) attached to a character card."""

    name: str = ''
    description: str = ''
    scan_depth: Optional[int] = None
    token_budget: Optional[int] = None
    recursive_scanning: bool = False
    extensions: dict[str, Any] = field(default_factory=dict)
    entries: list[BookEntry] = field(default_factory=list)
    # Unmodeled top-level fields from the source book (ST world-info extras
    # and third-party keys).  Preserved verbatim through round-trips so
    # syncing a book never silently deletes fields the app doesn't model.
    extra_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # Start from the preserved unmodeled fields so unknown keys survive.
        data: dict[str, Any] = dict(self.extra_data)
        data.update({
            'name': self.name,
            'description': self.description,
            'extensions': dict(self.extensions),
            'entries': [e.to_dict() for e in self.entries],
        })
        if self.scan_depth is not None:
            data['scan_depth'] = self.scan_depth
        if self.token_budget is not None:
            data['token_budget'] = self.token_budget
        if self.recursive_scanning:
            data['recursive_scanning'] = self.recursive_scanning
        return data

    @classmethod
    def from_dict(cls, raw: dict) -> CharacterBook:
        if not isinstance(raw, dict):
            return cls()
        known = {
            'name', 'description', 'scan_depth', 'token_budget',
            'recursive_scanning', 'extensions', 'entries',
        }
        extra = {
            k: v for k, v in raw.items()
            if k not in known and not callable(v)
        }
        raw_entries = raw.get('entries', [])
        # Some exporters key entries by uid instead of using a list.
        if isinstance(raw_entries, dict):
            def _key_order(item: tuple) -> tuple:
                k = str(item[0])
                try:
                    return (0, float(k), '')
                except ValueError:
                    return (1, 0.0, k)
            raw_entries = [
                v for _, v in sorted(raw_entries.items(), key=_key_order)
                if isinstance(v, dict)
            ]
        elif not isinstance(raw_entries, list):
            raw_entries = []
        entries = [BookEntry.from_dict(e) if isinstance(e, dict) else BookEntry() for e in raw_entries]
        ext = raw.get('extensions', {})
        if not isinstance(ext, dict):
            ext = {}
        else:
            ext = dict(ext)
        return cls(
            name=_str(raw.get('name', '')),
            description=_str(raw.get('description', '')),
            scan_depth=_opt_int(raw.get('scan_depth')),
            token_budget=_opt_int(raw.get('token_budget')),
            recursive_scanning=_bool(raw.get('recursive_scanning', False)),
            extensions=ext,
            entries=entries,
            extra_data=extra,
        )


def parse_character_book(raw: dict | None) -> CharacterBook:
    """Parse a raw character book dict into a CharacterBook dataclass.

    Returns an empty CharacterBook when *raw* is None or not a dict.
    """
    if not isinstance(raw, dict):
        return CharacterBook()
    return CharacterBook.from_dict(raw)


@dataclass
class CharacterCard:
    name: str = ''
    description: str = ''
    personality: str = ''
    scenario: str = ''
    first_mes: str = ''
    mes_example: str = ''
    creator_notes: str = ''
    system_prompt: str = ''
    post_history_instructions: str = ''
    alternate_greetings: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    creator: str = ''
    character_version: str = ''
    talkativeness: float = 0.5
    fav: bool = False
    extensions: dict[str, Any] = field(default_factory=dict)
    character_book: Optional[dict[str, Any]] = None
    spec: str = 'chara_card_v2'
    spec_version: str = '2.0'
    create_date: str = ''
    # Unmodeled fields from the source card's ``data`` dict (V3 extras such
    # as ``assets``, ``nickname``, ``group_only_greetings``, plus arbitrary
    # third-party extensions).  Preserved verbatim through round-trips so
    # editing a card never silently deletes fields the app doesn't model.
    extra_data: dict[str, Any] = field(default_factory=dict)

    # Computed / runtime fields (not serialized)
    token_count: int = 0
    source_path: str = ''

    _KNOWN_DATA_KEYS = frozenset({
        'name', 'description', 'personality', 'scenario', 'first_mes',
        'mes_example', 'creator_notes', 'system_prompt',
        'post_history_instructions', 'alternate_greetings', 'tags',
        'creator', 'character_version', 'extensions', 'character_book',
        'create_date',
    })

    def __post_init__(self) -> None:
        if not self.create_date:
            self.create_date = date.today().isoformat()
        # A card carrying NaN/Infinity (accepted by Python's json parser and
        # reachable via a hand-edited file) would be re-emitted as the bare
        # tokens NaN/Infinity, which every strict JSON parser - including
        # SillyTavern's - rejects, making the card unreadable elsewhere.
        if not math.isfinite(self.talkativeness):
            self.talkativeness = 0.5

    def to_spec_dict(self) -> dict:
        # Start from the preserved unmodeled fields so unknown V3 extras
        # survive the round-trip, then overlay the modeled fields.
        data: dict[str, Any] = dict(self.extra_data)
        data.update({
            'name': self.name,
            'description': self.description,
            'personality': self.personality,
            'scenario': self.scenario,
            'first_mes': self.first_mes,
            'mes_example': self.mes_example,
            'creator_notes': self.creator_notes,
            'system_prompt': self.system_prompt,
            'post_history_instructions': self.post_history_instructions,
            'alternate_greetings': list(self.alternate_greetings),
            'tags': list(self.tags),
            'creator': self.creator,
            'character_version': self.character_version,
            'create_date': self.create_date,
            'extensions': {
                # Spread existing extensions first, then override with
                # the card-level fields so they always reflect current values.
                **{k: v for k, v in self.extensions.items() if k not in ('talkativeness', 'fav')},
                # ``or`` would rewrite a legitimate 0.0 ("never talk") to 0.5.
                'talkativeness': 0.5 if _finite_float(self.talkativeness) is None else self.talkativeness,
                'fav': self.fav,
            },
        })
        # Legacy V2 mirror keys: keep values that arrived in extra_data
        # (round-trip fidelity) and only fall back to the spec defaults.
        data.setdefault('avatar', 'none')
        data.setdefault('chat', '')
        if self.character_book is not None:
            data['character_book'] = self.character_book
        # A field can be mutated after construction (an extension editor, a
        # generated card), so re-check the float rather than trusting
        # __post_init__.
        talkativeness = 0.5 if _finite_float(self.talkativeness) is None else self.talkativeness

        return {
            'spec': self.spec,
            'spec_version': self.spec_version,
            'name': self.name,
            'description': self.description,
            'personality': self.personality,
            'scenario': self.scenario,
            'first_mes': self.first_mes,
            'mes_example': self.mes_example,
            'avatar': 'none',
            'chat': '',
            'talkativeness': talkativeness,
            'fav': self.fav,
            'tags': list(self.tags),
            'create_date': self.create_date,
            'data': data,
        }

    @classmethod
    def from_spec_dict(cls, raw: dict, source_path: str = '') -> CharacterCard:
        if not isinstance(raw, dict):
            return cls(source_path=source_path)

        # ``data={}`` is falsy but still a V2/V3 payload; only fall back to
        # the V1 shape when ``data`` is absent or not a dict at all.
        if raw.get('spec') and isinstance(raw.get('data'), dict):
            d = raw['data']
            ext = d.get('extensions', {})
            if not isinstance(ext, dict):
                ext = {}
            extra = {
                k: v for k, v in d.items()
                if k not in cls._KNOWN_DATA_KEYS and not callable(v)
            }
            card = cls(
                name=_str(d.get('name', raw.get('name', ''))),
                description=_str(d.get('description', '')),
                personality=_str(d.get('personality', '')),
                scenario=_str(d.get('scenario', '')),
                first_mes=_str(d.get('first_mes', '')),
                mes_example=_str(d.get('mes_example', '')),
                creator_notes=_str(d.get('creator_notes', '')),
                system_prompt=_str(d.get('system_prompt', '')),
                post_history_instructions=_str(d.get('post_history_instructions', '')),
                alternate_greetings=_list(d.get('alternate_greetings', [])),
                tags=_list(d.get('tags', [])),
                creator=_str(d.get('creator', '')),
                character_version=_str(d.get('character_version', '')),
                talkativeness=_float(ext.get('talkativeness', raw.get('talkativeness', 0.5))),
                fav=_bool(ext.get('fav', raw.get('fav', False))),
                extensions=ext,
                character_book=d.get('character_book') if isinstance(d.get('character_book'), dict) else None,
                spec=_str(raw.get('spec', 'chara_card_v2')),
                spec_version=_str(raw.get('spec_version', '2.0')),
                create_date=_str(raw.get('create_date', d.get('create_date', ''))),
                extra_data=extra,
            )
        else:
            # V1 (or an unlabelled payload): every V1 field must be read, not
            # just the nine the legacy format nominally requires. Dropping
            # creator_notes/system_prompt/alternate_greetings/extensions here
            # silently destroyed them on the first open+save cycle.
            ext = raw.get('extensions', {})
            if not isinstance(ext, dict):
                ext = {}
            extra = {
                k: v for k, v in raw.items()
                if k not in cls._KNOWN_DATA_KEYS and not callable(v)
            }
            card = cls(
                name=_str(raw.get('name', '')),
                description=_str(raw.get('description', '')),
                personality=_str(raw.get('personality', '')),
                scenario=_str(raw.get('scenario', '')),
                first_mes=_str(raw.get('first_mes', '')),
                mes_example=_str(raw.get('mes_example', '')),
                creator_notes=_str(raw.get('creator_notes', '')),
                system_prompt=_str(raw.get('system_prompt', '')),
                post_history_instructions=_str(raw.get('post_history_instructions', '')),
                alternate_greetings=_list(raw.get('alternate_greetings', [])),
                tags=_list(raw.get('tags', [])),
                creator=_str(raw.get('creator', '')),
                character_version=_str(raw.get('character_version', '')),
                talkativeness=_float(ext.get('talkativeness', raw.get('talkativeness', 0.5))),
                fav=_bool(ext.get('fav', raw.get('fav', False))),
                extensions=ext,
                character_book=(
                    raw.get('character_book')
                    if isinstance(raw.get('character_book'), dict) else None
                ),
                spec='chara_card_v1',
                spec_version='1.0',
                create_date=_str(raw.get('create_date', '')),
                extra_data=extra,
            )

        card.source_path = source_path
        return card

    def summary_text(self) -> str:
        parts = []
        if self.description:
            parts.append(_str(self.description)[:300])
        if self.personality:
            parts.append(f"Personality: {_str(self.personality)}")
        if self.scenario:
            parts.append(f"Scenario: {_str(self.scenario)}")
        return '\n\n'.join(parts) if parts else 'No description available.'

    def full_text_for_tokens(self) -> str:
        return '\n'.join(filter(None, [
            _str(self.name),
            _str(self.description),
            _str(self.personality),
            _str(self.scenario),
            _str(self.first_mes),
            _str(self.mes_example),
            _str(self.system_prompt),
            _str(self.post_history_instructions),
        ]))


def build_duplicate_card(base: CharacterCard) -> CharacterCard:
    """Create a clone of *base* with a '(copy)' name suffix.

    The clone's ``fav`` is cleared, ``create_date`` is reset to today, and
    ``source_path`` is emptied (the caller must set it to the new file path).
    Pure function so duplication logic can be unit-tested without Qt.
    """
    return CharacterCard(
        name=f"{base.name} (copy)",
        description=base.description,
        personality=base.personality,
        scenario=base.scenario,
        first_mes=base.first_mes,
        mes_example=base.mes_example,
        creator_notes=base.creator_notes,
        system_prompt=base.system_prompt,
        post_history_instructions=base.post_history_instructions,
        alternate_greetings=list(base.alternate_greetings),
        tags=list(base.tags),
        creator=base.creator,
        character_version=base.character_version,
        talkativeness=base.talkativeness,
        fav=False,
        extensions=dict(base.extensions) if base.extensions else {},
        # Deep copies: these are nested structures, and an in-place edit of the
        # duplicate's book must not reach back into the original card.
        character_book=copy.deepcopy(base.character_book),
        spec=base.spec,
        spec_version=base.spec_version,
        create_date='',
        extra_data=copy.deepcopy(base.extra_data) if base.extra_data else {},
    )

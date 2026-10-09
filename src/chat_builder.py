from __future__ import annotations

import random
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional

from src.card_models import BookEntry, CharacterBook, CharacterCard, parse_character_book

# SillyTavern macro substitution: {{user}} and {{char}} (case-insensitive).
_USER_RE = re.compile(r'\{\{\s*user\s*\}\}', re.IGNORECASE)
_CHAR_RE = re.compile(r'\{\{\s*char\s*\}\}', re.IGNORECASE)

# Dynamic SillyTavern macros evaluated per substitution pass.
_DATE_RE = re.compile(r'\{\{\s*date\s*\}\}', re.IGNORECASE)
_TIME_RE = re.compile(r'\{\{\s*time\s*\}\}', re.IGNORECASE)
_DATETIME_RE = re.compile(r'\{\{\s*datetime\s*\}\}', re.IGNORECASE)
_RANDOM_RE = re.compile(r'\{\{\s*(?:random|pick)\s*:\s*([^{}]*)\}\}', re.IGNORECASE)
_ROLL_RE = re.compile(r'\{\{\s*roll\s*:\s*([^{}]*)\}\}', re.IGNORECASE)
# ``NdM+K`` / ``d20`` / ``2d6-1`` dice notation for {{roll:...}}.
_ROLL_SPEC_RE = re.compile(r'^\s*(\d*)\s*[dD]\s*(\d+)\s*(?:([+-])\s*(\d+))?\s*$')

# ``{{char}}`` / ``{{user}}`` line prefixes inside mes_example blocks.
_EXAMPLE_SPEAKER_RE = re.compile(
    r'^\s*(\{\{\s*char\s*\}\}|\{\{\s*user\s*\}\})\s*:\s?', re.IGNORECASE
)

# Lorebook defaults when the book doesn't specify them.
DEFAULT_LOREBOOK_SCAN_DEPTH = 4
# Hard cap on recursive rescans so mutually-triggering entries can't loop.
MAX_LOREBOOK_PASSES = 5


def _sub_literal(pattern: 're.Pattern | str', replacement: str, text: str) -> str:
    """re.sub treating *replacement* as literal text.

    Passing the value as a replacement *string* would interpret backslash
    sequences (``C:\\Users`` raises ``bad escape \\U``; ``\\n`` silently
    becomes a newline).  A lambda replacement is always literal.
    """
    regex = re.compile(pattern, re.IGNORECASE) if isinstance(pattern, str) else pattern
    return regex.sub(lambda _m: replacement, text)


def substitute_macros(
    text: str,
    user_name: str,
    char_name: str,
    custom_macros: dict[str, str] | None = None,
    escape_for_format: bool = False,
    rng: Optional[random.Random] = None,
    now: Optional[datetime] = None,
) -> str:
    """Replace ``{{user}}`` / ``{{char}}`` macros in *text*.

    Case-insensitive and tolerant of internal whitespace (``{{ user }}``).
    *custom_macros* maps additional macro names (without braces) to values
    and is applied after the built-in name macros.  Values are substituted
    literally (backslashes have no special meaning).  Pure function so it
    can be unit-tested without Qt.

    SillyTavern's dynamic macros are evaluated after the custom ones (so a
    custom ``{{time}}`` wins): ``{{time}}``, ``{{date}}``, ``{{datetime}}``,
    ``{{random:a|b|c}}`` / ``{{pick:a, b, c}}`` and dice rolls
    ``{{roll:2d6+3}}``.  *rng* / *now* make the random and clock values
    injectable for tests.

    When *escape_for_format* is set the replacement values have their braces
    doubled: the caller is going to run ``str.format_map`` over the result,
    and without escaping a card named ``{description}`` would silently have
    its own name replaced by its description (third-party card text must not
    become template syntax).
    """
    if not text:
        return ''

    def _value(v) -> str:
        s = v if isinstance(v, str) else str(v)
        return s.replace('{', '{{').replace('}', '}}') if escape_for_format else s

    text = _sub_literal(_USER_RE, _value(user_name or ''), text)
    text = _sub_literal(_CHAR_RE, _value(char_name or ''), text)
    if custom_macros:
        for name, value in custom_macros.items():
            key = str(name).strip()
            if not key:
                continue
            pattern = r'\{\{\s*' + re.escape(key) + r'\s*\}\}'
            text = _sub_literal(pattern, _value(value), text)
    return _sub_dynamic_macros(text, escape_for_format=escape_for_format, rng=rng, now=now)


def _sub_dynamic_macros(
    text: str,
    escape_for_format: bool = False,
    rng: Optional[random.Random] = None,
    now: Optional[datetime] = None,
) -> str:
    """Evaluate ``{{time}}`` / ``{{date}}`` / ``{{random}}`` / ``{{roll}}``."""
    if not text:
        return ''
    moment = now or datetime.now()
    dice = rng or random.Random()

    def _value(v) -> str:
        s = v if isinstance(v, str) else str(v)
        return s.replace('{', '{{').replace('}', '}}') if escape_for_format else s

    text = _DATE_RE.sub(lambda _m: _value(moment.strftime('%Y-%m-%d')), text)
    text = _TIME_RE.sub(lambda _m: _value(moment.strftime('%H:%M')), text)
    text = _DATETIME_RE.sub(lambda _m: _value(moment.strftime('%Y-%m-%d %H:%M')), text)

    def _replace_random(m: 're.Match') -> str:
        options = _macro_options(m.group(1))
        if not options:
            return ''
        return _value(dice.choice(options))

    text = _RANDOM_RE.sub(_replace_random, text)

    def _replace_roll(m: 're.Match') -> str:
        rolled = _roll_macro(m.group(1), dice)
        return m.group(0) if rolled is None else _value(str(rolled))

    return _ROLL_RE.sub(_replace_roll, text)


def _macro_options(payload: str) -> list[str]:
    """Split a ``{{random:a|b}}`` / ``{{pick: a, b}}`` option list.

    Pipes win when present (SillyTavern's ``random`` separator); otherwise
    commas split the options.  Surrounding quotes and whitespace are stripped
    and empty options dropped.
    """
    raw = payload or ''
    parts = raw.split('|') if '|' in raw else raw.split(',')
    options: list[str] = []
    for part in parts:
        item = part.strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in ('"', "'"):
            item = item[1:-1].strip()
        if item:
            options.append(item)
    return options


def _roll_macro(payload: str, rng: random.Random) -> Optional[int]:
    """Evaluate a ``{{roll: NdM±K}}`` dice expression (None when invalid)."""
    match = _ROLL_SPEC_RE.match(payload or '')
    if not match:
        return None
    count = int(match.group(1) or '1')
    sides = int(match.group(2))
    if sides < 1 or count < 1 or count > 100:
        return None
    total = sum(rng.randint(1, sides) for _ in range(count))
    if match.group(3):
        bonus = int(match.group(4))
        total += bonus if match.group(3) == '+' else -bonus
    return total


def clean_generated_text(text: str, user_name: str = 'User', char_name: str = '') -> str:
    """Resolve ``{{user}}`` / ``{{char}}`` macros that leaked into model output.

    Models occasionally echo the prompt's macros back; storing them verbatim
    makes re-rendering show raw braces and re-substituting later would rewrite
    history.  Pure function.
    """
    if not text:
        return ''
    out = _sub_literal(_USER_RE, user_name or '', text)
    return _sub_literal(_CHAR_RE, char_name or '', out)


def _card_fields(card: CharacterCard) -> dict[str, Any]:
    return {
        'name': card.name or '',
        'description': card.description or '',
        'personality': card.personality or '',
        'scenario': card.scenario or '',
        'first_mes': card.first_mes or '',
    }


def resolve_system_prompt(
    card: CharacterCard,
    user_name: str = 'User',
    chat_template: str | None = None,
    custom_macros: dict[str, str] | None = None,
) -> str:
    """Build the system prompt for a chat with *card*.

    Prefers the card's own ``system_prompt`` (with macro substitution).
    Falls back to the chat template (loaded from settings when
    *chat_template* is None) when the card doesn't define one.

    Macros (``{{user}}`` / ``{{char}}``) are substituted *before* the
    ``{name}``-style format placeholders so the double-brace macros aren't
    consumed by ``str.format`` escaping.
    """
    if card.system_prompt and card.system_prompt.strip():
        # Raw user text — only macro-sub (don't format_map: might have literal braces).
        return substitute_macros(card.system_prompt, user_name, card.name, custom_macros)

    if chat_template is not None:
        raw = chat_template
    else:
        from src.ai_prompts import load_prompt
        raw = load_prompt('chat_system')

    # Escape braces in the macro values: ``substitute`` below runs
    # format_map over this string, and card/user text must not become
    # template syntax.
    macroed = substitute_macros(
        raw, user_name, card.name, custom_macros, escape_for_format=True,
    )
    from src.ai_prompts import substitute
    return substitute(macroed, **_card_fields(card))


def build_initial_messages(
    card: CharacterCard,
    user_name: str = 'User',
    custom_macros: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return the initial message history (excluding the system prompt).

    The card's ``first_mes`` becomes the first assistant message, with
    ``{{user}}`` / ``{{char}}`` macros substituted.  Returns an empty list
    when the card has no first message.
    """
    if not card.first_mes or not card.first_mes.strip():
        return []
    content = substitute_macros(card.first_mes, user_name, card.name, custom_macros)
    return [{'role': 'assistant', 'content': content}]


def _message_ts() -> str:
    """Timestamp stamped onto a message at creation time (not render time)."""
    return datetime.now().isoformat(timespec='seconds')


def append_message(
    messages: list[dict[str, str]],
    role: str,
    content: str,
    user_name: str = 'User',
    char_name: str = '',
    custom_macros: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return a new message list with *content* appended under *role*.

    Macros are substituted for user/assistant messages.  Pure function.
    Each message carries a ``ts`` stamp so re-rendering the chat does not
    rewrite every visible timestamp to "now".
    """
    substituted = substitute_macros(content, user_name, char_name, custom_macros)
    new_list = list(messages)
    new_list.append({'role': role, 'content': substituted, 'ts': _message_ts()})
    return new_list


def make_message(role: str, content: str, attachments: list[dict] | None = None) -> dict:
    """Build a message dict, attaching an optional attachment list.

    Attachments are stored verbatim (never macro-substituted) so file contents
    are passed through unchanged.  Pure function.
    """
    msg: dict = {'role': role, 'content': content or '', 'ts': _message_ts()}
    if attachments:
        msg['attachments'] = [dict(a) for a in attachments]
    return msg


# ---- Example-dialogue (mes_example) parsing ----

def _split_example_blocks(mes_example: str) -> list[str]:
    """Split a card's ``mes_example`` into per-``<START>`` blocks."""
    if not mes_example:
        return []
    blocks = re.split(r'<\s*START\s*>', mes_example, flags=re.IGNORECASE)
    return [b.strip('\n') for b in blocks if b and b.strip()]


def parse_example_messages(
    mes_example: str,
    user_name: str,
    char_name: str,
    custom_macros: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Convert a card's ``mes_example`` into few-shot API messages.

    Each ``<START>`` block becomes one or more messages: lines starting with
    ``{{user}}:`` map to the ``user`` role and ``{{char}}:`` lines map to
    ``assistant``.  Continuation lines without a recognised speaker prefix are
    appended to the previous message (narration belongs to the last speaker).
    Macros are substituted.  Blocks with no recognised speaker produce a
    single ``assistant`` message so narration-only examples still guide style.
    Pure function.
    """
    messages: list[dict[str, str]] = []
    for block in _split_example_blocks(mes_example or ''):
        speaker: str | None = None
        buffer: list[str] = []

        def flush() -> None:
            text = '\n'.join(buffer).strip()
            if text:
                messages.append({
                    'role': speaker or 'assistant',
                    'content': text,
                })

        for line in block.splitlines():
            match = _EXAMPLE_SPEAKER_RE.match(line)
            if match:
                flush()
                raw_speaker = match.group(1).lower()
                speaker = 'user' if 'user' in raw_speaker else 'assistant'
                buffer = [line[match.end():]]
            else:
                buffer.append(line)
        flush()

    return [
        {'role': m['role'], 'content': substitute_macros(
            m['content'], user_name, char_name, custom_macros,
        )}
        for m in messages
    ]


def _persona_block(persona: str, user_name: str = 'User') -> str:
    return f"[User persona]\n{user_name or 'User'} is: {persona.strip()}"


def apply_persona(system: str, persona: str, user_name: str = 'User') -> str:
    """Append a ``[User persona]`` block to *system* when *persona* is set.

    Mirrors :func:`apply_memory`: pure, returns the input unchanged when the
    persona is blank.
    """
    if not persona or not persona.strip():
        return system or ''
    block = _persona_block(persona, user_name)
    if system and system.strip():
        return f"{system}\n\n{block}"
    return block


def _attachment_text_block(attachment: dict) -> str:
    name = attachment.get('name', 'file')
    data = attachment.get('data', '')
    return f"[Attached file: {name}]\n{data}"


def history_for_api(messages: list[dict]) -> list[dict]:
    """Convert internal chat messages into API-ready message dicts.

    Internal fields (``attachments``, and any ``id``/``ts`` markers) are
    stripped.  Text attachments are prepended to the message as a labelled
    block; image attachments turn the message ``content`` into an
    OpenAI-compatible multimodal list of text + ``image_url`` parts.  Pure
    function so it can be unit-tested without Qt.
    """
    result: list[dict] = []
    for m in messages or []:
        role = m.get('role', 'user')
        content = m.get('content', '') or ''
        attachments = m.get('attachments') or []

        text_blocks = [
            _attachment_text_block(a)
            for a in attachments
            if a.get('kind') == 'text'
        ]
        images = [a for a in attachments if a.get('kind') == 'image']

        if images:
            combined = content
            if text_blocks:
                combined = '\n\n'.join(text_blocks + ([content] if content else []))
            parts: list[dict] = []
            # Some OpenAI-compatible backends reject an empty text part;
            # omit it entirely for image-only messages.
            if combined:
                parts.append({'type': 'text', 'text': combined})
            for img in images:
                mime = img.get('mime', 'image/jpeg')
                data = img.get('data', '')
                parts.append({
                    'type': 'image_url',
                    'image_url': {'url': f'data:{mime};base64,{data}'},
                })
            result.append({'role': role, 'content': parts})
        elif text_blocks:
            combined = '\n\n'.join(text_blocks + ([content] if content else []))
            result.append({'role': role, 'content': combined})
        else:
            result.append({'role': role, 'content': content})
    return result


def apply_memory(system: str, memory: str) -> str:
    """Append a chat-memory block to *system* when *memory* is non-empty.

    Pure function.  Returns the original *system* untouched when memory is
    blank.
    """
    if not memory or not memory.strip():
        return system or ''
    block = f"[Chat memory]\n{memory.strip()}"
    if system and system.strip():
        return f"{system}\n\n{block}"
    return block


def _memory_section(memories: list[dict], limit: int = 20) -> Optional[str]:
    """Build the ``[Chat memory]`` block, or None when nothing to include."""
    items = [
        m for m in (memories or [])
        if isinstance(m, dict) and (m.get('content') or '').strip()
    ]
    if not items:
        return None
    recent = items[-limit:]
    blocks = [f"- {m['content'].strip()}" for m in recent]
    return "[Chat memory]\n" + '\n'.join(blocks)


def apply_memories(system: str, memories: list[dict], limit: int = 20) -> str:
    """Append the most recent *limit* memory entries to *system*.

    Each entry contributes one bullet point; older entries are dropped so
    the prompt doesn't grow unboundedly.  Entries without ``content`` are
    silently skipped.  Pure function.
    """
    section = _memory_section(memories, limit)
    if section is None:
        return system or ''
    if system and system.strip():
        return f"{system}\n\n{section}"
    return section


# ---- Lorebook (world info) injection ----

def _entry_matches(entry: BookEntry, scan_text: str) -> bool:
    """Return True when any of *entry*'s keys match *scan_text*.

    Honours ``case_sensitive`` and ``match_whole_words``.  Keys are matched
    as literal text (regex metacharacters have no special meaning).
    """
    keys = [k for k in (entry.keys or []) if isinstance(k, str) and k.strip()]
    if not keys:
        return False
    flags = 0 if entry.case_sensitive else re.IGNORECASE
    for key in keys:
        escaped = re.escape(key.strip())
        if entry.match_whole_words:
            pattern = r'\b' + escaped + r'\b'
        else:
            pattern = escaped
        if re.search(pattern, scan_text, flags):
            return True
    return False


def match_book_entries(
    book: CharacterBook,
    scan_text: str,
    exclude: set[int] | None = None,
) -> list[tuple[int, BookEntry]]:
    """Return ``(index, entry)`` pairs whose keys match *scan_text*.

    Only enabled entries with non-empty content participate.  Results are
    ordered by ``insertion_order`` (ascending = higher priority), with list
    position as a stable tiebreaker.  *exclude* suppresses already-activated
    indices so recursive passes only surface new entries.  Pure function.
    """
    skip = exclude or set()
    matches: list[tuple[int, BookEntry]] = []
    for idx, entry in enumerate(book.entries):
        if idx in skip:
            continue
        if not entry.enabled:
            continue
        if not entry.content or not entry.content.strip():
            continue
        if _entry_matches(entry, scan_text):
            matches.append((idx, entry))
    matches.sort(key=lambda pair: pair[1].insertion_order)
    return matches


def _lorebook_scan_text(messages: list[dict], scan_depth: int) -> str:
    """Concatenate the most recent *scan_depth* message contents."""
    recent = [
        (m.get('content') or '')
        for m in (messages or [])
        if isinstance(m, dict)
    ][-max(0, scan_depth):]
    return '\n'.join(recent)


def _activate_book_entries(
    book: CharacterBook,
    messages: list[dict],
    token_fn=None,
) -> list[BookEntry]:
    """Activate entries from a single book, honouring recursion+budget.

    The initial scan window is the last ``scan_depth`` messages (the book's
    value, defaulting to :data:`DEFAULT_LOREBOOK_SCAN_DEPTH`).  When the
    book enables ``recursive_scanning``, activated entries' content joins
    the scan text and matching repeats until no new entries fire or
    :data:`MAX_LOREBOOK_PASSES` sweeps have run.  When the book defines a
    ``token_budget``, lowest-priority (highest insertion-order) entries are
    dropped until the total fits.  Pure function (token counting injectable).
    """
    candidates = [e for e in book.entries if e.enabled and (e.content or '').strip()]
    if not candidates:
        return []

    if token_fn is None:
        from src.token_counter import count_tokens as token_fn

    scan_depth = book.scan_depth if (book.scan_depth or 0) > 0 else DEFAULT_LOREBOOK_SCAN_DEPTH
    scan_text = _lorebook_scan_text(messages, scan_depth)

    max_passes = MAX_LOREBOOK_PASSES if book.recursive_scanning else 1
    activated: dict[int, BookEntry] = {}
    for _ in range(max_passes):
        matches = match_book_entries(book, scan_text, exclude=set(activated))
        if not matches:
            break
        for idx, entry in matches:
            activated[idx] = entry
            scan_text += '\n' + (entry.content or '')
        if not book.recursive_scanning:
            break

    ordered = sorted(activated.values(), key=lambda e: e.insertion_order)

    budget = book.token_budget or 0
    if budget > 0:
        # Drop lowest-priority (highest insertion-order) entries until the
        # activated set fits the book's token budget.
        costs = [token_fn(e.content or '') for e in ordered]
        total = sum(costs)
        count = len(ordered)
        while count > 0 and total > budget:
            count -= 1
            total -= costs[count]
        return ordered[:count]
    return ordered


def collect_lorebook_entries(
    card: CharacterCard,
    messages: list[dict],
    token_fn=None,
    extra_books: Optional[list[CharacterBook]] = None,
) -> list[BookEntry]:
    """Activate lorebook entries for a chat turn across all sources.

    The card's own character book is scanned first, then each book in
    *extra_books* in order.  Scan depth, recursion, and the token budget
    are applied per source book (a book's settings never leak into another
    book's activation), and the results are concatenated.  Pure function
    (token counting injectable).
    """
    books: list[CharacterBook] = []
    if card is not None:
        books.append(parse_character_book(card.character_book))
    for extra in extra_books or []:
        if isinstance(extra, CharacterBook):
            books.append(extra)
    result: list[BookEntry] = []
    for book in books:
        result.extend(_activate_book_entries(book, messages, token_fn))
    return result


def _world_info_section(entries: list[BookEntry]) -> str:
    """Build the ``[World Info]`` block from activated entries."""
    return '[World Info]\n' + '\n\n'.join(e.content.strip() for e in entries)


def apply_lorebook(
    system: str,
    card: CharacterCard,
    messages: list[dict],
    token_fn=None,
    extra_books: Optional[list[CharacterBook]] = None,
) -> str:
    """Append a ``[World Info]`` block to *system* for lorebook activations.

    Returns *system* unchanged when nothing activates.  *extra_books* are
    standalone lorebooks scanned after the card's own book (see
    :func:`collect_lorebook_entries`).  Pure function.
    """
    entries = collect_lorebook_entries(card, messages, token_fn=token_fn, extra_books=extra_books)
    if not entries:
        return system or ''
    section = _world_info_section(entries)
    if system and system.strip():
        return f"{system}\n\n{section}"
    return section


# ---- Context plan (inspector) ----

@dataclass
class ContextSection:
    """One labelled piece of the outgoing API payload."""

    title: str
    text: str
    tokens: int


@dataclass
class ContextPlan:
    """Structured view of everything a chat request will send."""

    sections: list[ContextSection] = field(default_factory=list)
    total_tokens: int = 0


def post_history_text(
    card: CharacterCard,
    user_name: str = 'User',
    custom_macros: dict[str, str] | None = None,
) -> str:
    """The card's macro-substituted ``post_history_instructions`` (or '')."""
    if not card or not (card.post_history_instructions or '').strip():
        return ''
    return substitute_macros(
        card.post_history_instructions, user_name, card.name or '', custom_macros,
    )


def example_messages_for_card(
    card: CharacterCard,
    user_name: str = 'User',
    custom_macros: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Few-shot messages derived from *card*'s ``mes_example`` (may be empty)."""
    if not card or not (card.mes_example or '').strip():
        return []
    return parse_example_messages(
        card.mes_example, user_name, card.name or '', custom_macros,
    )


def example_block_text(
    card: CharacterCard,
    user_name: str = 'User',
    custom_macros: dict[str, str] | None = None,
) -> str:
    """Few-shot examples rendered as one ``<START>``-separated text block.

    SillyTavern sends example dialogue as a labelled text block (macros
    substituted, ``<START>`` separators kept) rather than as real chat turns;
    presenting examples as if they happened skews both the model's sense of
    the conversation and any summarizer reading the history.  Pure function.
    """
    if not card or not (card.mes_example or '').strip():
        return ''
    return substitute_macros(
        card.mes_example.strip(), user_name, card.name or '', custom_macros,
    )


# ---- SillyTavern-style system prompt assembly ----

_SYSTEM_BLOCK_TITLES = (
    ('Description', 'description'),
    ('Personality', 'personality'),
    ('Scenario', 'scenario'),
)


def character_def_blocks(
    card: CharacterCard,
    user_name: str = 'User',
    custom_macros: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Labelled ``[Description]`` / ``[Personality]`` / ``[Scenario]`` blocks.

    Macro-substituted; empty fields are skipped.  Pure function.
    """
    blocks: list[tuple[str, str]] = []
    for title, attr in _SYSTEM_BLOCK_TITLES:
        raw = getattr(card, attr, '') or ''
        if not raw.strip():
            continue
        body = substitute_macros(raw, user_name, card.name or '', custom_macros)
        blocks.append((title, f'[{title}]\n{body.strip()}'))
    return blocks


def system_prompt_blocks(
    card: CharacterCard,
    messages: list[dict],
    user_name: str = 'User',
    persona: str = '',
    memories: list[dict] | None = None,
    custom_macros: dict[str, str] | None = None,
    chat_template: str | None = None,
    extra_books: Optional[list[CharacterBook]] = None,
    token_fn=None,
) -> list[tuple[str, str]]:
    """The system-side blocks of a chat request, in SillyTavern order.

    Main prompt (the card's ``system_prompt`` when set, else the chat
    template) -> world info *before* the character -> description /
    personality / scenario -> world info *after* the character -> user
    persona -> chat memory.  Mirrors SillyTavern's prompt ordering: a card
    system prompt replaces only the main prompt, its character definition is
    still injected.

    A character-def block whose raw text already appears in the main prompt
    (custom templates may inline ``{description}``) is not duplicated.
    ``at_depth`` / Author's-Note-positioned world-info entries are
    message-level injections (see :func:`injections_for_chat`), not system
    blocks.  Pure function (token counting injectable).
    """
    if token_fn is None:
        from src.token_counter import count_tokens as token_fn

    blocks: list[tuple[str, str]] = []
    main = resolve_system_prompt(
        card, user_name, chat_template=chat_template, custom_macros=custom_macros,
    )
    if main.strip():
        blocks.append(('System Prompt', main.strip()))

    entries = collect_lorebook_entries(
        card, messages, token_fn=token_fn, extra_books=extra_books,
    )
    before = [e for e in entries if _entry_position(e) == 'before_char']
    after = [e for e in entries if _entry_position(e) == 'after_char']
    if before:
        blocks.append(('World Info', _world_info_section(
            _substituted_entries(before, user_name, card.name or '', custom_macros),
        )))

    for title, attr in _SYSTEM_BLOCK_TITLES:
        raw = (getattr(card, attr, '') or '')
        if not raw.strip():
            continue
        if raw.strip() in main:
            # The main prompt already inlines this field (custom template).
            continue
        body = substitute_macros(raw, user_name, card.name or '', custom_macros)
        blocks.append((title, f'[{title}]\n{body.strip()}'))

    if after:
        blocks.append(('World Info (after character)', _world_info_section(
            _substituted_entries(after, user_name, card.name or '', custom_macros),
        )))

    if persona and persona.strip():
        blocks.append(('User Persona', _persona_block(persona, user_name)))
    mem_block = _memory_section(memories or [])
    if mem_block:
        blocks.append(('Chat Memory', mem_block))
    return blocks


def _substituted_entries(
    entries: list[BookEntry],
    user_name: str,
    char_name: str,
    custom_macros: dict[str, str] | None = None,
) -> list[BookEntry]:
    """Copy *entries* with macro-substituted content (SillyTavern does this)."""
    out: list[BookEntry] = []
    for e in entries:
        out.append(replace(
            e,
            content=substitute_macros(
                e.content or '', user_name, char_name, custom_macros,
            ),
        ))
    return out


def assemble_system(
    card: CharacterCard,
    messages: list[dict],
    user_name: str = 'User',
    persona: str = '',
    memories: list[dict] | None = None,
    custom_macros: dict[str, str] | None = None,
    chat_template: str | None = None,
    extra_books: Optional[list[CharacterBook]] = None,
    token_fn=None,
) -> str:
    """The system message text for a chat request (see :func:`system_prompt_blocks`)."""
    blocks = system_prompt_blocks(
        card, messages,
        user_name=user_name, persona=persona, memories=memories,
        custom_macros=custom_macros, chat_template=chat_template,
        extra_books=extra_books, token_fn=token_fn,
    )
    return '\n\n'.join(text for _title, text in blocks)


# ---- Depth-based prompt injections (Author's Note / world info) ----

@dataclass
class PromptInjection:
    """A message spliced into the history at ``depth`` messages from the end.

    ``depth`` 0 lands after the newest message (still before post-history
    instructions), 4 four messages from the end — SillyTavern's Author's Note
    / ``at_depth`` world-info semantics.  ``role`` is the API role the
    injected message carries.
    """

    depth: int
    role: str
    content: str
    title: str = ''


_ROLE_FROM_INT = {0: 'system', 1: 'user', 2: 'assistant'}

_POSITION_ALIASES = {
    'before_char': 'before_char',
    'after_char': 'after_char',
    'before_em': 'before_em',
    'after_em': 'after_em',
    'top_an': 'before_em',
    'bottom_an': 'after_em',
    'at_depth': 'at_depth',
    'depth': 'at_depth',
}


def _entry_position(entry: BookEntry) -> str:
    """Canonical placement of *entry* (unknown values become ``before_char``)."""
    raw = (getattr(entry, 'position', '') or '').strip()
    key = raw.lower().replace('-', '_').replace(' ', '_')
    return _POSITION_ALIASES.get(key, 'before_char')


def _entry_role(entry: BookEntry) -> str:
    """API role for an ``at_depth`` entry (ST roles: 0=system, 1=user, 2=assistant)."""
    try:
        value = int(getattr(entry, 'role', 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return _ROLE_FROM_INT.get(value, 'system')


def author_note_injection(
    text: str,
    depth: int = 4,
    role: str = 'system',
    user_name: str = 'User',
    char_name: str = '',
    custom_macros: dict[str, str] | None = None,
) -> Optional[PromptInjection]:
    """Build the Author's Note injection, or None when *text* is blank."""
    if not text or not text.strip():
        return None
    if role not in ('system', 'user', 'assistant'):
        role = 'system'
    return PromptInjection(
        depth=max(0, int(depth)),
        role=role,
        content=substitute_macros(
            text.strip(), user_name, char_name, custom_macros,
        ).strip(),
        title="Author's Note",
    )


def injections_for_chat(
    card: CharacterCard,
    messages: list[dict],
    author_note: dict | None = None,
    extra_books: Optional[list[CharacterBook]] = None,
    user_name: str = 'User',
    custom_macros: dict[str, str] | None = None,
    token_fn=None,
) -> list[PromptInjection]:
    """Message-level injections for a chat turn, in splice order.

    Covers world-info entries placed at ``at_depth`` (each with its own
    depth/role), entries at the Author's-Note positions (``before_EM`` /
    ``top_an`` and ``after_EM`` / ``bottom_an``, which wrap the note at the
    note's depth), and the Author's Note itself (*author_note* is
    ``{'text', 'depth', 'role'}``).  Pure function (token counting
    injectable).
    """
    entries = collect_lorebook_entries(
        card, messages, token_fn=token_fn, extra_books=extra_books,
    )
    note = author_note or {}
    an_depth = max(0, int(note.get('depth', 4) or 4))
    char_name = card.name if card else ''

    top: list[PromptInjection] = []
    bottom: list[PromptInjection] = []
    at_depth: list[PromptInjection] = []
    for e in entries:
        content = substitute_macros(
            e.content or '', user_name, char_name, custom_macros,
        ).strip()
        if not content:
            continue
        title = f"World Info ({e.name})" if e.name else 'World Info'
        pos = _entry_position(e)
        if pos == 'before_em':
            top.append(PromptInjection(
                an_depth, 'system', content, f"{title} — top of Author's Note",
            ))
        elif pos == 'after_em':
            bottom.append(PromptInjection(
                an_depth, 'system', content, f"{title} — bottom of Author's Note",
            ))
        elif pos == 'at_depth':
            depth = max(0, int(getattr(e, 'depth', 0) or 0))
            at_depth.append(PromptInjection(
                depth, _entry_role(e), content, f'{title} (depth {depth})',
            ))

    an = author_note_injection(
        note.get('text', ''), an_depth, note.get('role', 'system'),
        user_name, char_name, custom_macros,
    )
    injections = top + ([an] if an else []) + bottom + at_depth
    return [i for i in injections if i.content.strip()]


def apply_depth_injections(
    history: list[dict],
    injections: list[PromptInjection],
    with_titles: bool = False,
) -> list[dict]:
    """Return *history* with each injection spliced at its depth from the end.

    Depth is counted in messages that follow the insertion point (0 = after
    everything); insertions run deepest-first so every depth stays anchored
    to the end of the conversation.  With *with_titles*, injected dicts carry
    an extra ``_title`` key (readers such as ``history_for_api`` drop it).
    Pure function; the splice itself lives in ``ai_client.splice_injections``
    so the send path and the context inspector cannot drift apart.
    """
    from src.ai_client import splice_injections
    payload = [
        {'depth': i.depth, 'role': i.role, 'content': i.content, 'title': i.title}
        for i in (injections or [])
    ]
    return splice_injections(history or [], payload, with_titles=with_titles)


def build_context_plan(
    card: CharacterCard,
    messages: list[dict],
    memories: list[dict] | None = None,
    user_name: str = 'User',
    chat_template: str | None = None,
    custom_macros: dict[str, str] | None = None,
    persona: str = '',
    token_fn=None,
    extra_books: Optional[list[CharacterBook]] = None,
    example_placement: str = 'history',
    author_note: dict | None = None,
    jailbreak: str = '',
) -> ContextPlan:
    """Build the exact context a chat turn will send, section by section.

    Mirrors the Test tab's send path: system blocks (main prompt -> world
    info -> character definition -> persona -> memory, see
    :func:`system_prompt_blocks`), the few-shot example dialogue (placed per
    *example_placement*: ``'system'``, ``'post_history'`` or the legacy
    ``'history'`` turns), the message history with depth injections
    (Author's Note / ``at_depth`` world info) spliced in, the trailing
    post-history instructions, and the user *jailbreak* block after them.
    Pure function (token counting injectable).
    """
    if token_fn is None:
        from src.token_counter import count_tokens as token_fn

    sections: list[ContextSection] = []

    for title, text in system_prompt_blocks(
        card, messages,
        user_name=user_name, persona=persona, memories=memories,
        custom_macros=custom_macros, chat_template=chat_template,
        extra_books=extra_books, token_fn=token_fn,
    ):
        sections.append(ContextSection(title, text, token_fn(text)))

    examples_text = example_block_text(card, user_name, custom_macros)
    examples_as_turns: list[dict[str, str]] = []
    if example_placement == 'system' and examples_text:
        sections.append(ContextSection(
            'Example Dialogue (system prompt)', examples_text, token_fn(examples_text),
        ))
    elif example_placement != 'post_history':
        examples_as_turns = example_messages_for_card(card, user_name, custom_macros)

    for i, ex in enumerate(examples_as_turns, 1):
        role = ex.get('role', 'assistant')
        sections.append(ContextSection(
            f'Example {i} ({role})', ex.get('content', ''), token_fn(ex.get('content', '')),
        ))

    injections = injections_for_chat(
        card, messages, author_note=author_note, extra_books=extra_books,
        user_name=user_name, custom_macros=custom_macros, token_fn=token_fn,
    )
    rows = apply_depth_injections(messages or [], injections, with_titles=True)
    msg_no = 0
    for row in rows:
        role = row.get('role', 'user') if isinstance(row, dict) else 'user'
        content = (row.get('content') or '') if isinstance(row, dict) else ''
        if isinstance(row, dict) and row.get('_title'):
            sections.append(ContextSection(
                f"{row['_title']} ({role})", content, token_fn(content),
            ))
            continue
        msg_no += 1
        attachments = row.get('attachments') or [] if isinstance(row, dict) else []
        title = f'Message {msg_no} ({role})'
        if attachments:
            title += f' — {len(attachments)} attachment(s)'
        sections.append(ContextSection(title, content, token_fn(content)))

    if example_placement == 'post_history' and examples_text:
        sections.append(ContextSection(
            'Example Dialogue (post-history)', examples_text, token_fn(examples_text),
        ))

    phi = post_history_text(card, user_name, custom_macros)
    if phi:
        sections.append(ContextSection('Post-History Instructions', phi, token_fn(phi)))

    jb = (jailbreak or '').strip()
    if jb:
        jb_text = substitute_macros(jb, user_name, card.name or '', custom_macros)
        sections.append(ContextSection('Jailbreak', jb_text, token_fn(jb_text)))

    return ContextPlan(
        sections=sections,
        total_tokens=sum(s.tokens for s in sections),
    )

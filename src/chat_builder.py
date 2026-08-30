from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from src.card_models import BookEntry, CharacterBook, CharacterCard, parse_character_book

# SillyTavern macro substitution: {{user}} and {{char}} (case-insensitive).
_USER_RE = re.compile(r'\{\{\s*user\s*\}\}', re.IGNORECASE)
_CHAR_RE = re.compile(r'\{\{\s*char\s*\}\}', re.IGNORECASE)

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
) -> str:
    """Replace ``{{user}}`` / ``{{char}}`` macros in *text*.

    Case-insensitive and tolerant of internal whitespace (``{{ user }}``).
    *custom_macros* maps additional macro names (without braces) to values
    and is applied after the built-in macros.  Values are substituted
    literally (backslashes have no special meaning).  Pure function so it
    can be unit-tested without Qt.
    """
    if not text:
        return ''
    text = _sub_literal(_USER_RE, user_name or '', text)
    text = _sub_literal(_CHAR_RE, char_name or '', text)
    if custom_macros:
        for name, value in custom_macros.items():
            key = str(name).strip()
            if not key:
                continue
            pattern = r'\{\{\s*' + re.escape(key) + r'\s*\}\}'
            text = _sub_literal(pattern, str(value), text)
    return text


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

    macroed = substitute_macros(raw, user_name, card.name, custom_macros)
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
    """
    substituted = substitute_macros(content, user_name, char_name, custom_macros)
    new_list = list(messages)
    new_list.append({'role': role, 'content': substituted})
    return new_list


def make_message(role: str, content: str, attachments: list[dict] | None = None) -> dict:
    """Build a message dict, attaching an optional attachment list.

    Attachments are stored verbatim (never macro-substituted) so file contents
    are passed through unchanged.  Pure function.
    """
    msg: dict = {'role': role, 'content': content or ''}
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


def apply_persona(system: str, persona: str, user_name: str = 'User') -> str:
    """Append a ``[User persona]`` block to *system* when *persona* is set.

    Mirrors :func:`apply_memory`: pure, returns the input unchanged when the
    persona is blank.
    """
    if not persona or not persona.strip():
        return system or ''
    block = f"[User persona]\n{user_name or 'User'} is: {persona.strip()}"
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
) -> ContextPlan:
    """Build the exact context a chat turn will send, section by section.

    Mirrors :meth:`ui.test_tab.TestTab._assemble_system` layering (base
    prompt -> user persona -> world info -> chat memory) plus the few-shot
    example dialogue, the message history, and the trailing
    post-history instruction injected after the history.  *extra_books* are
    standalone lorebooks injected after the card's own book.  Pure function
    (token counting injectable).
    """
    if token_fn is None:
        from src.token_counter import count_tokens as token_fn

    sections: list[ContextSection] = []

    base = resolve_system_prompt(
        card, user_name, chat_template=chat_template, custom_macros=custom_macros,
    )
    base = apply_persona(base, persona, user_name)
    if base.strip():
        sections.append(ContextSection('System Prompt', base, token_fn(base)))

    entries = collect_lorebook_entries(card, messages, token_fn=token_fn, extra_books=extra_books)
    if entries:
        block = _world_info_section(entries)
        sections.append(ContextSection('World Info', block, token_fn(block)))

    mem_block = _memory_section(memories or [])
    if mem_block:
        sections.append(ContextSection('Chat Memory', mem_block, token_fn(mem_block)))

    examples = example_messages_for_card(card, user_name, custom_macros)
    for i, ex in enumerate(examples, 1):
        role = ex.get('role', 'assistant')
        sections.append(ContextSection(
            f'Example {i} ({role})', ex.get('content', ''), token_fn(ex.get('content', '')),
        ))

    for i, m in enumerate(messages or [], 1):
        role = m.get('role', 'user') if isinstance(m, dict) else 'user'
        content = (m.get('content') or '') if isinstance(m, dict) else ''
        attachments = m.get('attachments') or [] if isinstance(m, dict) else []
        title = f'Message {i} ({role})'
        if attachments:
            title += f' — {len(attachments)} attachment(s)'
        sections.append(ContextSection(title, content, token_fn(content)))

    phi = post_history_text(card, user_name, custom_macros)
    if phi:
        sections.append(ContextSection('Post-History Instructions', phi, token_fn(phi)))

    return ContextPlan(
        sections=sections,
        total_tokens=sum(s.tokens for s in sections),
    )

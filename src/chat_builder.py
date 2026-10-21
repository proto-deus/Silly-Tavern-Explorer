from __future__ import annotations

import random
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional

from src.card_models import BookEntry, CharacterBook, CharacterCard, parse_character_book
from src.context_templates import (
    ContextTemplate,
    default_template,
    name_prefixes as _name_prefixes,
    render_block,
    render_jailbreak,
    render_main,
    render_post_history,
)

# SillyTavern macro substitution: {{user}} and {{char}} (case-insensitive).
#
# The lookarounds keep a *format-escaped* macro (``{{{{user}}}}``) out of the
# substitution: the doubled braces are how a template asks ``str.format`` to
# leave a literal ``{{user}}`` in the output - for instance the speaker
# prefix an example dialogue is supposed to use.  Substituting it turned
# the instruction into the user's name, and made the prompt preview show
# something different from what the chat actually sends.
_USER_RE = re.compile(r'(?<!\{)\{\{\s*user\s*\}\}(?!\})', re.IGNORECASE)
_CHAR_RE = re.compile(r'(?<!\{)\{\{\s*char\s*\}\}(?!\})', re.IGNORECASE)

# Dynamic SillyTavern macros evaluated per substitution pass.
_DATE_RE = re.compile(r'(?<!\{)\{\{\s*date\s*\}\}(?!\})', re.IGNORECASE)
_TIME_RE = re.compile(r'(?<!\{)\{\{\s*time\s*\}\}(?!\})', re.IGNORECASE)
_DATETIME_RE = re.compile(r'(?<!\{)\{\{\s*datetime\s*\}\}(?!\})', re.IGNORECASE)
_RANDOM_RE = re.compile(
    r'(?<!\{)\{\{\s*(?:random|pick)\s*:\s*([^{}]*)\}\}(?!\})', re.IGNORECASE,
)
_ROLL_RE = re.compile(r'(?<!\{)\{\{\s*roll\s*:\s*([^{}]*)\}\}(?!\})', re.IGNORECASE)
# ``NdM+K`` / ``d20`` / ``2d6-1`` dice notation for {{roll:...}}.
# The digit runs are bounded: an unbounded run (thousands of digits) made the
# int() conversion raise ValueError on Python 3.11+.
_ROLL_SPEC_RE = re.compile(
    r'^\s*(\d{0,3})\s*[dD]\s*(\d{1,9})\s*(?:([+-])\s*(\d{1,9}))?\s*$'
)

# ``{{char}}`` / ``{{user}}`` line prefixes inside mes_example blocks.
_EXAMPLE_SPEAKER_RE = re.compile(
    r'^\s*(\{\{\s*char\s*\}\}|\{\{\s*user\s*\}\})\s*:\s?', re.IGNORECASE
)

# Lorebook defaults when the book doesn't specify them.
DEFAULT_LOREBOOK_SCAN_DEPTH = 4
# The token budget a lorebook gets when it does not define one.  The book's
# own budget lives in the card's character book - i.e. it is controlled by
# whoever made the card - so a book that omits it must not get an unbounded
# injection.
DEFAULT_LOREBOOK_TOKEN_BUDGET = 2000
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

    Dynamic macros are evaluated on the *template* before any name or custom
    value is inserted: a character named ``{{random:alpha|beta}}`` must render
    literally, not be rolled.  A custom macro shadows the dynamic macro of the
    same name (SillyTavern behaviour), so a custom ``{{time}}`` still wins.
    """
    if not text:
        return ''

    def _value(v) -> str:
        s = v if isinstance(v, str) else str(v)
        return s.replace('{', '{{').replace('}', '}}') if escape_for_format else s

    custom_macros = custom_macros or {}
    shadowed = {
        str(name).strip().lower()
        for name in custom_macros
        if str(name).strip()
    }
    text = _sub_dynamic_macros(
        text, escape_for_format=escape_for_format, rng=rng, now=now, skip=shadowed,
    )
    text = _sub_literal(_USER_RE, _value(user_name or ''), text)
    text = _sub_literal(_CHAR_RE, _value(char_name or ''), text)
    for name, value in custom_macros.items():
        key = str(name).strip()
        if not key:
            continue
        pattern = r'\{\{\s*' + re.escape(key) + r'\s*\}\}'
        text = _sub_literal(pattern, _value(value), text)
    return text


def _sub_dynamic_macros(
    text: str,
    escape_for_format: bool = False,
    rng: Optional[random.Random] = None,
    now: Optional[datetime] = None,
    skip: Optional[set[str]] = None,
) -> str:
    """Evaluate ``{{time}}`` / ``{{date}}`` / ``{{random}}`` / ``{{roll}}``.

    *skip* names macro keywords to leave alone (lowercased): a custom macro
    shadows the dynamic macro of the same name.
    """
    if not text:
        return ''
    moment = now or datetime.now()
    dice = rng or random.Random()
    skip = {str(s).lower() for s in (skip or set())}

    def _value(v) -> str:
        s = v if isinstance(v, str) else str(v)
        return s.replace('{', '{{').replace('}', '}}') if escape_for_format else s

    if 'date' not in skip:
        text = _DATE_RE.sub(lambda _m: _value(moment.strftime('%Y-%m-%d')), text)
    if 'time' not in skip:
        text = _TIME_RE.sub(lambda _m: _value(moment.strftime('%H:%M')), text)
    if 'datetime' not in skip:
        text = _DATETIME_RE.sub(
            lambda _m: _value(moment.strftime('%Y-%m-%d %H:%M')), text,
        )

    def _replace_random(m: 're.Match') -> str:
        options = _macro_options(m.group(1))
        if not options:
            return ''
        return _value(dice.choice(options))

    if not {'random', 'pick'} & skip:
        text = _RANDOM_RE.sub(_replace_random, text)

    def _replace_roll(m: 're.Match') -> str:
        rolled = _roll_macro(m.group(1), dice)
        return m.group(0) if rolled is None else _value(str(rolled))

    if 'roll' not in skip:
        text = _ROLL_RE.sub(_replace_roll, text)
    return text


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
    """Evaluate a ``{{roll: NdM±K}}`` dice expression (None when invalid).

    The digit runs are bounded in the pattern *and* the conversion is
    guarded: an unbounded run (a card can put thousands of digits in a
    ``{{roll:}}`` macro) made ``int()`` raise ``ValueError`` on Python 3.11+
    ("Exceeds the limit (4300 digits) for integer string conversion"), which
    escaped into the send path and aborted the app.
    """
    match = _ROLL_SPEC_RE.match(payload or '')
    if not match:
        return None
    try:
        count = int(match.group(1) or '1')
        sides = int(match.group(2))
    except (ValueError, OverflowError):
        return None
    if sides < 1 or count < 1 or count > 100:
        return None
    try:
        bonus = int(match.group(4)) if match.group(3) else 0
    except (ValueError, OverflowError):
        return None
    total = sum(rng.randint(1, sides) for _ in range(count))
    if match.group(3):
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


def _untrusted(text: str) -> str:
    """Neutralise the card-data delimiters inside third-party text.

    A character card is a third-party download, and so is a lorebook entry.
    Their text must never be able to close the ``<card_data>`` block that
    separates the data from the instructions.
    """
    from src.ai_prompts import neutralize_card_data

    return neutralize_card_data(text or '')


def _as_card_data(body: str) -> str:
    """Wrap third-party text in the ``<card_data>`` delimiters.

    The text is neutralised first, so a card cannot forge the closing
    delimiter and have the rest of its text read as an instruction.
    """
    from src.ai_prompts import CARD_DATA_CLOSE, CARD_DATA_OPEN

    body = _untrusted(body or '').strip()
    if not body:
        return ''
    return f'{CARD_DATA_OPEN}\n{body}\n{CARD_DATA_CLOSE}'


def _card_fields(card: CharacterCard) -> dict[str, Any]:
    return {
        'name': _untrusted(card.name or ''),
        'description': _untrusted(card.description or ''),
        'personality': _untrusted(card.personality or ''),
        'scenario': _untrusted(card.scenario or ''),
        'first_mes': _untrusted(card.first_mes or ''),
    }


def resolve_system_prompt(
    card: CharacterCard,
    user_name: str = 'User',
    chat_template: str | None = None,
    custom_macros: dict[str, str] | None = None,
) -> str:
    """Build the main prompt for a chat with *card*.

    The system prompt template (*chat_template*, or the active template from
    :mod:`src.prompt_templates` when ``None``) is the main prompt; the card's
    own ``system_prompt`` is appended after it as extra character-specific
    instructions (SillyTavern's "Prefer Char. Instructions" ordering).  A
    blank template leaves just the card prompt, and vice versa.

    Macros (``{{user}}`` / ``{{char}}``) are substituted *before* the
    ``{name}``-style format placeholders so the double-brace macros aren't
    consumed by ``str.format`` escaping.  The card's own prompt is raw user
    text: only macro substitution, never ``format_map`` (it may contain
    literal braces).
    """
    if chat_template is not None:
        raw = chat_template
    else:
        from src.prompt_templates import active_template_content
        raw = active_template_content()

    parts: list[str] = []
    if raw and raw.strip():
        # Escape braces in the macro values: ``substitute`` below runs
        # format_map over this string, and card/user text must not become
        # template syntax.
        macroed = substitute_macros(
            raw, user_name, card.name, custom_macros, escape_for_format=True,
        )
        from src.ai_prompts import substitute
        parts.append(substitute(macroed, **_card_fields(card)).strip())
    if card.system_prompt and card.system_prompt.strip():
        # The card's own prompt is third-party text: it is quoted as data so
        # it cannot pose as an instruction of the app's own system prompt.
        parts.append(_as_card_data(substitute_macros(
            card.system_prompt, user_name, card.name, custom_macros,
        ).strip()))
    return '\n\n'.join(p for p in parts if p)


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


def _parse_example_turns(
    mes_example: str,
) -> list[list[tuple[Optional[str], str]]]:
    """Split ``mes_example`` into per-``<START>`` blocks of speaker turns.

    Each turn is ``(speaker, text)`` where *speaker* is ``'user'`` /
    ``'assistant'`` or None for narration-only blocks (no recognised
    ``{{user}}:`` / ``{{char}}:`` prefix anywhere in the block).  Macros are
    NOT substituted here.  Pure function.
    """
    out: list[list[tuple[Optional[str], str]]] = []
    for block in _split_example_blocks(mes_example or ''):
        turns: list[tuple[Optional[str], str]] = []
        speaker: Optional[str] = None
        buffer: list[str] = []

        def flush() -> None:
            text = '\n'.join(buffer).strip()
            if text:
                turns.append((speaker, text))

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
        if turns:
            out.append(turns)
    return out


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
    for turns in _parse_example_turns(mes_example or ''):
        for speaker, text in turns:
            messages.append({
                'role': speaker or 'assistant',
                'content': substitute_macros(
                    text, user_name, char_name, custom_macros,
                ),
            })
    return messages


def _persona_block(persona: str, user_name: str = 'User') -> str:
    """The user-persona body (label-free; the caller frames it)."""
    return f"{user_name or 'User'} is: {persona.strip()}"


def apply_persona(system: str, persona: str, user_name: str = 'User') -> str:
    """Append a ``[User persona]`` block to *system* when *persona* is set.

    Mirrors :func:`apply_memory`: pure, returns the input unchanged when the
    persona is blank.
    """
    if not persona or not persona.strip():
        return system or ''
    block = f"[User persona]\n{_persona_block(persona, user_name)}"
    if system and system.strip():
        return f"{system}\n\n{block}"
    return block


def _attachment_text_block(attachment: dict) -> str:
    name = attachment.get('name', 'file')
    data = attachment.get('data', '')
    return f"[Attached file: {name}]\n{data}"


def history_for_api(
    messages: list[dict],
    name_prefixes: dict[str, str] | None = None,
) -> list[dict]:
    """Convert internal chat messages into API-ready message dicts.

    Internal fields (``attachments``, and any ``id``/``ts`` markers) are
    stripped.  Text attachments are prepended to the message as a labelled
    block; image attachments turn the message ``content`` into an
    OpenAI-compatible multimodal list of text + ``image_url`` parts.  When
    *name_prefixes* maps a role to a prefix (the context template's names
    behavior), it is prepended to the message text - SillyTavern's "add
    character/user names to messages".  Pure function so it can be
    unit-tested without Qt.
    """
    result: list[dict] = []
    for m in messages or []:
        role = m.get('role', 'user')
        content = m.get('content', '') or ''
        # The attachments come from a session file on disk: a hand-edited
        # one can hold anything, and ``a.get(...)`` on a string raised in the
        # send path.
        attachments = [
            a for a in (m.get('attachments') or []) if isinstance(a, dict)
        ]
        prefix = (name_prefixes or {}).get(role, '') or ''

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
            if prefix and combined:
                combined = prefix + combined
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
            result.append({'role': role, 'content': prefix + combined})
        else:
            result.append({'role': role, 'content': prefix + content})
    return result


# ---- Strict chat-template role normalisation ----

# Header prepended when a leading assistant turn (the card greeting) has to
# move into the system message.  Macro-free on purpose: normalisation runs on
# the wire payload where every macro has already been substituted.
CONVERSATION_START_HEADER = '[Conversation start]'


def _content_parts(content: Any) -> list[dict]:
    """Normalise a message ``content`` field into OpenAI-style parts."""
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    text = content if isinstance(content, str) else ('' if content is None else str(content))
    return [{'type': 'text', 'text': text}] if text else []


def _merge_contents(contents: list[Any]) -> Any:
    """Combine several message contents into one.

    All-text contents collapse to a single ``\\n\\n``-joined string (so plain
    messages stay plain strings on the wire); anything carrying multimodal
    parts becomes one concatenated parts list.
    """
    parts: list[dict] = []
    for content in contents:
        parts.extend(_content_parts(content))
    if all(p.get('type') == 'text' for p in parts):
        return '\n\n'.join(p.get('text', '') for p in parts if p.get('text', ''))
    return parts


def _split_text_parts(content: Any) -> tuple[str, list[dict]]:
    """Split *content* into its plain text and its non-text parts.

    Used when a message has to fold into the system string: text can ride
    along, but image parts are handed back so they become their own turn.
    """
    parts = _content_parts(content)
    texts = [p.get('text', '') for p in parts if p.get('type') == 'text']
    rest = [p for p in parts if p.get('type') != 'text']
    return '\n\n'.join(t for t in texts if t), rest


def _merge_same_role(messages: list[dict]) -> list[dict]:
    """Return *messages* with every run of equal roles merged into one."""
    out: list[dict] = []
    for m in messages:
        role = m.get('role', 'user')
        if out and out[-1].get('role', 'user') == role:
            merged = dict(out[-1])
            merged['content'] = _merge_contents(
                [out[-1].get('content', ''), m.get('content', '')],
            )
            out[-1] = merged
        else:
            out.append(dict(m))
    return out


def normalize_chat_roles(
    messages: list[dict],
    conversation_start_header: str = CONVERSATION_START_HEADER,
) -> list[dict]:
    """Rewrite *messages* into the shape strict chat templates require.

    Llama 3.x / Gemma Jinja templates reject any request whose roles are not
    exactly: one optional ``system`` message first, then ``user`` /
    ``assistant`` strictly alternating and starting with ``user``.  The send
    path legitimately builds shapes that violate this - trailing
    post-history / jailbreak system blocks, ``system`` injections spliced at
    depth (Author's Note, world info), the card greeting as a leading
    ``assistant`` turn, consecutive same-role turns from Impersonate or a
    failed send - so the wire payload is repaired here without losing text:

    1. Only the first message stays ``system``; every later ``system``
       message becomes ``user`` (a trailing block then glues onto the
       preceding user turn, SillyTavern's "invisible user injection", or
       alternates legally after an ``assistant`` turn).
    2. Consecutive same-role messages merge (blank-line joined; multimodal
       ``content`` part-lists concatenate).
    3. A leading ``assistant`` run - the greeting - moves into the system
       message under :data:`CONVERSATION_START_HEADER` so history can start
       on a ``user`` turn; any image parts it carried become a ``user``
       message of their own.

    The result is idempotent: roles alternate from the second message on, so
    re-running changes nothing.  Pure function.
    """
    msgs = [dict(m) for m in (messages or []) if isinstance(m, dict)]
    lead: dict | None = None
    if msgs and msgs[0].get('role') == 'system':
        lead = msgs.pop(0)
    for m in msgs:
        if m.get('role') == 'system':
            m['role'] = 'user'
    msgs = _merge_same_role(msgs)

    head: list[dict] = []
    while msgs and msgs[0].get('role') == 'assistant':
        head.append(msgs.pop(0))
    if head:
        texts: list[str] = []
        leftovers: list[dict] = []
        for m in head:
            text, rest = _split_text_parts(m.get('content', ''))
            if text:
                texts.append(text)
            if rest:
                leftovers.append({'role': 'user', 'content': rest})
        if texts:
            body = f"{conversation_start_header}\n" + '\n\n'.join(texts)
            if lead is None:
                lead = {'role': 'system', 'content': body}
            else:
                lead = dict(lead)
                lead['content'] = _merge_contents([lead.get('content', ''), body])
        if leftovers:
            msgs = _merge_same_role(leftovers + msgs)

    return ([lead] if lead is not None else []) + msgs


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
    """The chat-memory body (bulleted lines), or None when nothing to include."""
    items = []
    for m in (memories or []):
        if not isinstance(m, dict):
            continue
        content = m.get('content')
        # A session file can hold anything: a non-string content made
        # ``.strip()`` raise in the send path.
        if not isinstance(content, str) or not content.strip():
            continue
        items.append(content)
    if not items:
        return None
    recent = items[-limit:]
    return '\n'.join(f"- {content.strip()}" for content in recent)


def apply_memories(system: str, memories: list[dict], limit: int = 20) -> str:
    """Append the most recent *limit* memory entries to *system*.

    Each entry contributes one bullet point; older entries are dropped so
    the prompt doesn't grow unboundedly.  Entries without ``content`` are
    silently skipped.  Pure function.
    """
    section = _memory_section(memories, limit)
    if section is None:
        return system or ''
    block = f"[Chat memory]\n{section}"
    if system and system.strip():
        return f"{system}\n\n{block}"
    return block


def _attributes_section(attributes: list[dict] | None) -> Optional[str]:
    """The persistent-attribute body (``name: value`` lines) or None."""
    items = [
        a for a in (attributes or [])
        if isinstance(a, dict)
        and a.get('enabled', True)
        and (a.get('name') or '').strip()
        and (a.get('value') or '').strip()
    ]
    if not items:
        return None
    return '\n'.join(
        f"{a['name'].strip()}: {a['value'].strip()}" for a in items
    )


def apply_attributes(system: str, attributes: list[dict] | None) -> str:
    """Append the persistent-attribute state to *system* when any are set.

    Pure function.  Returns the original *system* untouched when no
    attribute carries a value.
    """
    section = _attributes_section(attributes)
    if section is None:
        return system or ''
    block = f"[Persistent attributes]\n{section}"
    if system and system.strip():
        return f"{system}\n\n{block}"
    return block


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
            # Not \b: that requires a word/non-word transition, so keys that
            # start or end with a non-word character ('.hack', 'C++', '#tag')
            # could never match at that edge and the entry silently never
            # fired. The lookarounds accept any non-word neighbour instead.
            pattern = r'(?<!\w)' + escaped + r'(?!\w)'
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
    if budget <= 0:
        # The book's own budget is attacker-controlled (it lives in the
        # card's character book), so a book that simply omits it got *no*
        # cap at all: a third-party book could inject an unbounded amount
        # of text.  A default budget applies when the book asks for none.
        budget = DEFAULT_LOREBOOK_TOKEN_BUDGET
    # Drop lowest-priority (highest insertion-order) entries until the
    # activated set fits the book's token budget.
    costs = [token_fn(e.content or '') for e in ordered]
    total = sum(costs)
    count = len(ordered)
    while count > 0 and total > budget:
        count -= 1
        total -= costs[count]
    return ordered[:count]


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
    """The world-info body from activated entries (label-free).

    Every entry is third-party text (the card's own book and the standalone
    books), so the delimiters inside it are neutralised.
    """
    return '\n\n'.join(_untrusted(e.content).strip() for e in entries)


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
    section = f"[World Info]\n{_as_card_data(_world_info_section(entries))}"
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


def _render_example_block(
    mes_example: str,
    user_name: str,
    char_name: str,
    custom_macros: dict[str, str] | None,
    template: ContextTemplate,
) -> str:
    """Re-render the few-shot examples under a context template's rules.

    Speaker prefixes and the block separator come from the template;
    narration-only turns are kept unprefixed.  Macros in both the lines and
    the prefixes are substituted.  Pure function.
    """
    user_prefix = substitute_macros(
        template.example_user_prefix or '', user_name, char_name, custom_macros,
    )
    char_prefix = substitute_macros(
        template.example_char_prefix or '', user_name, char_name, custom_macros,
    )
    separator = template.example_separator or '<START>'
    rendered_blocks: list[str] = []
    for turns in _parse_example_turns(mes_example):
        lines: list[str] = []
        for speaker, text in turns:
            body = substitute_macros(text, user_name, char_name, custom_macros)
            if speaker == 'user':
                lines.append(user_prefix + body)
            elif speaker == 'assistant':
                lines.append(char_prefix + body)
            else:
                lines.append(body)
        rendered_blocks.append(f'{separator}\n' + '\n'.join(lines))
    return '\n'.join(rendered_blocks)


def example_block_text(
    card: CharacterCard,
    user_name: str = 'User',
    custom_macros: dict[str, str] | None = None,
    context_template: ContextTemplate | None = None,
) -> str:
    """Few-shot examples rendered as one text block ('' = none).

    Without a *context_template* the card's ``mes_example`` is returned with
    macros substituted and its ``<START>`` separators kept (the classic
    output).  With one, the examples are re-rendered under the template's
    speaker prefixes and block separator.  SillyTavern sends example
    dialogue as a text block rather than as real chat turns; presenting
    examples as if they happened skews both the model's sense of the
    conversation and any summarizer reading the history.  Pure function.

    The examples are third-party text and are quoted as data: the block is
    sent as a trailing system message, the strongest position in the
    request, so an example must not be able to pose as an instruction.
    """
    if not card or not (card.mes_example or '').strip():
        return ''
    if context_template is None:
        body = substitute_macros(
            card.mes_example.strip(), user_name, card.name or '', custom_macros,
        )
    else:
        body = _render_example_block(
            card.mes_example, user_name, card.name or '', custom_macros, context_template,
        )
    return _as_card_data(body)


# ---- SillyTavern-style system prompt assembly ----

_SYSTEM_BLOCK_TITLES = (
    ('Description', 'description'),
    ('Personality', 'personality'),
    ('Scenario', 'scenario'),
)

# Labels as they appear inside the framed block text (the inspector's titles
# keep their prettier casing).  Framing comes from the context template.
_SYSTEM_BLOCK_LABELS = {
    'System Prompt': 'System Prompt',
    'World Info': 'World Info',
    'Description': 'Description',
    'Personality': 'Personality',
    'Scenario': 'Scenario',
    'World Info (after character)': 'World Info',
    'User Persona': 'User persona',
    'Persistent Attributes': 'Persistent attributes',
    'Chat Memory': 'Chat memory',
}


def _template_field_refs(template: str) -> set[str]:
    """Format-field names referenced by *template* as real placeholders.

    ``string.Formatter`` reports ``{description}`` as a field but treats
    ``{{description}}`` as an escaped literal, which is exactly the split the
    inline-field check needs. Never raises: malformed templates report no
    references (blocks are then all emitted, which is safe).
    """
    from string import Formatter
    refs: set[str] = set()
    try:
        parsed = Formatter().parse(template or '')
        for _literal, field_name, _spec, _conv in parsed:
            if not field_name:
                continue
            base = field_name.split('.')[0].split('[')[0].strip()
            if base:
                refs.add(base)
    except ValueError:
        return set()
    return refs


def _active_template_source(chat_template: str | None) -> str:
    if chat_template is not None:
        return chat_template
    from src.prompt_templates import active_template_content
    return active_template_content()


def character_def_blocks(
    card: CharacterCard,
    user_name: str = 'User',
    custom_macros: dict[str, str] | None = None,
    context_template: ContextTemplate | None = None,
) -> list[tuple[str, str]]:
    """Labelled ``[Description]`` / ``[Personality]`` / ``[Scenario]`` blocks.

    Macro-substituted; empty fields are skipped.  Block framing follows the
    context template.  Pure function.
    """
    blocks: list[tuple[str, str]] = []
    for title, attr in _SYSTEM_BLOCK_TITLES:
        raw = getattr(card, attr, '') or ''
        if not raw.strip():
            continue
        body = substitute_macros(raw, user_name, card.name or '', custom_macros)
        blocks.append((title, render_block(
            context_template, _SYSTEM_BLOCK_LABELS.get(title, title),
            _as_card_data(body.strip()),
        )))
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
    attributes: list[dict] | None = None,
    context_template: ContextTemplate | None = None,
) -> list[tuple[str, str]]:
    """The system-side blocks of a chat request, in SillyTavern order.

    Main prompt (the system prompt template with the card's own
    ``system_prompt`` appended, see :func:`resolve_system_prompt`) ->
    world info *before* the character -> description / personality /
    scenario -> world info *after* the character -> user persona ->
    persistent attributes -> chat memory.  Mirrors SillyTavern's prompt
    ordering: the card's system prompt adds to the main prompt, its
    character definition is still injected.

    A character-def block whose field the template structurally references
    (``{description}`` etc. in a custom template) is not duplicated.
    ``at_depth`` / Author's-Note-positioned world-info entries are
    message-level injections (see :func:`injections_for_chat`), not system
    blocks.  Block framing follows the *context_template* (labels and
    separators).  Pure function (token counting injectable).
    """
    if token_fn is None:
        from src.token_counter import count_tokens as token_fn

    def framed(title: str, body: str) -> str:
        return render_block(context_template, _SYSTEM_BLOCK_LABELS.get(title, title), body)

    blocks: list[tuple[str, str]] = []
    main = resolve_system_prompt(
        card, user_name, chat_template=chat_template, custom_macros=custom_macros,
    )
    # The untrusted-data rule travels with the request: the card's own text
    # sits in the *system* message, so the model has to be told explicitly
    # that the delimited blocks are data and not instructions.  It is always
    # present - a "Blank" template still gets the card's description, world
    # info and example dialogue.
    from src.ai_prompts import CHAT_CARD_DATA_GUARD
    body = main.strip()
    body = f'{body}\n\n{CHAT_CARD_DATA_GUARD}' if body else CHAT_CARD_DATA_GUARD
    blocks.append(('System Prompt', render_main(context_template, body)))

    entries = collect_lorebook_entries(
        card, messages, token_fn=token_fn, extra_books=extra_books,
    )
    before = [e for e in entries if _entry_position(e) == 'before_char']
    after = [e for e in entries if _entry_position(e) == 'after_char']
    if before:
        blocks.append(('World Info', framed('World Info', _as_card_data(_world_info_section(
            _substituted_entries(before, user_name, card.name or '', custom_macros),
        )))))

    # Structural inlining check: a custom template that references
    # ``{description}`` / ``{personality}`` / ``{scenario}`` already emits the
    # field, so the labelled block would duplicate it. (The old check was a
    # substring test of the raw field against the whole main prompt, which
    # silently dropped any short field - a character named "Aria" whose
    # description is "Aria" lost its entire [Description] block.)
    inlined = _template_field_refs(_active_template_source(chat_template))

    for title, attr in _SYSTEM_BLOCK_TITLES:
        raw = (getattr(card, attr, '') or '')
        if not raw.strip():
            continue
        if attr in inlined:
            # The main prompt already inlines this field (custom template).
            continue
        body = substitute_macros(raw, user_name, card.name or '', custom_macros)
        blocks.append((title, framed(title, _as_card_data(body.strip()))))

    if after:
        blocks.append(('World Info (after character)', framed(
            'World Info (after character)',
            _as_card_data(_world_info_section(
                _substituted_entries(after, user_name, card.name or '', custom_macros),
            )),
        )))

    if persona and persona.strip():
        blocks.append(('User Persona', framed(
            'User Persona', _persona_block(persona, user_name),
        )))
    attr_block = _attributes_section(attributes)
    if attr_block:
        # Attribute names may carry {{char}} / {{user}} macros (e.g.
        # "{{char}} mood"); resolve them like every other system block.
        blocks.append(('Persistent Attributes', framed(
            'Persistent Attributes',
            substitute_macros(attr_block, user_name, card.name or '', custom_macros),
        )))
    mem_block = _memory_section(memories or [])
    if mem_block:
        blocks.append(('Chat Memory', framed('Chat Memory', mem_block)))
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
    attributes: list[dict] | None = None,
    context_template: ContextTemplate | None = None,
) -> str:
    """The system message text for a chat request (see :func:`system_prompt_blocks`).

    Blocks are joined with the context template's block separator.
    """
    blocks = system_prompt_blocks(
        card, messages,
        user_name=user_name, persona=persona, memories=memories,
        custom_macros=custom_macros, chat_template=chat_template,
        extra_books=extra_books, token_fn=token_fn, attributes=attributes,
        context_template=context_template,
    )
    separator = (context_template or default_template()).block_separator or '\n\n'
    return separator.join(text for _title, text in blocks)


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


def _safe_depth(value, default: int) -> int:
    """A non-negative depth from *value* (falls back to *default*).

    The depth comes from a session file or a lorebook entry, so it can be
    anything; ``int()`` on a non-numeric value raised in the send path.
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


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
        depth=max(0, _safe_depth(depth, 4)),
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
    an_depth = max(0, _safe_depth(note.get('depth', 4), 4))
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
            depth = max(0, _safe_depth(getattr(e, 'depth', 0), 0))
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
    template_post_history: str = '',
    attributes: list[dict] | None = None,
    context_template: ContextTemplate | None = None,
) -> ContextPlan:
    """Build the exact context a chat turn will send, section by section.

    Mirrors the Test tab's send path: system blocks (main prompt -> world
    info -> character definition -> persona -> persistent attributes ->
    memory, see :func:`system_prompt_blocks`), the few-shot example dialogue
    (placed per *example_placement*: ``'system'``, ``'post_history'`` or the
    legacy ``'history'`` turns), the message history with depth injections
    (Author's Note / ``at_depth`` world info) spliced in, the trailing
    post-history instructions, and the user *jailbreak* block after them.
    *context_template* drives block framing, names behavior, and example
    rendering exactly as it does on the send path.  Pure function (token
    counting injectable).
    """
    if token_fn is None:
        from src.token_counter import count_tokens as token_fn

    sections: list[ContextSection] = []

    for title, text in system_prompt_blocks(
        card, messages,
        user_name=user_name, persona=persona, memories=memories,
        custom_macros=custom_macros, chat_template=chat_template,
        extra_books=extra_books, token_fn=token_fn, attributes=attributes,
        context_template=context_template,
    ):
        sections.append(ContextSection(title, text, token_fn(text)))

    examples_text = example_block_text(
        card, user_name, custom_macros, context_template=context_template,
    )
    prefixes = _name_prefixes(context_template, user_name, card.name or '')
    examples_as_turns: list[dict[str, str]] = []
    if example_placement == 'system' and examples_text:
        sections.append(ContextSection(
            'Example Dialogue (system prompt)', examples_text, token_fn(examples_text),
        ))
    elif example_placement != 'post_history':
        examples_as_turns = example_messages_for_card(card, user_name, custom_macros)

    for i, ex in enumerate(examples_as_turns, 1):
        role = ex.get('role', 'assistant')
        content = (prefixes or {}).get(role, '') + ex.get('content', '')
        sections.append(ContextSection(
            f'Example {i} ({role})', content, token_fn(content),
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
        counted = content
        if attachments:
            title += f' — {len(attachments)} attachment(s)'
            # Count (and show) what the wire actually sends: history_for_api
            # prepends text attachments to the content, so counting content
            # alone under-reported the token label and the "over the
            # configured context size" warning.
            text_blocks = [
                _attachment_text_block(a) for a in attachments
                if isinstance(a, dict) and a.get('kind') == 'text'
            ]
            if text_blocks:
                counted = '\n\n'.join(text_blocks + ([content] if content else []))
        counted = (prefixes or {}).get(role, '') + counted
        sections.append(ContextSection(title, counted, token_fn(counted)))

    if example_placement == 'post_history' and examples_text:
        sections.append(ContextSection(
            'Example Dialogue (post-history)', examples_text, token_fn(examples_text),
        ))

    phi_parts = []
    phi = post_history_text(card, user_name, custom_macros)
    if phi:
        phi_parts.append(phi)
    tpl_phi = (template_post_history or '').strip()
    if tpl_phi:
        phi_parts.append(substitute_macros(
            tpl_phi, user_name, card.name or '', custom_macros,
        ))
    phi = '\n\n'.join(p for p in phi_parts if p)
    if phi:
        phi = render_post_history(context_template, phi)
        sections.append(ContextSection('Post-History Instructions', phi, token_fn(phi)))

    jb = (jailbreak or '').strip()
    if jb:
        jb_text = substitute_macros(jb, user_name, card.name or '', custom_macros)
        jb_text = render_jailbreak(context_template, jb_text)
        sections.append(ContextSection('Jailbreak', jb_text, token_fn(jb_text)))

    return ContextPlan(
        sections=sections,
        total_tokens=sum(s.tokens for s in sections),
    )

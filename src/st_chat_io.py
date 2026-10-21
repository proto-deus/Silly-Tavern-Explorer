from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from src import safe_json

logger = logging.getLogger(__name__)

#: Limits for an imported ``.jsonl`` chat.  The file is untrusted input.
MAX_IMPORT_MESSAGES = 20000
MAX_LINE_CHARS = 4 * 1024 * 1024
MAX_MESSAGE_CHARS = 1024 * 1024

# SillyTavern stamps send_date as ``YYYY-MM-DD @HHhMMm SSs MMMms``.
_SEND_DATE_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2})[ T]@?(\d{2})h\s*(\d{2})m\s*(\d{2})s\s*(\d{3})ms'
)


def _st_send_date(ts: str | None = None) -> str:
    """Format a timestamp the way SillyTavern writes ``send_date``."""
    moment: datetime | None = None
    if ts:
        try:
            moment = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            moment = None
    if moment is None:
        moment = datetime.now()
    return moment.strftime('%Y-%m-%d @%Hh%Mm %Ss 000ms')


def _parse_send_date(value) -> str:
    """Convert a SillyTavern ``send_date`` back into an ISO timestamp string."""
    if not isinstance(value, str) or not value.strip():
        return datetime.now().isoformat(timespec='seconds')
    match = _SEND_DATE_RE.search(value)
    if match:
        date, hour, minute, second = match.group(1), match.group(2), match.group(3), match.group(4)
        return f'{date}T{hour}:{minute}:{second}'
    try:
        return datetime.fromisoformat(value.strip()).isoformat(timespec='seconds')
    except (TypeError, ValueError):
        return datetime.now().isoformat(timespec='seconds')


def _variant_position(value, default: int) -> int:
    """Coerce a stored ``variant_pos``/``swipe_id`` to a safe int.

    Hand-edited session/chat files can carry a non-numeric value; the old
    bare ``int()`` aborted the whole export (and, in Qt slots, the process).
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _variant_list(value) -> list[str]:
    """Coerce a stored ``variants``/``swipes`` field to a list of strings.

    A dict-valued field used to be iterated as its keys; anything but a
    list/tuple is treated as "no variants".
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value]


def export_st_chat(
    messages: list[dict],
    user_name: str = 'User',
    char_name: str = 'Assistant',
    chat_name: str = '',
) -> str:
    """Serialize chat *messages* as a SillyTavern ``.jsonl`` chat file.

    The first line is SillyTavern's header object (``user_name`` /
    ``character_name`` / ``create_date``); each following line is one message
    with ``is_user`` / ``name`` / ``send_date`` / ``mes``.  Stored alternates
    (swipes) are exported as the message's ``swipes`` list.  Pure function.
    """
    lines: list[str] = [json.dumps({
        'user_name': user_name or 'User',
        'character_name': char_name or 'Assistant',
        'create_date': _st_send_date(),
        'chat': chat_name or 'chat',
        'mes': '',
    }, ensure_ascii=False)]
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get('role', 'user')
        is_user = role == 'user'
        # A system message is not a character turn: SillyTavern marks it
        # with ``is_system``, and writing it as an assistant turn would put
        # the app's own prompt into the character's mouth.
        is_system = role == 'system'
        entry: dict = {
            'is_user': is_user,
            'is_system': is_system,
            'name': user_name if is_user else char_name,
            'send_date': _st_send_date(m.get('ts')),
            'mes': str(m.get('content', '') or ''),
        }
        variants = _variant_list(m.get('variants'))
        if len(variants) > 1:
            entry['swipes'] = variants
            pos = _variant_position(
                m.get('variant_pos', len(variants) - 1), len(variants) - 1,
            )
            entry['swipe_id'] = max(0, min(pos, len(variants) - 1))
        lines.append(json.dumps(entry, ensure_ascii=False))
    return '\n'.join(lines) + '\n'


def import_st_chat(text: str) -> list[dict]:
    """Parse a SillyTavern ``.jsonl`` chat into internal message dicts.

    The header line and system-flagged lines are skipped; ``is_user`` maps to
    the ``user`` / ``assistant`` role and multi-swipe messages keep their
    alternates in ``variants``.  Malformed lines are skipped rather than
    aborting the import.  Pure function.

    The number of messages and the size of a single message are capped: the
    file is untrusted input, and a chat with a million lines (or one with a
    100 MB line) would otherwise be loaded into memory in full.
    """
    messages: list[dict] = []
    for line in (text or '').splitlines():
        if len(messages) >= MAX_IMPORT_MESSAGES:
            logger.warning(
                "The chat file has more than %d messages; the rest was skipped.",
                MAX_IMPORT_MESSAGES,
            )
            break
        line = line.strip()
        if not line:
            continue
        if len(line) > MAX_LINE_CHARS:
            logger.warning("A chat line is too long; it was skipped.")
            continue
        try:
            obj = safe_json.loads_safe(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if 'is_user' not in obj:
            # Header line (user_name/character_name/create_date/mes).
            continue
        if obj.get('is_system'):
            continue
        content = obj.get('mes', '')
        if isinstance(content, (dict, list)):
            continue
        msg: dict = {
            'role': 'user' if obj.get('is_user') else 'assistant',
            'content': str(content or '')[:MAX_MESSAGE_CHARS],
            'ts': _parse_send_date(obj.get('send_date')),
        }
        swipes = obj.get('swipes')
        if isinstance(swipes, list) and len(swipes) > 1:
            variants = [str(s) for s in swipes]
            msg['variants'] = variants
            try:
                pos = int(obj.get('swipe_id', len(variants) - 1) or 0)
            except (TypeError, ValueError):
                pos = len(variants) - 1
            msg['variant_pos'] = max(0, min(pos, len(variants) - 1))
        messages.append(msg)
    return messages

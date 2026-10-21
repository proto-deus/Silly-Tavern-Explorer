"""Validation of model-generated content before it is written into a card.

The Generate tab writes the model's reply straight into the card, so a
malformed reply could store a JSON fragment as a tag, a 200 000-character
"description", or a list where a string belongs.  The helpers here turn a
raw model reply into values that are safe to persist.

They are deliberately Qt-free so they can be unit-tested directly.
"""

from __future__ import annotations

#: Longest tag accepted from a model reply.  Anything longer is prose, not
#: a tag, and writing it into a card would pollute the tag filter.
TAG_MAX_LEN = 64

#: Characters that never occur in a tag.  Their presence means the model
#: returned a JSON fragment instead of a tag.
TAG_FORBIDDEN = '{}[]"\'`'

#: Upper bound for a single generated text field.  A runaway generation
#: ("repeat this sentence 5000 times") would otherwise be written into the
#: card file and slow down every later edit of that card.
TEXT_MAX_LEN = 20000

#: Upper bound for the number of generated tags / alternate greetings.
LIST_MAX_ITEMS = 50

#: Fields that become *model instructions* when the card is used in a chat
#: (they are injected as system-level instructions).  Saving them deserves
#: an explicit confirmation.
INSTRUCTION_FIELDS = ('system_prompt', 'post_history_instructions')

#: Fields whose value is a list of strings.
LIST_FIELDS = ('tags', 'alternate_greetings')

#: The card fields the "Fill Missing Fields" action may write.  A model
#: reply that names anything else (including a typo of a real field) is
#: ignored: the result is written with ``setattr`` onto the card object.
FILLABLE_FIELDS = (
    'description',
    'personality',
    'scenario',
    'first_mes',
    'mes_example',
    'creator_notes',
    'system_prompt',
    'post_history_instructions',
    'tags',
    'alternate_greetings',
)


def clean_tag(value: object) -> str:
    """Normalise one model-produced tag, or return ``''`` to drop it."""
    if isinstance(value, bool):
        return ''
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ''
    tag = value.strip().lower()
    if not tag or len(tag) > TAG_MAX_LEN:
        return ''
    if any(ch in tag for ch in TAG_FORBIDDEN):
        return ''
    return tag


def clean_tags(values) -> list[str]:
    """Turn a list of model-produced values into a clean, de-duplicated tag list."""
    tags: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        if isinstance(value, dict):
            # ``[{"tag": "fantasy"}]`` - take the first sensible string field.
            inner = None
            for key in ('tag', 'name', 'value', 'label', 'keyword'):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    inner = candidate
                    break
            if inner is None:
                continue
            value = inner
        tag = clean_tag(value)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    return tags


def clean_string_list(values) -> list[str]:
    """Turn a list of model-produced values into a list of clean strings."""
    items: list[str] = []
    for value in values or []:
        if isinstance(value, dict):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        items.append(text[:TEXT_MAX_LEN])
        if len(items) >= LIST_MAX_ITEMS:
            break
    return items


def coerce_text(value: object) -> str:
    """Coerce a model-produced value into a text field, or ``''`` to skip it."""
    if isinstance(value, bool):
        return ''
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ''
    return value.strip()[:TEXT_MAX_LEN]


def validate_fill_result(
    data: dict,
    requested: list[str],
) -> tuple[dict, list[str]]:
    """Validate a "fill missing fields" reply.

    Only the fields the user asked for are considered, and only values of
    the right shape are kept:

    * ``tags`` / ``alternate_greetings`` must be lists - a plain string is
      split on commas instead of being stored as a single tag;
    * every other field must be text;
    * values are length-capped.

    Returns ``(clean_data, applied_fields)``.
    """
    clean: dict = {}
    applied: list[str] = []
    if not isinstance(data, dict):
        return clean, applied
    for field_name in requested or []:
        if field_name not in data:
            continue
        if field_name not in FILLABLE_FIELDS:
            # Never write an arbitrary model-named attribute onto the card.
            continue
        value = data[field_name]
        if field_name == 'tags':
            if isinstance(value, str):
                value = [part for part in value.split(',')]
            if not isinstance(value, list):
                continue
            tags = clean_tags(value)
            if not tags:
                continue
            clean[field_name] = tags
            applied.append(field_name)
        elif field_name in LIST_FIELDS:
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                continue
            items = clean_string_list(value)
            if not items:
                continue
            clean[field_name] = items
            applied.append(field_name)
        else:
            text = coerce_text(value)
            if not text:
                continue
            clean[field_name] = text
            applied.append(field_name)
    return clean, applied

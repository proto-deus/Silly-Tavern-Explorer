from __future__ import annotations

import logging
from typing import Any

from src.card_models import CharacterCard

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default prompt templates
#
# Placeholders use ``{name}``-style fields filled in by ``substitute``.
# Unknown placeholders resolve to an empty string so a partially-edited
# template never raises ``KeyError``.
# ---------------------------------------------------------------------------

DEFAULT_TAGS_SYSTEM = (
    'You are a character card tag generator. Given a character description, '
    'output ONLY a JSON array of relevant tags. Tags should be lowercase, '
    'single words or short phrases. Example: ["fantasy", "mage", "wise", "mentor"]. '
    'Output ONLY the JSON array, nothing else.'
)

DEFAULT_TAGS_USER = (
    'Character: {name}\n'
    'Description: {description}\n'
    'Personality: {personality}\n'
    'Scenario: {scenario}'
    '{length}'
    '{extra}'
)

DEFAULT_MISSING_TAGS_SYSTEM = (
    'You are a character card tag assistant. Given an existing character card '
    'and its current tags, identify additional tags that are missing but would '
    'be appropriate based on the rest of the card data. Output ONLY a JSON '
    'array of the NEW tags to add, lowercase, single words or short phrases. '
    'Do NOT repeat any tag that already exists. If nothing is missing, output '
    'an empty JSON array []. Output ONLY the JSON array, nothing else.'
)

DEFAULT_MISSING_TAGS_USER = (
    'Character: {name}\n'
    'Description: {description}\n'
    'Personality: {personality}\n'
    'Scenario: {scenario}\n'
    'Existing tags: {existing_tags}'
    '{length}'
    '{extra}'
)

DEFAULT_SUMMARY_SYSTEM = (
    'You are a character card summarizer. Given a character\'s full data, '
    'write a concise summary of who they are, their personality, '
    'and their setting. Output ONLY the summary text.'
)

DEFAULT_SUMMARY_USER = (
    'Name: {name}\n'
    'Description: {description}\n'
    'Personality: {personality}\n'
    'Scenario: {scenario}\n'
    'First Message: {first_mes}'
    '{length}'
    '{extra}'
)

DEFAULT_ALT_GREETINGS_SYSTEM = (
    'You are a character card assistant. Given a character\'s full data, '
    'write a single alternate greeting message for the character. An '
    'alternate greeting is an alternative opening message the character '
    'could use to start a conversation, written in the character\'s own '
    'voice. Output ONLY the greeting text, nothing else.'
)

DEFAULT_ALT_GREETINGS_USER = (
    'Name: {name}\n'
    'Description: {description}\n'
    'Personality: {personality}\n'
    'Scenario: {scenario}\n'
    'First Message: {first_mes}'
    '{extra}'
    '{length}'
)

DEFAULT_CHARACTER_SYSTEM = (
    'You are a creative character card designer for roleplay AI. '
    'Generate a complete, vivid character card as a JSON object matching the '
    'SillyTavern V2 character card spec.\n\n'
    'Write high-quality, specific content for each field:\n'
    '- description: 2-4 sentences covering appearance, role, and demeanor.\n'
    '- personality: 3-6 concrete traits plus speech patterns, habits, fears, and quirks.\n'
    '- scenario: 1-3 sentences establishing the setting and the character\'s current situation.\n'
    '- first_mes: an engaging opening message written in the character\'s own voice.\n'
    '- mes_example: 2-3 short dialogue exchanges using <START> markers.\n'
    '- creator_notes: a brief summary of who the character is.\n'
    '- system_prompt: instructions for the AI on how to faithfully play this character.\n'
    '- post_history_instructions: guidelines for continuing the scene once chat history ends.\n'
    '- alternate_greetings: 2-3 alternative opening messages.\n'
    '- tags: 5-10 lowercase, specific tags.\n\n'
    'The JSON must include these fields inside a "data" object: name, description, '
    'personality, scenario, first_mes, mes_example (using <START> markers), creator_notes, '
    'system_prompt, post_history_instructions, alternate_greetings (array of 2-3 strings), '
    'tags (array of 5-10 lowercase tags), creator (use "AI Generated"), '
    'character_version ("1.0"), and extensions with talkativeness (0.5) and fav (false). '
    'Also include top-level: spec ("chara_card_v2"), spec_version ("2.0"), name, '
    'description, personality, scenario, first_mes, mes_example, avatar ("none"), '
    'chat (""), talkativeness (0.5), fav (false), tags, create_date (""), '
    'and a "data" object containing all the sub-fields.\n\n'
    'Make characters vivid, detailed, and interesting, and avoid clichés and '
    'placeholder text. Output ONLY the JSON object, no markdown fences or explanation.'
)

DEFAULT_FILL_SYSTEM = (
    'You are a character card assistant. You will be given an existing character card '
    'with some fields already filled in, and a list of fields that need to be generated. '
    'Generate ONLY the requested missing fields, keeping them consistent with the '
    'existing character data. Output a JSON object containing ONLY the fields you are '
    'generating (do not include fields that already exist). '
    'For "tags", output a JSON array of 5-10 lowercase tags. '
    'For "alternate_greetings", output a JSON array of 2-3 strings. '
    'For "mes_example", use <START> markers. '
    'Output ONLY the JSON object, no markdown fences or explanation.'
    '{length}'
    '{extra}'
)

DEFAULT_FILL_USER = (
    'Character Name: {name}\n\n'
    'Existing fields:\n'
    '{existing_fields}\n\n'
    'Fields to generate:\n'
    '{fields_to_generate}'
)

DEFAULT_CHARACTER_USER = (
    'Concept: {concept}'
    '\nAdditional instructions: {extra}'
    '{length}'
)

# Character-creation wizard interview prompts (guided step-by-step questions).
DEFAULT_WIZARD_SYSTEM = (
    'You are a friendly, expert character-creation assistant for SillyTavern. '
    'Your goal is to interview the user and turn their ideas into a vivid, '
    'memorable roleplay character. Guide them through the process one step at '
    'a time, asking exactly one focused question per turn and probing for '
    'concrete, specific detail.\n\n'
    'The process covers these steps, in order:\n'
    '1. Name — settle on a name that fits the setting, tone, and personality. '
    'Offer suggestions and ask about meaning, nicknames, or titles.\n'
    '2. Appearance — explore build, age, hair, eyes, distinctive features, '
    'clothing, posture, and anything that makes the character visually unique.\n'
    '3. Personality — uncover core traits, values, motivations, fears, flaws, '
    'speech patterns, habits, and how they react under stress or around others.\n'
    '4. Backstory & Scenario — establish their history, key life events, current '
    'situation, relationships, and the world or setting they inhabit.\n'
    '5. First Message — understand how they would open a conversation: tone, '
    'voice, actions, and the situation in which they meet the user.\n'
    '6. Extra Details — gather anything remaining: relationships, goals, secrets, '
    'abilities, or world-building that rounds the character out.\n\n'
    'Rules: stay on the current step; ask exactly one question; make it concrete '
    'and easy to answer in a sentence or two; and build on what the user has '
    'already shared rather than repeating earlier steps.'
)

DEFAULT_WIZARD_USER = (
    'Current step: {topic}\n'
    'Context gathered so far:\n{context}\n\n'
    'Ask the user ONE specific, open-ended question for this step. Make it '
    'concrete and tailored to what is already known, and do not re-ask anything '
    'that is already covered in the context above.'
)

# Default chat-preview system prompt (Phase 6D).
DEFAULT_CHAT_SYSTEM = (
    'You are roleplaying as {name} in an ongoing, never-ending roleplay chat. '
    'Stay fully in character at all times and never mention being an AI.\n'
    'Write {name}\'s spoken dialogue inside "quotes" and actions, gestures, '
    'and emphasis inside *asterisks*.\n'
    'Respond with vivid, specific detail; move the scene forward; and vary '
    'sentence structure so replies never feel repetitive.\n'
    'Never write dialogue, actions, thoughts, or decisions for {{user}} — '
    'end your turn to give them room to respond. Keep replies focused.\n\n'
    'Description: {description}\n'
    'Personality: {personality}\n'
    'Scenario: {scenario}'
)

DEFAULT_MEMORY_SUMMARY_SYSTEM = (
    'You are a roleplay conversation summarizer. Write a concise, factual '
    'summary of the key events, decisions, and character developments in the '
    'conversation so the scene can continue seamlessly later. Output ONLY the '
    'summary text.'
)

DEFAULT_MEMORY_SUMMARY_USER = (
    'Summarize the following conversation into a compact memory (events, '
    'characters involved, important details, and the current situation):\n\n'
    '{conversation}'
)

# Lorebook (world info) generation prompts.
DEFAULT_LOREBOOK_SYSTEM = (
    'You are an expert lorebook (world info) designer for roleplay AI. Given a '
    'concept and optional context, produce lorebook entries: concise, factual '
    'world-building blocks that get injected into the AI\'s context only when '
    'their trigger keywords appear in the conversation.\n'
    'Output ONLY a JSON object of the form:\n'
    '{"name": "book name", "description": "one-line book description", '
    '"entries": [{"name": "entry title", "keys": ["lowercase trigger", ...], '
    '"content": "entry text", "insertion_order": 100}]}\n\n'
    'Entry rules:\n'
    '- content: 1-3 sentences of neutral third-person world-building. Never '
    'address the user or reference the chat itself.\n'
    '- keys: 2-6 lowercase words or short phrases that would naturally appear '
    'in conversation when this entry is relevant (names, places, terms).\n'
    '- insertion_order: lower numbers = higher priority; use multiples of 100.\n'
    '- Cover distinct topics; do not duplicate information already present in '
    'the character card or existing entries.\n'
    'Output ONLY the JSON object, no markdown fences or explanation.'
)

DEFAULT_LOREBOOK_USER = (
    'Concept/topic: {concept}\n'
    '{count}'
    '{card_context}'
    '{existing_entries}'
    '{length}'
    '{extra}'
)

DEFAULT_LOREBOOK_ENTRY_SYSTEM = (
    'You are a world-info writer for roleplay AI lorebooks. Write the content '
    'for ONE lorebook entry: concise, factual third-person world-building that '
    'will be injected into the AI\'s context when its trigger keywords appear. '
    'Never address the user or reference the chat. Output ONLY the entry text '
    '(no titles, no JSON, no commentary).'
)

DEFAULT_LOREBOOK_ENTRY_USER = (
    'Write the lorebook entry content for "{entry_name}".\n'
    'Trigger keywords: {keys}\n'
    '{book_description}'
    '{card_context}'
    '{other_entries}'
    '{length}'
    '{extra}'
)

# Keys under which customized prompts are stored in QSettings.
PROMPT_KEYS: list[str] = [
    'tags_system',
    'tags_user',
    'missing_tags_system',
    'missing_tags_user',
    'summary_system',
    'summary_user',
    'alt_greetings_system',
    'alt_greetings_user',
    'character_system',
    'character_user',
    'chat_system',
    'fill_system',
    'fill_user',
    'wizard_system',
    'wizard_user',
    'memory_summary_system',
    'memory_summary_user',
    'lorebook_system',
    'lorebook_user',
    'lorebook_entry_system',
    'lorebook_entry_user',
]

_DEFAULTS: dict[str, str] = {
    'tags_system': DEFAULT_TAGS_SYSTEM,
    'tags_user': DEFAULT_TAGS_USER,
    'missing_tags_system': DEFAULT_MISSING_TAGS_SYSTEM,
    'missing_tags_user': DEFAULT_MISSING_TAGS_USER,
    'summary_system': DEFAULT_SUMMARY_SYSTEM,
    'summary_user': DEFAULT_SUMMARY_USER,
    'alt_greetings_system': DEFAULT_ALT_GREETINGS_SYSTEM,
    'alt_greetings_user': DEFAULT_ALT_GREETINGS_USER,
    'character_system': DEFAULT_CHARACTER_SYSTEM,
    'character_user': DEFAULT_CHARACTER_USER,
    'chat_system': DEFAULT_CHAT_SYSTEM,
    'fill_system': DEFAULT_FILL_SYSTEM,
    'fill_user': DEFAULT_FILL_USER,
    'wizard_system': DEFAULT_WIZARD_SYSTEM,
    'wizard_user': DEFAULT_WIZARD_USER,
    'memory_summary_system': DEFAULT_MEMORY_SUMMARY_SYSTEM,
    'memory_summary_user': DEFAULT_MEMORY_SUMMARY_USER,
    'lorebook_system': DEFAULT_LOREBOOK_SYSTEM,
    'lorebook_user': DEFAULT_LOREBOOK_USER,
    'lorebook_entry_system': DEFAULT_LOREBOOK_ENTRY_SYSTEM,
    'lorebook_entry_user': DEFAULT_LOREBOOK_ENTRY_USER,
}


class _SafeDict(dict):
    """dict subclass returning '' for missing keys during str.format_map."""

    def __missing__(self, key: str) -> str:  # noqa: D401
        return ''


def substitute(template: str, **fields: Any) -> str:
    """Substitute ``{placeholder}`` fields in *template* with *fields*.

    Never raises: unknown placeholders resolve to empty strings, and
    templates with malformed format syntax (literal ``{"a": 1}`` JSON
    examples, unclosed braces) fall back to macro-only substitution so a
    user-edited template cannot crash generation.  Literal braces in the
    template should be escaped as ``{{`` / ``}}`` per Python ``str.format``.
    """
    if not template:
        return ''
    try:
        return template.format_map(_SafeDict(fields))
    except (ValueError, IndexError, AttributeError, TypeError):
        # Malformed format string (e.g. a literal JSON example in the
        # template). Degrade gracefully: leave placeholders untouched.
        logger.warning("Prompt template has invalid format syntax; using it unformatted")
        return template


def default_prompt(key: str) -> str:
    """Return the built-in default template for *key*."""
    return _DEFAULTS.get(key, '')


def load_prompt(key: str) -> str:
    """Load a prompt template from QSettings, falling back to the default.

    Returns the default when the key is unknown or the stored value is empty.
    """
    if key not in _DEFAULTS:
        return ''
    from src.settings_manager import _get_settings
    settings = _get_settings()
    stored = settings.value(f'ai/prompts/{key}', '')
    if stored and isinstance(stored, str) and stored.strip():
        return stored
    return _DEFAULTS[key]


def save_prompt(key: str, value: str) -> None:
    """Persist a customized prompt template (or remove it if empty/reset)."""
    from src.settings_manager import _get_settings
    settings = _get_settings()
    if value and value.strip() and value != _DEFAULTS.get(key):
        settings.setValue(f'ai/prompts/{key}', value)
    else:
        settings.remove(f'ai/prompts/{key}')


def load_all_prompts() -> dict[str, str]:
    """Load every prompt template (customized or default)."""
    return {key: load_prompt(key) for key in PROMPT_KEYS}


def reset_prompt(key: str) -> str:
    """Reset *key* to its default and return the default text."""
    from src.settings_manager import _get_settings
    settings = _get_settings()
    settings.remove(f'ai/prompts/{key}')
    return _DEFAULTS.get(key, '')


# ---------------------------------------------------------------------------
# Prompt builders — assemble (system, user) for each generation mode.
# ---------------------------------------------------------------------------


def _card_fields(card: CharacterCard) -> dict[str, Any]:
    return {
        'name': card.name or '',
        'description': card.description or '',
        'personality': card.personality or '',
        'scenario': card.scenario or '',
        'first_mes': card.first_mes or '',
    }


def build_tags_prompts(
    card: CharacterCard,
    templates: dict[str, str] | None = None,
    extra: str = '',
    length: str = '',
) -> tuple[str, str]:
    """Build the (system, user) prompts for tag generation.

    *templates* overrides the loaded/default templates (used for live preview
    in the prompt settings dialog and for unit tests).
    """
    tmpl = templates or {}
    system = tmpl.get('tags_system') or load_prompt('tags_system')
    user = tmpl.get('tags_user') or load_prompt('tags_user')
    fields = _card_fields(card)
    fields['extra'] = f'\nAdditional instructions: {extra}' if extra else ''
    fields['length'] = f'\nGenerate approximately {length} tags.' if length else ''
    return substitute(system, **fields), substitute(user, **fields)


def build_missing_tags_prompts(
    card: CharacterCard,
    templates: dict[str, str] | None = None,
    extra: str = '',
    length: str = '',
) -> tuple[str, str]:
    """Build the (system, user) prompts for suggesting *additional* tags.

    Unlike :func:`build_tags_prompts`, the card's existing tags are included so
    the model only returns tags that are missing.
    """
    tmpl = templates or {}
    system = tmpl.get('missing_tags_system') or load_prompt('missing_tags_system')
    user = tmpl.get('missing_tags_user') or load_prompt('missing_tags_user')
    fields = _card_fields(card)
    fields['existing_tags'] = ', '.join(card.tags) if card.tags else '(none)'
    fields['extra'] = f'\nAdditional instructions: {extra}' if extra else ''
    fields['length'] = f'\nSuggest approximately {length} additional tags.' if length else ''
    return substitute(system, **fields), substitute(user, **fields)


def build_summary_prompts(
    card: CharacterCard,
    templates: dict[str, str] | None = None,
    extra: str = '',
    length: str = '',
) -> tuple[str, str]:
    tmpl = templates or {}
    system = tmpl.get('summary_system') or load_prompt('summary_system')
    user = tmpl.get('summary_user') or load_prompt('summary_user')
    fields = _card_fields(card)
    fields['extra'] = f'\nAdditional instructions: {extra}' if extra else ''
    fields['length'] = f'\nTarget length: approximately {length} words.' if length else ''
    return substitute(system, **fields), substitute(user, **fields)


def build_alt_greetings_prompts(
    card: CharacterCard,
    templates: dict[str, str] | None = None,
    extra: str = '',
    length: str = '',
) -> tuple[str, str]:
    tmpl = templates or {}
    system = tmpl.get('alt_greetings_system') or load_prompt('alt_greetings_system')
    user = tmpl.get('alt_greetings_user') or load_prompt('alt_greetings_user')
    fields = _card_fields(card)
    fields['extra'] = f'\nAdditional instructions: {extra}' if extra else ''
    fields['length'] = f'\nTarget length: approximately {length} words.' if length else ''
    return substitute(system, **fields), substitute(user, **fields)


def build_character_prompts(
    concept: str,
    extra: str = '',
    templates: dict[str, str] | None = None,
    length: str = '',
) -> tuple[str, str]:
    tmpl = templates or {}
    system = tmpl.get('character_system') or load_prompt('character_system')
    user = tmpl.get('character_user') or load_prompt('character_user')
    length_str = f'\nTarget length: approximately {length} words total across all fields.' if length else ''
    return substitute(system, concept=concept or '', extra=extra or '', length=length_str), \
        substitute(user, concept=concept or '', extra=extra or '', length=length_str)


def build_chat_system(card: CharacterCard, templates: dict[str, str] | None = None) -> str:
    """Build the chat-preview system prompt for *card*."""
    tmpl = templates or {}
    system = tmpl.get('chat_system') or load_prompt('chat_system')
    fields = _card_fields(card)
    return substitute(system, **fields)


def build_memory_summary_prompts(
    conversation: str,
    templates: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Build the (system, user) prompts for summarizing a conversation into memory."""
    tmpl = templates or {}
    system = tmpl.get('memory_summary_system') or load_prompt('memory_summary_system')
    user = tmpl.get('memory_summary_user') or load_prompt('memory_summary_user')
    return substitute(system), substitute(user, conversation=conversation or '')


def _lorebook_card_block(card: CharacterCard | None) -> str:
    """Multi-line card summary for lorebook prompts ('' when no card)."""
    if card is None:
        return ''
    parts: list[str] = []
    if card.name:
        parts.append(f'Character: {card.name}')
    if card.description:
        parts.append(f'Description: {card.description}')
    if card.personality:
        parts.append(f'Personality: {card.personality}')
    if card.scenario:
        parts.append(f'Scenario: {card.scenario}')
    if not parts:
        return ''
    return 'Card context (keep entries consistent with this character):\n' + '\n'.join(parts) + '\n'


def build_lorebook_prompts(
    concept: str,
    card: CharacterCard | None = None,
    count: str = '',
    existing_summary: str = '',
    length: str = '',
    extra: str = '',
    templates: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Build the (system, user) prompts for lorebook entry generation.

    Used both for generating a full book from scratch and for appending
    entries to an existing book (*existing_summary* describes what already
    exists so the model avoids duplicating it).
    """
    tmpl = templates or {}
    system = tmpl.get('lorebook_system') or load_prompt('lorebook_system')
    user = tmpl.get('lorebook_user') or load_prompt('lorebook_user')
    count_str = f'Target number of new entries: {count}\n' if count else ''
    length_str = f'\nTarget length: approximately {length} words per entry.' if length else ''
    extra_str = f'\nAdditional instructions: {extra}' if extra else ''
    existing_str = ''
    if existing_summary and existing_summary.strip():
        existing_str = (
            'Existing entries (do NOT duplicate these topics):\n'
            f'{existing_summary.strip()}\n'
        )
    return (
        substitute(system),
        substitute(
            user,
            concept=concept or '',
            count=count_str,
            card_context=_lorebook_card_block(card),
            existing_entries=existing_str,
            length=length_str,
            extra=extra_str,
        ),
    )


def build_lorebook_entry_prompts(
    entry_name: str,
    keys: list[str],
    card: CharacterCard | None = None,
    book_description: str = '',
    other_entries: str = '',
    length: str = '',
    extra: str = '',
    templates: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Build the (system, user) prompts for filling ONE entry's content."""
    tmpl = templates or {}
    system = tmpl.get('lorebook_entry_system') or load_prompt('lorebook_entry_system')
    user = tmpl.get('lorebook_entry_user') or load_prompt('lorebook_entry_user')
    desc_str = f'Book description: {book_description}\n' if book_description else ''
    length_str = f'\nTarget length: approximately {length} words.' if length else ''
    extra_str = f'\nAdditional instructions: {extra}' if extra else ''
    other_str = ''
    if other_entries and other_entries.strip():
        other_str = (
            'Other entries in this book (match their style, do not repeat):\n'
            f'{other_entries.strip()}\n'
        )
    return (
        substitute(system),
        substitute(
            user,
            entry_name=entry_name or '(untitled)',
            keys=', '.join(keys) if keys else '(none)',
            book_description=desc_str,
            card_context=_lorebook_card_block(card),
            other_entries=other_str,
            length=length_str,
            extra=extra_str,
        ),
    )


def build_fill_prompts(
    card: CharacterCard,
    fields_to_generate: list[str],
    templates: dict[str, str] | None = None,
    extra: str = '',
    field_lengths: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Build the (system, user) prompts for filling missing fields."""
    tmpl = templates or {}
    system = tmpl.get('fill_system') or load_prompt('fill_system')
    user = tmpl.get('fill_user') or load_prompt('fill_user')

    existing_parts: list[str] = []
    if card.name:
        existing_parts.append(f"Name: {card.name}")
    if card.description:
        existing_parts.append(f"Description: {card.description}")
    if card.personality:
        existing_parts.append(f"Personality: {card.personality}")
    if card.scenario:
        existing_parts.append(f"Scenario: {card.scenario}")
    if card.first_mes:
        existing_parts.append(f"First Message: {card.first_mes}")
    if card.mes_example:
        existing_parts.append(f"Example Messages: {card.mes_example}")
    if card.creator_notes:
        existing_parts.append(f"Creator Notes: {card.creator_notes}")
    if card.system_prompt:
        existing_parts.append(f"System Prompt: {card.system_prompt}")
    if card.post_history_instructions:
        existing_parts.append(f"Post-History Instructions: {card.post_history_instructions}")
    if card.tags:
        existing_parts.append(f"Tags: {', '.join(card.tags)}")

    generate_descriptions = {
        'description': 'Description (detailed character description)',
        'personality': 'Personality (traits, quirks, behavioral patterns)',
        'scenario': 'Scenario (setting and context for the conversation)',
        'first_mes': 'First Message (the character\'s opening message)',
        'mes_example': 'Example Messages (using <START> markers)',
        'creator_notes': 'Creator Notes (summary of the character)',
        'system_prompt': 'System Prompt (instructions for the AI on how to play this character)',
        'post_history_instructions': 'Post-History Instructions (guidelines after chat history)',
        'tags': 'Tags (array of 5-10 lowercase tags)',
        'alternate_greetings': 'Alternate Greetings (array of 2-3 alternative opening messages)',
    }
    gen_lines = []
    lengths = field_lengths or {}
    for f in fields_to_generate:
        desc = generate_descriptions.get(f, f)
        len_hint = lengths.get(f, '')
        if len_hint:
            if f in ('tags',):
                gen_lines.append(f"- {desc} [~{len_hint} tags]")
            elif f in ('alternate_greetings',):
                gen_lines.append(f"- {desc} [~{len_hint} greetings]")
            else:
                gen_lines.append(f"- {desc} [~{len_hint} words]")
        else:
            gen_lines.append(f"- {desc}")

    length_str = ''
    if lengths:
        length_str = '\nFollow the approximate length targets shown in brackets for each field above.'

    fields_dict = _card_fields(card)
    return substitute(system, **fields_dict, extra=f'\nAdditional instructions: {extra}' if extra else '', length=length_str), substitute(
        user,
        name=card.name or '',
        existing_fields='\n'.join(existing_parts) if existing_parts else '(none)',
        fields_to_generate='\n'.join(gen_lines),
    )


def build_character_from_answers(
    answers: dict[str, str],
    templates: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Build the (system, user) prompts for generating a card from guided answers.

    *answers* maps a field label (name/appearance/personality/scenario/
    first_mes/extra) to the user's collected answer.  Reuses the existing
    character-generation system prompt so the result is a full V2 card.
    """
    tmpl = templates or {}
    system = tmpl.get('character_system') or load_prompt('character_system')
    labels = (
        ('name', 'Name'),
        ('appearance', 'Appearance'),
        ('personality', 'Personality'),
        ('scenario', 'Scenario'),
        ('first_mes', 'First Message'),
        ('extra', 'Additional Details'),
    )
    parts: list[str] = []
    for key, label in labels:
        value = (answers.get(key) or '').strip()
        if value:
            parts.append(f'{label}: {value}')
    body = '\n\n'.join(parts) if parts else '(no details provided)'
    user = (
        'Create a complete character card as a JSON object based on the '
        f'following details:\n\n{body}\n\nOutput ONLY the JSON object, '
        'no markdown fences or explanation.'
    )
    return system, user


def build_wizard_question_prompts(
    topic: str,
    context: str = '',
    templates: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Build the (system, user) prompts for a wizard interview question."""
    tmpl = templates or {}
    system = tmpl.get('wizard_system') or load_prompt('wizard_system')
    user = tmpl.get('wizard_user') or load_prompt('wizard_user')
    return substitute(system), substitute(user, topic=topic or '', context=context or '(nothing yet)')


def validate_placeholders(template: str) -> list[str]:
    """Return the list of placeholders referenced by *template*.

    Used by the prompt settings dialog to warn the user about typos.  Pure
    function so it can be unit-tested without Qt.
    """
    import re
    # Match {name} but not {{ or }} (escaped braces).
    matches = re.findall(r'(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})', template)
    seen: list[str] = []
    for m in matches:
        if m not in seen:
            seen.append(m)
    return seen

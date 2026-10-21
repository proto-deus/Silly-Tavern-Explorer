"""Character-card linter.

Checks a card for the defects that make it misbehave in a chat: missing
fields, placeholder text left over from a template, a description that
overshoots the context window, an unbalanced quote in the example
dialogue, lorebook entries that can never fire, and text that tries to
instruct the model instead of describing the character.

The linter only *reports*.  A card is third-party content, so a phrase
that looks like a prompt-injection attempt is shown to the user and
nothing else happens: the text is never obeyed, removed or rewritten.

Pure logic (no Qt) so it is unit-testable and safe to run on a worker
thread.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from src.card_models import CharacterCard, parse_character_book

logger = logging.getLogger(__name__)

SEVERITY_ERROR = 'error'
SEVERITY_WARNING = 'warning'
SEVERITY_INFO = 'info'

#: A description longer than this is unreadable in the editor and eats the
#: context window before the conversation even starts.
MAX_DESCRIPTION_CHARS = 4000

#: Default ceiling for the estimated prompt size of a single card.
DEFAULT_TOKEN_LIMIT = 8192

#: The card spec this app writes; older cards still work but are worth a note.
EXPECTED_SPEC = 'chara_card_v3'

#: The spec version each known spec is expected to carry.
EXPECTED_SPEC_VERSIONS = {
    'chara_card_v1': '1.0',
    'chara_card_v2': '2.0',
    'chara_card_v3': '3.0',
}

#: Phrases that look like an attempt to talk to the model rather than to the
#: user.  They are reported, never executed.
INJECTION_PHRASES = (
    'ignore previous instructions',
    'ignore all previous instructions',
    'system prompt',
    'you are now',
    'disregard the above',
    'developer mode',
    'reveal your prompt',
)

#: Obvious leftovers from a template or a work-in-progress card.
PLACEHOLDER_PATTERNS = (
    re.compile(r'\bTODO\b', re.IGNORECASE),
    re.compile(r'lorem\s+ipsum', re.IGNORECASE),
    re.compile(r'\bXXX\b'),
    re.compile(r'<placeholder>', re.IGNORECASE),
)


@dataclass
class LintIssue:
    """One problem found in a card."""

    code: str            # e.g. 'empty-description'
    severity: str        # 'error' | 'warning' | 'info'
    message: str
    field: str = ''      # the card field it concerns
    hint: str = ''       # a short suggestion


def _text(value: Any) -> str:
    """Return *value* as a stripped string ('' for None)."""
    return str(value or '').strip()


#: The card fields that must not be empty: (code, severity, field, message, hint).
FIELD_CHECKS = (
    ('empty-name', SEVERITY_ERROR, 'name',
     'The card has no name.',
     'Give the character a name - it is used as the chat title.'),
    ('empty-description', SEVERITY_ERROR, 'description',
     'The card has no description.',
     'The description is the main thing the model knows about the character.'),
    ('empty-personality', SEVERITY_WARNING, 'personality',
     'The card has no personality.',
     'A short trait list keeps the character consistent.'),
    ('empty-scenario', SEVERITY_WARNING, 'scenario',
     'The card has no scenario.',
     'Describe where the chat takes place.'),
    ('empty-first-mes', SEVERITY_ERROR, 'first_mes',
     'The card has no first message.',
     'Without a greeting the chat cannot start.'),
    ('missing-alt-greetings', SEVERITY_INFO, 'alternate_greetings',
     'The card has no alternate greetings.',
     'Alternate greetings make repeated chats less repetitive.'),
    ('no-tags', SEVERITY_INFO, 'tags',
     'The card has no tags.',
     'Tags are how the card is found in the library.'),
    ('no-creator', SEVERITY_INFO, 'creator',
     'The card has no creator name.',
     'Credit the author so the card can be shared properly.'),
)


def _check_required_fields(card: CharacterCard) -> list[LintIssue]:
    """The fields a card cannot do without (and the ones it should have)."""
    issues: list[LintIssue] = []
    for code, severity, field_name, message, hint in FIELD_CHECKS:
        value = getattr(card, field_name, '')
        if isinstance(value, str):
            empty = not value.strip()
        else:
            empty = not value
        if empty:
            issues.append(LintIssue(code, severity, message, field_name, hint))
    return issues


def _check_description_size(card: CharacterCard) -> list[LintIssue]:
    """A description that is far too long to be read or to fit."""
    description = str(card.description or '')
    if len(description) <= MAX_DESCRIPTION_CHARS:
        return []
    return [LintIssue(
        'huge-description', SEVERITY_WARNING,
        f'The description is {len(description)} characters long '
        f'(over {MAX_DESCRIPTION_CHARS}).',
        'description',
        'Move the bulk of it into a lorebook entry so the prompt stays small.',
    )]


def _estimate_tokens(card: CharacterCard) -> int:
    """Estimated token count of the card's prompt-facing text (0 if unknown).

    ``count_tokens`` is imported lazily: the tokenizer is an optional
    runtime dependency and a missing one must not break the import of
    this module (or the rest of the linting).
    """
    try:
        from src.token_counter import count_tokens
    except Exception:      # pragma: no cover - the tokenizer is a hard dep
        logger.warning('Token counter unavailable; skipping the token check')
        return 0
    try:
        return count_tokens(card.full_text_for_tokens())
    except Exception:
        logger.exception('Could not estimate the token count of the card')
        return 0


def _check_token_budget(card: CharacterCard, token_limit: int) -> list[LintIssue]:
    """The card does not fit into the token budget of a single turn."""
    limit = int(token_limit or 0)
    if limit <= 0:
        return []
    tokens = _estimate_tokens(card)
    if tokens <= 0 or tokens <= limit:
        return []
    return [LintIssue(
        'token-budget', SEVERITY_WARNING,
        f'The card uses about {tokens} tokens, over the {limit}-token budget.',
        'description',
        'Shorten the card so the conversation still fits into the context window.',
    )]


def _check_spec(card: CharacterCard) -> list[LintIssue]:
    """The card does not use the expected spec / spec version."""
    spec = _text(card.spec)
    version = _text(card.spec_version)
    problems: list[str] = []
    if spec != EXPECTED_SPEC:
        problems.append(f"spec is {spec or 'unset'!r} (expected {EXPECTED_SPEC!r})")
    expected_version = EXPECTED_SPEC_VERSIONS.get(spec)
    if expected_version is None:
        if not version:
            problems.append('spec_version is unset')
    elif version != expected_version:
        problems.append(
            f"spec_version is {version or 'unset'!r} (expected {expected_version!r})",
        )
    if not problems:
        return []
    return [LintIssue(
        'spec-version', SEVERITY_INFO,
        'Card spec is not the expected ' + EXPECTED_SPEC + ': ' + '; '.join(problems),
        'spec',
        'Older cards still work; export the card again to upgrade it.',
    )]


def _check_example_dialogue(card: CharacterCard) -> list[LintIssue]:
    """The example dialogue contains an unbalanced double quote."""
    text = str(card.mes_example or '')
    if not text.strip():
        return []
    if text.count('"') % 2:
        return [LintIssue(
            'unclosed-example-dialogue', SEVERITY_WARNING,
            'The example dialogue has an unbalanced double quote.', 'mes_example',
            'Close the quote so the example reads as intended.',
        )]
    return []


def _check_character_book(card: CharacterCard) -> list[LintIssue]:
    """The card's lorebook has entries that can never fire."""
    if not getattr(card, 'character_book', None):
        return []
    book = parse_character_book(card.character_book)
    issues: list[LintIssue] = []
    seen_keys: set[str] = set()
    for index, entry in enumerate(book.entries):
        label = entry.name or f'entry {index + 1}'
        if not _text(entry.content):
            issues.append(LintIssue(
                'character-book-issues', SEVERITY_WARNING,
                f"Lorebook entry '{label}' has no content.", 'character_book',
                'Give the entry some text, or delete it.',
            ))
        keys = [k for k in (entry.keys or []) if _text(k)]
        if not keys:
            issues.append(LintIssue(
                'character-book-issues', SEVERITY_WARNING,
                f"Lorebook entry '{label}' has no keys, so it can never trigger.",
                'character_book',
                'Add at least one key word to the entry.',
            ))
        for key in keys:
            lowered = key.casefold()
            if lowered in seen_keys:
                issues.append(LintIssue(
                    'character-book-issues', SEVERITY_WARNING,
                    f"Lorebook key '{key}' is used by more than one entry.",
                    'character_book',
                    'Duplicate keys make the activation order ambiguous.',
                ))
            else:
                seen_keys.add(lowered)
    return issues


def _check_injection(card: CharacterCard) -> list[LintIssue]:
    """The description or personality tries to instruct the model."""
    issues: list[LintIssue] = []
    for field_name in ('description', 'personality'):
        text = str(getattr(card, field_name, '') or '').casefold()
        for phrase in INJECTION_PHRASES:
            if phrase not in text:
                continue
            issues.append(LintIssue(
                'suspicious-prompt-injection', SEVERITY_WARNING,
                f"The {field_name} contains '{phrase}', which reads like an "
                'instruction to the model.',
                field_name,
                'Card text is sent to the model as-is; remove anything that '
                'tries to change its instructions.',
            ))
    return issues


def _check_placeholders(card: CharacterCard) -> list[LintIssue]:
    """The description still carries template placeholder text."""
    description = str(card.description or '')
    issues: list[LintIssue] = []
    seen: set[str] = set()
    for pattern in PLACEHOLDER_PATTERNS:
        for match in pattern.finditer(description):
            found = match.group(0)
            if found.casefold() in seen:
                continue
            seen.add(found.casefold())
            issues.append(LintIssue(
                'placeholder-text', SEVERITY_INFO,
                f"The description still contains the placeholder '{found}'.",
                'description',
                'Replace the placeholder with real text.',
            ))
    return issues


def lint_card(card: CharacterCard, *, token_limit: int = DEFAULT_TOKEN_LIMIT) -> list[LintIssue]:
    """Check *card* and return the problems found, most severe first.

    Args:
        card: The card to check.  Any object with the character-card
            attributes works; the card is never modified.
        token_limit: Ceiling for the estimated prompt size of the card.
            Values below one disable the token-budget check.

    Returns:
        The issues found, sorted by severity (errors, then warnings, then
        notes).  An empty list means the card passed every check.
    """
    issues: list[LintIssue] = []
    issues.extend(_check_required_fields(card))
    issues.extend(_check_description_size(card))
    issues.extend(_check_token_budget(card, token_limit))
    issues.extend(_check_spec(card))
    issues.extend(_check_example_dialogue(card))
    issues.extend(_check_character_book(card))
    issues.extend(_check_injection(card))
    issues.extend(_check_placeholders(card))
    order = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}
    issues.sort(key=lambda issue: (order.get(issue.severity, 3), issue.code, issue.field))
    return issues


def lint_summary(issues: list[LintIssue]) -> str:
    """One-line summary of *issues*, suitable for a status bar.

    Args:
        issues: The issues returned by :func:`lint_card`.

    Returns:
        A one-line summary counting the errors, warnings and notes.
    """
    issues = list(issues or [])
    if not issues:
        return 'Card check passed - no problems found.'
    errors = sum(1 for issue in issues if issue.severity == SEVERITY_ERROR)
    warnings = sum(1 for issue in issues if issue.severity == SEVERITY_WARNING)
    notes = len(issues) - errors - warnings
    parts: list[str] = []
    if errors:
        parts.append(f'{errors} error(s)')
    if warnings:
        parts.append(f'{warnings} warning(s)')
    if notes:
        parts.append(f'{notes} note(s)')
    return 'Card check: ' + ', '.join(parts) + '.'

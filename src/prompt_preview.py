"""Preview of the exact prompt a chat turn would send.

A chat request is assembled from a dozen sources: the main prompt template,
the card's character definition, the user persona, world info, persistent
attributes, chat memory, the example dialogue, the history with its depth
injections, and the post-history instructions.  This module builds that
payload through the *real* pipeline in :mod:`src.chat_builder` and reports it
as labelled blocks with token counts, so the UI can show "what will actually
be sent" instead of an approximation that can drift away from the send path.

Pure logic: no Qt, no network, no settings access - so the module can run on
a worker thread.  The caller supplies the main prompt template, the personas
and (optionally) the token counter, which keeps the result reproducible.

The preview has two layers:

* ``blocks`` describe the *whole* prompt the pipeline would contribute,
  including the parts the context window will not fit.  Their sum is
  ``total_tokens``, and ``pressure`` compares it against the context limit.
* ``messages`` is the payload as it would be posted: the same assembly,
  trimmed to the context window by :func:`src.ai_client.trim_messages` and
  with the depth injections spliced in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.ai_client import trim_messages
from src.card_models import CharacterBook, CharacterCard
from src.chat_builder import (
    ContextTemplate,
    PromptInjection,
    apply_depth_injections,
    apply_persona,
    assemble_system,
    example_block_text,
    history_for_api,
    injections_for_chat,
    system_prompt_blocks,
)

logger = logging.getLogger(__name__)

#: Depth the Author's Note is injected at (SillyTavern's default).
AUTHOR_NOTE_DEPTH = 4

#: Preview labels for the pipeline's own section titles.  The titles come
#: from :func:`src.chat_builder.system_prompt_blocks`; they are only renamed
#: so the preview reads as prose ("User persona") and so the world-info block
#: is recognisable as the character book.
_LABEL_MAP = {
    'System Prompt': 'System prompt',
    'World Info': 'Character book',
    'World Info (after character)': 'Character book (after character)',
    'User Persona': 'User persona',
    'Persistent Attributes': 'Persistent attributes',
    'Chat Memory': 'Chat memory',
    'Example Dialogue (system prompt)': 'Example dialogue',
    'Example Dialogue (post-history)': 'Example dialogue (post-history)',
    'Post-History Instructions': 'Post-history instructions',
    'Jailbreak': 'Jailbreak',
}


@dataclass
class PreviewBlock:
    """One labelled piece of the outgoing prompt."""

    label: str
    text: str
    tokens: int
    role: str = ''


@dataclass
class PromptPreview:
    """The full preview of a chat turn's prompt.

    ``blocks`` describes the whole prompt, ``total_tokens`` is its size and
    ``messages`` is the payload that would actually be posted.  ``system_text``
    is the system message of that payload.
    """

    blocks: list[PreviewBlock] = field(default_factory=list)
    total_tokens: int = 0
    context_limit: int = 8192
    system_text: str = ''
    messages: list[dict] = field(default_factory=list)

    @property
    def pressure(self) -> float:
        """Fraction of the context window the prompt fills (0.0 - 1.0+)."""
        if not self.context_limit or self.context_limit <= 0:
            return 0.0
        return self.total_tokens / self.context_limit

    def block_by_label(self, label: str) -> Optional[PreviewBlock]:
        """Return the first block labelled *label* (None when there is none)."""
        for block in self.blocks:
            if block.label == label:
                return block
        return None


def _content_text(content: Any) -> str:
    """Flatten a message ``content`` field into plain text.

    Handles the plain-string form and the OpenAI-style multimodal part list
    (only the text parts carry tokens).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                parts.append(str(item.get('text', '') or ''))
        return '\n'.join(parts)
    if content is None:
        return ''
    return str(content)


def _message_text(row: dict) -> str:
    """The text a message contributes, including its text attachments."""
    text = _content_text(row.get('content', ''))
    blocks = [
        f"[Attached file: {a.get('name', 'file')}]\n{a.get('data', '')}"
        for a in (row.get('attachments') or [])
        if isinstance(a, dict) and a.get('kind') == 'text'
    ]
    if blocks:
        return '\n\n'.join(blocks + ([text] if text else []))
    return text


def _persona_entries(personas: Any) -> list[tuple[str, str]]:
    """Normalise the persona argument into ``(name, text)`` pairs.

    Accepts the app's persona dicts (``{'id', 'name', 'text'}``), plain
    strings, a single dict/string, or any object with a ``text`` attribute.
    Blank personas are dropped.
    """
    if not personas:
        return []
    if isinstance(personas, (str, dict)):
        personas = [personas]
    entries: list[tuple[str, str]] = []
    for item in personas:
        if isinstance(item, str):
            entries.append(('', item))
        elif isinstance(item, dict):
            entries.append((
                str(item.get('name', '') or ''),
                str(item.get('text', '') or ''),
            ))
        else:
            entries.append(('', str(getattr(item, 'text', '') or '')))
    return [(name, text) for name, text in entries if text.strip()]


def _coerce_injections(raw: Any) -> list[PromptInjection]:
    """Normalise caller-supplied depth injections into PromptInjection objects."""
    out: list[PromptInjection] = []
    for item in raw or []:
        if isinstance(item, PromptInjection):
            out.append(item)
        elif isinstance(item, dict):
            try:
                depth = int(item.get('depth', 0) or 0)
            except (TypeError, ValueError):
                depth = 0
            out.append(PromptInjection(
                depth=max(0, depth),
                role=str(item.get('role', 'system') or 'system'),
                content=str(item.get('content', '') or ''),
                title=str(item.get('title', '') or ''),
            ))
    return out


def build_preview(
    card: CharacterCard,
    messages: list[dict],
    *,
    system_prompt: str = '',
    context_limit: int = 8192,
    max_tokens: int = 0,
    personas: Any = None,
    memories: list[dict] | None = None,
    attributes: list[dict] | None = None,
    author_note: str | dict | None = None,
    jailbreak: str = '',
    post_history: str = '',
    examples_block: str = '',
    example_turns: list[dict] | None = None,
    depth_injections: Any = None,
    extra_books: list[CharacterBook] | None = None,
    token_fn: Callable[[str], int] | None = None,
    user_name: str = 'User',
    name_prefixes: dict[str, str] | None = None,
    custom_macros: dict[str, str] | None = None,
    context_template: ContextTemplate | None = None,
) -> PromptPreview:
    """Build the preview of the prompt a chat turn would send.

    Args:
        card: The character card the chat belongs to.
        messages: The chat history (internal message dicts).
        system_prompt: The main prompt template.  An empty string means "no
            template" - only the card's own ``system_prompt`` is used, which
            keeps the preview independent of the user's saved settings.
        context_limit: The provider's context window, in tokens.
        max_tokens: The reply budget the send path reserves; the preview
            trims with the same reserve so the payload it shows is the
            payload that would be sent.
        personas: The user personas to include (see :func:`_persona_entries`).
        memories: The chat-memory entries to include.
        attributes: The persistent attributes to include.
        author_note: The Author's Note.  Either the text (injected at
            SillyTavern's default depth 4 as a system message) or a full
            ``{'text', 'depth', 'role'}`` dict, which is what the Context
            Inspector's note editor produces.
        jailbreak: The user's own trailing instructions (depth 0).
        post_history: The card's / template's post-history instructions.
        examples_block: The example dialogue as a trailing block (the
            "post-history" placement).
        example_turns: The example dialogue as fake history turns (the
            legacy "history" placement).
        depth_injections: Extra depth injections to preview on top of the
            ones the pipeline derives (the note and ``at_depth`` world info).
        extra_books: Standalone lorebooks scanned after the card's own book.
        token_fn: The token counter; defaults to
            :func:`src.token_counter.count_tokens`.
        user_name: The ``{{user}}`` name used in the persona block.
        name_prefixes: The context template's name prefixes (a role -> prefix
            map), applied to the history exactly as the send path does.
        custom_macros: The user's custom macros (substituted in the card's
            own text).
        context_template: The active context template.  It changes how the
            blocks are framed and separated, so the preview has to use the
            same one as the send path.

    Returns:
        The :class:`PromptPreview` with the labelled blocks, the token
        totals and the payload that would be posted.
    """
    if token_fn is None:
        from src.token_counter import count_tokens as token_fn
    limit = max(1, int(context_limit or 1))
    if card is None:
        return PromptPreview(context_limit=limit)

    history = [m for m in (messages or []) if isinstance(m, dict)]
    # The example dialogue can be part of the system text, a trailing
    # block, or fake history turns.  The preview must show the same
    # placement the send path uses, so all three are accepted.
    api_history = list(example_turns or []) + history
    persona_entries = _persona_entries(personas)
    persona_text = '\n\n'.join(text for _name, text in persona_entries)

    note = None
    if isinstance(author_note, dict):
        # A full note (text + depth + role) - the Context Inspector's note
        # editor produces one, and the depth matters for the preview.
        if (author_note.get('text') or '').strip():
            note = dict(author_note)
    elif (author_note or '').strip():
        note = {'text': author_note, 'depth': AUTHOR_NOTE_DEPTH, 'role': 'system'}

    injections = list(injections_for_chat(
        card, history, author_note=note, extra_books=extra_books,
        user_name=user_name, custom_macros=custom_macros, token_fn=token_fn,
    ))
    injections.extend(_coerce_injections(depth_injections))

    blocks: list[PreviewBlock] = []
    for title, text in system_prompt_blocks(
        card, history, user_name=user_name, persona=persona_text,
        memories=memories, custom_macros=custom_macros,
        chat_template=system_prompt, extra_books=extra_books,
        token_fn=token_fn, attributes=attributes,
        context_template=context_template,
    ):
        if title == 'User Persona':
            # The pipeline folds every persona into one block; the preview
            # shows them separately so it is clear how many are in play.
            for name, persona_body in persona_entries:
                persona_text_block = apply_persona('', persona_body, user_name)
                label = 'User persona' if not name else f'User persona ({name})'
                blocks.append(PreviewBlock(
                    label, persona_text_block, token_fn(persona_text_block), 'system',
                ))
            continue
        blocks.append(PreviewBlock(
            _LABEL_MAP.get(title, title), text, token_fn(text), 'system',
        ))

    examples_text = example_block_text(
        card, user_name, custom_macros, context_template=context_template,
    )
    if examples_text and not examples_block:
        # The example dialogue is part of the system message.
        blocks.append(PreviewBlock(
            'Example dialogue', examples_text, token_fn(examples_text), 'system',
        ))

    message_no = 0
    for row in apply_depth_injections(api_history, injections, with_titles=True):
        role = str(row.get('role', 'user') or 'user')
        text = _message_text(row)
        title = row.get('_title')
        if title:
            blocks.append(PreviewBlock(
                f'Context injection: {title}', text, token_fn(text), 'context',
            ))
            continue
        message_no += 1
        blocks.append(PreviewBlock(
            f'Chat history {message_no} ({role})', text, token_fn(text), role,
        ))

    if examples_block and examples_block.strip():
        blocks.append(PreviewBlock(
            'Example dialogue (post-history)', examples_block,
            token_fn(examples_block), 'system',
        ))
    if post_history and post_history.strip():
        blocks.append(PreviewBlock(
            'Post-history instructions', post_history,
            token_fn(post_history), 'system',
        ))
    if jailbreak and jailbreak.strip():
        blocks.append(PreviewBlock(
            'Jailbreak', jailbreak, token_fn(jailbreak), 'system',
        ))

    system_text = assemble_system(
        card, history, user_name=user_name, persona=persona_text,
        memories=memories, custom_macros=custom_macros,
        chat_template=system_prompt, extra_books=extra_books,
        token_fn=token_fn, attributes=attributes,
        context_template=context_template,
    )
    if examples_text and not examples_block:
        system_text = (
            f'{system_text}\n\n{examples_text}' if system_text.strip() else examples_text
        )

    # The payload is assembled exactly like the send path
    # (AIClient.generate_chat): the system message, the history, then the
    # trailing blocks in the order examples -> post-history -> jailbreak.
    payload: list[dict] = []
    if system_text.strip():
        payload.append({'role': 'system', 'content': system_text})
    payload.extend(history_for_api(api_history, name_prefixes))
    trailing = [
        block for block in (examples_block, post_history, jailbreak)
        if block and block.strip()
    ]
    for block in trailing:
        payload.append({'role': 'system', 'content': block})

    reserve = sum(token_fn(_content_text(i.content)) for i in injections)
    trimmed = trim_messages(
        payload, limit, max_tokens, token_fn=token_fn, reserve=reserve,
    )
    if trailing:
        # The trailing blocks are depth-0 blocks themselves, so the depth
        # is counted from the end of the conversation, not from the end of
        # the payload - exactly like the send path.
        head, tail = trimmed[:-len(trailing)], trimmed[-len(trailing):]
        wire = apply_depth_injections(head, injections, with_titles=False) + tail
    else:
        wire = apply_depth_injections(trimmed, injections, with_titles=False)

    sent_system = ''
    for message in wire:
        if message.get('role') == 'system':
            sent_system = _content_text(message.get('content', ''))
            break

    return PromptPreview(
        blocks=blocks,
        total_tokens=sum(block.tokens for block in blocks),
        context_limit=limit,
        system_text=sent_system,
        messages=wire,
    )

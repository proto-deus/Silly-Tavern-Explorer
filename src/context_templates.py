"""Context templates: how the chat context is framed and named (ST-style).

SillyTavern's *context templates* control the text formatting of the prompt:
role names, sequence separators, example-dialogue formatting, chat start.
This app speaks OpenAI-compatible chat-completions APIs (structured message
lists), so the templates adapt the knobs that still apply there:

* framing of the labelled system blocks (``[Description]`` etc.),
* names behavior - prepending ``Name: `` to history messages (important for
  roleplay and group chats),
* example-dialogue rendering (speaker prefixes and the block separator),
* wrapping of the trailing post-history / jailbreak blocks,
* the conversation-start header used when a greeting folds into the system
  message (strict chat templates).

Custom and built-in templates are managed like the system prompts: the
"Default" template reproduces the app's classic output exactly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_TEMPLATE_ID = 'default'

# Names behavior choices: which history messages get a ``Name: `` prefix.
NAMES_BEHAVIOR_CHOICES = ('none', 'char', 'user', 'both')


@dataclass
class ContextTemplate:
    """Formatting rules applied when the chat context is assembled."""

    id: str
    name: str
    builtin: bool = False
    # Framing of labelled system blocks ({title} and {body} placeholders).
    block_format: str = '[{title}]\n{body}'
    # Framing of the main system-prompt block ({body}).
    main_format: str = '{body}'
    # Text placed between system blocks.
    block_separator: str = '\n\n'
    # Names behavior for chat history: 'none' | 'char' | 'user' | 'both'.
    names_behavior: str = 'none'
    # How a speaker name is rendered before a history line ({name}).
    name_format: str = '{name}: '
    # Example-dialogue rendering ({{user}} / {{char}} are macro-substituted).
    example_separator: str = '<START>'
    example_user_prefix: str = '{{user}}: '
    example_char_prefix: str = '{{char}}: '
    # Wrapping of the trailing blocks ({body}).
    post_history_format: str = '{body}'
    jailbreak_format: str = '{body}'
    # Header used when a leading assistant greeting folds into the system
    # message (strict chat templates).
    conversation_start_header: str = '[Conversation start]'

    def to_dict(self) -> dict:
        """The persisted form (everything except ``builtin``)."""
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out.pop('builtin', None)
        return out

    @classmethod
    def from_dict(cls, data: dict) -> 'ContextTemplate':
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        kwargs.setdefault('id', '')
        kwargs.setdefault('name', 'Context Template')
        return cls(**kwargs)


def default_template() -> ContextTemplate:
    """The "Default" template: reproduces the app's classic output."""
    return ContextTemplate(id=DEFAULT_CONTEXT_TEMPLATE_ID, name='Default', builtin=True)


def _builtin(
    slug: str, name: str, **overrides,
) -> ContextTemplate:
    tpl = ContextTemplate(id=f'ctx:{slug}', name=name, builtin=True)
    for key, value in overrides.items():
        setattr(tpl, key, value)
    return tpl


_BUILTIN_SPECS = (
    # Formatting presets adapted from SillyTavern's context/sysprompt default
    # content (AGPL-3.0, https://github.com/SillyTavern/SillyTavern) to what
    # chat-completions APIs can express.
    _builtin(
        'simple-chat', 'Simple Chat',
        block_format='{title}:\n{body}',
        example_user_prefix='User: ',
        example_char_prefix='Assistant: ',
        example_separator='---',
        conversation_start_header='---',
    ),
    _builtin(
        'clean-prose', 'Clean Prose',
        block_format='{body}',
        example_separator='***',
    ),
    _builtin(
        'markdown', 'Markdown Sections',
        block_format='### {title}\n{body}',
        main_format='### System prompt\n{body}',
        conversation_start_header='### Conversation start',
    ),
    _builtin(
        'chatml', 'ChatML-Style',
        block_format='## {title}\n{body}',
        main_format='## System\n{body}',
        names_behavior='both',
        conversation_start_header='## Conversation start',
    ),
    _builtin(
        'alpaca', 'Alpaca-Style',
        block_format='### {title}\n{body}',
        main_format='### Instructions\n{body}',
        example_user_prefix='User: ',
        example_char_prefix='Assistant: ',
        example_separator='###',
        conversation_start_header='### Conversation start',
    ),
    _builtin(
        'roleplay', 'Roleplay',
        names_behavior='both',
    ),
    _builtin(
        'group-chat', 'Group Chat',
        block_format='{title}:\n{body}',
        names_behavior='both',
    ),
    _builtin(
        'text-adventure', 'Text Adventure',
        block_format='{title}:\n{body}',
        names_behavior='both',
        name_format='{name}> ',
        example_user_prefix='{{user}}> ',
        example_char_prefix='{{char}}> ',
    ),
    _builtin(
        'story-mode', 'Story Mode',
        block_format='{body}',
        names_behavior='both',
        example_separator='***',
    ),
    _builtin(
        'assistant', 'Assistant',
        block_format='{title}:\n{body}',
        example_user_prefix='User: ',
        example_char_prefix='Assistant: ',
        example_separator='---',
        conversation_start_header='---',
        post_history_format='{body}',
        jailbreak_format='{body}',
    ),
    _builtin(
        'structured-notes', 'Structured Notes',
        block_format='[{title}]\n{body}',
        main_format='[{title}]\n{body}',
    ),
)


def builtin_templates() -> list[ContextTemplate]:
    """Read-only templates: Default first, then the built-in presets."""
    out = [default_template()]
    out.extend(_BUILTIN_SPECS)
    return out


def user_templates() -> list[ContextTemplate]:
    """The user-defined templates (may be empty)."""
    from src.settings_manager import load_context_templates

    return [ContextTemplate.from_dict(d) for d in load_context_templates()]


def save_user_templates(templates: list[ContextTemplate]) -> None:
    """Persist the user-defined templates (built-ins are never stored)."""
    from src.settings_manager import save_context_templates

    save_context_templates([t.to_dict() for t in templates if not t.builtin])


def all_templates() -> list[ContextTemplate]:
    """Default, built-ins, then user-defined - the combo-box order."""
    return builtin_templates() + user_templates()


def get_template(template_id: str | None) -> ContextTemplate | None:
    """Look up a template by id; ''/None resolves to Default."""
    if not template_id or template_id == DEFAULT_CONTEXT_TEMPLATE_ID:
        return default_template()
    for tpl in all_templates():
        if tpl.id == template_id:
            return tpl
    return None


def resolve_template(template_id: str | None) -> ContextTemplate:
    """Like :func:`get_template` but falls back to Default for stale ids.

    Sessions outlive deleted templates, so a stored id that no longer exists
    must still render the context.
    """
    return get_template(template_id) or default_template()


def active_context_template_id() -> str:
    """The globally active template id (new chats start with it)."""
    from src.settings_manager import load_active_context_template

    return load_active_context_template()


def save_active_context_template_id(template_id: str) -> None:
    from src.settings_manager import save_active_context_template

    save_active_context_template(template_id or DEFAULT_CONTEXT_TEMPLATE_ID)


# ---- rendering helpers (used by chat_builder) ----


def _format(fmt: str, fallback: str, **kwargs) -> str:
    """``str.format`` that never raises on a malformed user template."""
    try:
        return (fmt or fallback).format(**kwargs)
    except (KeyError, IndexError, ValueError):
        pass
    try:
        return fallback.format(**kwargs) if '{' in fallback else fallback
    except (KeyError, IndexError, ValueError):
        # The fallback itself is malformed: return it verbatim rather than
        # letting the error escape into the send path.
        return fallback


def render_block(template: ContextTemplate | None, title: str, body: str) -> str:
    """Render one labelled system block under *template*'s framing rules."""
    tpl = template or default_template()
    return _format(tpl.block_format, '[{title}]\n{body}', title=title, body=body)


def render_main(template: ContextTemplate | None, body: str) -> str:
    """Render the main system-prompt block (label-free by default)."""
    tpl = template or default_template()
    return _format(tpl.main_format, '{body}', title='System Prompt', body=body)


def render_post_history(template: ContextTemplate | None, body: str) -> str:
    tpl = template or default_template()
    return _format(tpl.post_history_format, '{body}', body=body)


def render_jailbreak(template: ContextTemplate | None, body: str) -> str:
    tpl = template or default_template()
    return _format(tpl.jailbreak_format, '{body}', body=body)


def name_prefixes(
    template: ContextTemplate | None,
    user_name: str,
    char_name: str,
) -> dict[str, str] | None:
    """History name prefixes per role, or None when names are off.

    Returns e.g. ``{'user': 'Bob: ', 'assistant': 'Aria: '}`` according to the
    template's ``names_behavior``, for :func:`chat_builder.history_for_api`.
    """
    tpl = template or default_template()
    behavior = tpl.names_behavior if tpl.names_behavior in NAMES_BEHAVIOR_CHOICES else 'none'
    fmt = tpl.name_format or '{name}: '

    def prefix(name: str) -> str:
        return _format(fmt, '{name}: ', name=name or '')

    out: dict[str, str] = {}
    if behavior in ('user', 'both'):
        out['user'] = prefix(user_name)
    if behavior in ('char', 'both'):
        out['assistant'] = prefix(char_name)
    return out or None

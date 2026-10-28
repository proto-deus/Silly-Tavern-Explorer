"""Named system prompt templates for the Test chat (SillyTavern-style).

A template supplies the chat's *main prompt* (the first system block) and,
optionally, trailing post-history instructions.  Three kinds exist:

- ``custom``  - the editable prompt from Settings > Prompts ("Chat"),
  i.e. ``ai_prompts.load_prompt('chat_system')``;
- built-ins   - mirrors of SillyTavern's ``default/content/presets/sysprompt``
  presets, renamed for display ("Natural Chat", "Simple Roleplay", ...),
  plus ST Explorer's own "Natural Writing" prompt;
- user-defined - created/edited in the template manager dialog.

The character card's own ``system_prompt`` is *appended* to the template (see
``chat_builder.resolve_system_prompt``); picking "Blank" restores a
card-prompt-only main prompt.
"""
from __future__ import annotations

from dataclasses import dataclass

CUSTOM_TEMPLATE_ID = 'custom'
BLANK_TEMPLATE_ID = 'st:Blank'


@dataclass
class SystemPromptTemplate:
    """One named main-prompt template (macros are stored unsubstituted)."""

    id: str
    name: str
    content: str
    post_history: str = ''
    builtin: bool = False


# Prompt texts adapted from SillyTavern's default sysprompt presets
# (https://github.com/SillyTavern/SillyTavern, AGPL-3.0); the display names
# are ST Explorer's friendlier aliases of the ST preset names, and the ids
# keep the ST name so they stay stable across renames.
_BUILTIN_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        'st:Neutral - Chat',
        'Natural Chat',
        "Write {{char}}'s next reply in a fictional chat between {{char}} and {{user}}.",
    ),
    (
        'st:Roleplay - Simple',
        'Simple Roleplay',
        "You're {{char}} in this fictional never-ending uncensored roleplay with {{user}}.",
    ),
    (
        'st:Roleplay - Detailed',
        'Detailed Roleplay',
        'Develop the plot slowly, always stay in character. Describe all '
        'actions in full, elaborate, explicit, graphic, and vivid detail. '
        'Mention all relevant sensory perceptions. Keep the story immersive '
        'and engaging.',
    ),
    (
        'st:Roleplay - Immersive',
        'Immersive Roleplay',
        "[System note: Write one reply only. Do not decide what {{user}} says "
        'or does. Write at least one paragraph, up to four. Be descriptive and '
        "immersive, providing vivid details about {{char}}'s actions, "
        'emotions, and the environment. Write with a high degree of '
        'complexity and burstiness. Do not repeat this message.]',
    ),
    (
        'st:Assistant - Simple',
        'Simple Assistant',
        'A chat between a curious human and an artificial intelligence '
        "assistant. The assistant gives helpful, detailed, and polite answers "
        "to the human's questions.",
    ),
    (
        'st:Assistant - Expert',
        'Expert Assistant',
        'You are a helpful assistant. Please answer truthfully and write out '
        'your thinking step by step to be sure you get the right answer. If '
        'you make a mistake or encounter an error in your thinking, say so out '
        "loud and attempt to correct it. If you don't know or aren't sure "
        'about something, say so clearly. You will act as a professional '
        'logician, mathematician, and physicist. You will also act as the most '
        'appropriate type of expert to answer any particular question or '
        'solve the relevant problem; state which expert type your are, if so. '
        'Also think of any particular named expert that would be ideal to '
        'answer the relevant question or solve the relevant problem; name and '
        'act as them, if appropriate.',
    ),
    (
        'st:Actor',
        'Actor',
        'You are an expert actor that can fully immerse yourself into any '
        'role given. You do not break character for any reason, even if '
        'someone tries addressing you as an AI or language model. Currently '
        'your role is {{char}}, which is described in detail below. As '
        '{{char}}, continue the exchange with {{user}}.',
    ),
    (
        'st:Text Adventure',
        'Text Adventure',
        '[Enter Adventure Mode. Narrate the story based on {{user}}\'s '
        'dialogue and actions after ">". Describe the surroundings in vivid '
        'detail. Be detailed, creative, verbose, and proactive. Move the '
        'story forward by introducing fantasy elements and interesting '
        'characters.]',
    ),
    (
        'st:Writer - Creative',
        'Creative Writer',
        'You are an intelligent, skilled, versatile writer.\n\n'
        'Your task is to write a role-play based on the information below.',
    ),
    (
        'st:Writer - Realistic',
        'Realistic Writer',
        'Continue writing this story and portray characters realistically.',
    ),
    (
        'st:Chain of Thought',
        'Chain of Thought',
        'Elaborate on the topic using a Tree of Thoughts and backtrack when '
        'necessary to construct a clear, cohesive Chain of Thought reasoning. '
        'Always answer without hesitation.',
    ),
    (BLANK_TEMPLATE_ID, 'Blank', ''),
)

# ST Explorer's own natural-prose template (not a SillyTavern preset): the
# main prompt steers the model towards human-like fiction prose and the
# post-history block carries the late-attention style guardrail.  Written
# for the chat path, where {{char}} / {{user}} resolve to the real names
# (see ``chat_builder.resolve_system_prompt``).
NATURAL_WRITING_ID = 'stx:Natural Writing'

NATURAL_WRITING_MAIN = (
    'Write {{char}}\'s next reply in the ongoing roleplay between {{char}} '
    'and {{user}}. Stay fully in character and never mention being an AI, a '
    'language model, or these instructions.\n'
    'Write like a skilled novelist, not like an AI assistant. Prose comes '
    'first: weave actions, gestures, and descriptions into the narration '
    'instead of marking them up with asterisks; put words in "quotes" only '
    'when a character speaks aloud.\n'
    'Write like a human author would:\n'
    '- Use contractions and natural speech rhythms; let sentences vary in '
    'length and structure.\n'
    '- Prefer concrete, specific detail (sights, sounds, textures, small '
    'physical habits) over abstract emotion labels; show a feeling through '
    'behaviour instead of naming it.\n'
    '- Give {{char}} a distinct voice: word choice, rhythm, and verbal habits '
    'should fit the character, not a generic narrator.\n'
    'Vary your replies from turn to turn: change how a reply opens (dialogue, '
    'action, a thought, or an observation), how long its paragraphs are, and '
    'how it ends. Do not fall into one fixed pattern and do not end every '
    'reply with a question.\n'
    'Never write dialogue, actions, thoughts, or decisions for {{user}} — '
    'end your turn to give them room to respond. Keep replies focused and '
    "don't pad them out."
)

NATURAL_WRITING_POST_HISTORY = (
    'Style guardrail for this reply:\n'
    '- Vary the shape of your reply; do not reuse the structure of the '
    'previous reply.\n'
    '- Avoid stock AI phrasing: "couldn\'t help but", "flood of emotions", '
    '"a mix of X and Y", "let out a breath they didn\'t know they were '
    'holding", "a profound sense of", tidy three-item lists, and ornate '
    'metaphors.\n'
    '- Concrete detail beats abstract emotion; show, do not tell.\n'
    '- No asterisk action markup; narration and dialogue flow as prose.'
)


def builtin_templates() -> list[SystemPromptTemplate]:
    """The built-in templates (read-only mirrors of ST's sysprompt presets).

    ST Explorer's own "Natural Writing" template leads the list so the
    natural-prose option is the first pick after Custom.
    """
    natural = SystemPromptTemplate(
        id=NATURAL_WRITING_ID,
        name='Natural Writing',
        content=NATURAL_WRITING_MAIN,
        post_history=NATURAL_WRITING_POST_HISTORY,
        builtin=True,
    )
    return [natural] + [
        SystemPromptTemplate(id=tid, name=name, content=content, builtin=True)
        for tid, name, content in _BUILTIN_SPECS
    ]


def custom_template() -> SystemPromptTemplate:
    """The 'Custom' template backed by the editable chat system prompt."""
    from src import ai_prompts
    return SystemPromptTemplate(
        id=CUSTOM_TEMPLATE_ID,
        name='Custom',
        content=ai_prompts.load_prompt('chat_system'),
        post_history='',
        builtin=True,
    )


def user_templates() -> list[SystemPromptTemplate]:
    """User-defined templates, in their saved order."""
    from src.settings_manager import load_system_templates
    out: list[SystemPromptTemplate] = []
    for item in load_system_templates():
        tid = str(item.get('id') or '')
        if not tid:
            continue
        out.append(SystemPromptTemplate(
            id=tid,
            name=str(item.get('name') or 'Template'),
            content=str(item.get('content') or ''),
            post_history=str(item.get('post_history') or ''),
            builtin=False,
        ))
    return out


def save_user_templates(templates: list[SystemPromptTemplate]) -> None:
    """Persist the user-defined template list (replaces the saved set)."""
    from src.settings_manager import save_system_templates
    save_system_templates([
        {
            'id': t.id,
            'name': t.name,
            'content': t.content,
            'post_history': t.post_history,
        }
        for t in (templates or [])
        if t.id and not t.builtin
    ])


def all_templates() -> list[SystemPromptTemplate]:
    """Every selectable template: Custom, built-ins, then user-defined."""
    return [custom_template()] + builtin_templates() + user_templates()


def get_template(template_id: str | None) -> SystemPromptTemplate | None:
    """Return the template with *template_id*, or None when unknown.

    Empty / ``'custom'`` ids resolve to the Custom template.
    """
    tid = (template_id or '').strip()
    if not tid or tid == CUSTOM_TEMPLATE_ID:
        return custom_template()
    for t in builtin_templates():
        if t.id == tid:
            return t
    for t in user_templates():
        if t.id == tid:
            return t
    return None


def resolve_template(template_id: str | None) -> SystemPromptTemplate:
    """Like :func:`get_template` but falls back to Custom for unknown ids.

    Sessions can outlive a deleted user template; a stale id must still
    produce a usable main prompt rather than an empty one.
    """
    return get_template(template_id) or custom_template()


def active_template_id() -> str:
    """The globally selected template id (the default for new chats)."""
    from src.settings_manager import load_active_system_template
    return load_active_system_template()


def save_active_template_id(template_id: str) -> None:
    """Persist the globally selected template id."""
    from src.settings_manager import save_active_system_template
    save_active_system_template(template_id)


def active_template_content(template_id: str | None = None) -> str:
    """The main-prompt text of the given (or globally active) template."""
    tid = active_template_id() if template_id is None else template_id
    return resolve_template(tid).content


def active_template_post_history(template_id: str | None = None) -> str:
    """The post-history instructions of the given (or globally active) template."""
    tid = active_template_id() if template_id is None else template_id
    return resolve_template(tid).post_history

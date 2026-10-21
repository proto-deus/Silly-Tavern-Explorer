"""Chat session export (Markdown and HTML).

Turns a chat history into a document the user can read, paste or send
to someone else.  Markdown is the plain-text sibling of the HTML
export: same content, no styling, easy to paste into a notes app.

Pure logic (no Qt) so it is unit-testable and safe to run on a worker
thread.  The HTML export escapes every message body with
:func:`html.escape`, so a message can never inject markup into the
page.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The formats :func:`export_session` accepts.
MARKDOWN_FORMATS = ('md', 'markdown')
HTML_FORMATS = ('html', 'htm')

#: Headings used for the known message roles.
ROLE_HEADINGS = {
    'user': 'User',
    'assistant': 'Assistant',
    'system': 'System',
}

#: Fallback heading for a role the exporter does not know.
DEFAULT_ROLE_HEADING = 'Message'


def _role_of(message: Any) -> str:
    """The role of *message* (``'user'`` when it carries none)."""
    if not isinstance(message, dict):
        return 'user'
    return str(message.get('role', 'user') or 'user').strip().lower() or 'user'


def _content_of(message: Any) -> str:
    """The text of *message* ('' when it carries none)."""
    if not isinstance(message, dict):
        return ''
    return str(message.get('content', '') or '')


def _one_line(text: str) -> str:
    """*text* collapsed into a single line (for headings and labels)."""
    return ' '.join(str(text or '').split())


def _md_text(text: str) -> str:
    """*text* with the Markdown inline markup neutralised.

    The card name and the document title are user- (or card-) controlled
    and end up in the document as inline text.  Without this a name like
    ``**bold**`` or ``[click](javascript:...)`` changes how the exported
    document renders - and many Markdown viewers render HTML.
    """
    out = str(text or '')
    for char in ('\\', '`', '*', '_', '[', ']', '<', '>'):
        out = out.replace(char, '\\' + char)
    return out


def _heading_for(role: str) -> str:
    """The display heading of *role*."""
    return ROLE_HEADINGS.get(role, DEFAULT_ROLE_HEADING)


def session_to_markdown(
    messages: list[dict],
    *,
    card_name: str = '',
    title: str = '',
) -> str:
    """Render *messages* as a Markdown transcript.

    Args:
        messages: The chat messages (``{'role', 'content'}`` dicts).
        card_name: Name of the character, shown under the title.
        title: Document title; defaults to a generic heading.

    Returns:
        The Markdown document (always ends with a newline).
    """
    lines: list[str] = [f'# {_md_text(_one_line(title)) or "Chat transcript"}']
    if _one_line(card_name):
        lines.append('')
        lines.append(f'**Character:** {_md_text(_one_line(card_name))}')
    lines.append('')
    for message in messages or []:
        lines.append(f'## {_heading_for(_role_of(message))}')
        lines.append('')
        lines.append(_content_of(message))
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def _html_body(messages: list[dict]) -> str:
    """The message blocks of the HTML export."""
    blocks: list[str] = []
    for message in messages or []:
        role = _role_of(message)
        body = html.escape(_content_of(message)).replace('\n', '<br>\n')
        blocks.append(
            f'<div class="msg {html.escape(role, quote=True)}">'
            f'<div class="role">{html.escape(_heading_for(role))}</div>'
            f'<div class="text">{body}</div>'
            '</div>',
        )
    return '\n'.join(blocks)


def session_to_html(
    messages: list[dict],
    *,
    card_name: str = '',
    title: str = '',
) -> str:
    """Render *messages* as a standalone HTML document.

    The result is a complete page with inline styling, so it looks
    reasonable when opened in a browser without any extra file.  Every
    message body is escaped.

    Args:
        messages: The chat messages (``{'role', 'content'}`` dicts).
        card_name: Name of the character, shown under the title.
        title: Document title; defaults to a generic heading.

    Returns:
        The HTML document.
    """
    heading = _one_line(title) or 'Chat transcript'
    meta: list[str] = []
    if _one_line(card_name):
        meta.append(f'<div class="meta">Character: {html.escape(_one_line(card_name))}</div>')
    body = _html_body(messages) or '<div class="empty">This chat is empty.</div>'
    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        f'<title>{html.escape(heading)}</title>\n'
        '<style>\n'
        '  body { margin: 0; padding: 2rem 1rem; background: #14161d; color: #e7e9f0;\n'
        '         font-family: "Segoe UI", system-ui, -apple-system, sans-serif; }\n'
        '  .wrap { max-width: 46rem; margin: 0 auto; }\n'
        '  h1 { font-size: 1.35rem; margin: 0 0 .25rem; }\n'
        '  .meta { color: #98a1b5; font-size: .85rem; margin-bottom: 1.4rem; }\n'
        '  .msg { background: #1b1e27; border: 1px solid #272b38; border-radius: 10px;\n'
        '         padding: .65rem .85rem; margin: .55rem 0; }\n'
        '  .msg.user { background: #1d2735; }\n'
        '  .msg.system { background: #262019; font-style: italic; }\n'
        '  .role { color: #8fb8ff; font-size: .72rem; font-weight: 600;\n'
        '          letter-spacing: .06em; text-transform: uppercase; margin-bottom: .3rem; }\n'
        '  .text { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.5; }\n'
        '  .empty { color: #98a1b5; font-style: italic; }\n'
        '</style>\n'
        '</head>\n'
        '<body>\n'
        '<div class="wrap">\n'
        f'<h1>{html.escape(heading)}</h1>\n'
        f'{"".join(meta)}\n'
        f'{body}\n'
        '</div>\n'
        '</body>\n'
        '</html>\n'
    )


def export_session(
    messages: list[dict],
    path: str | Path,
    fmt: str = 'md',
    *,
    card_name: str = '',
    title: str = '',
) -> Path:
    """Write *messages* to *path* in the requested format.

    Args:
        messages: The chat messages (``{'role', 'content'}`` dicts).
        path: Where to write the export.
        fmt: ``'md'`` / ``'markdown'`` or ``'html'`` / ``'htm'``.
        card_name: Name of the character, shown in the header.
        title: Document title.

    Returns:
        The path that was written.

    Raises:
        ValueError: *fmt* is not a supported format.
    """
    key = str(fmt or '').strip().lower()
    if key in MARKDOWN_FORMATS:
        text = session_to_markdown(messages, card_name=card_name, title=title)
    elif key in HTML_FORMATS:
        text = session_to_html(messages, card_name=card_name, title=title)
    else:
        raise ValueError(
            f'Unsupported export format {fmt!r} '
            f'(expected one of {", ".join(MARKDOWN_FORMATS + HTML_FORMATS)})',
        )
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode('utf-8'))
    except OSError as exc:
        logger.warning('Could not write the chat export to %s: %s', target, exc)
        raise
    return target

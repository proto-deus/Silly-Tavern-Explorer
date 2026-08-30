from __future__ import annotations

import re

# Default colors for the three inline formatting styles.
DEFAULT_DIALOGUE_COLOR = '#9ad8ff'
DEFAULT_ACTION_COLOR = '#e0a060'
DEFAULT_EMPHASIS_COLOR = '#9be8a0'

# Ordered by precedence: dialogue (quotes) first, then double markers
# (**bold** / __strong__) before single ones (*action* / _emphasis_) so the
# double markers are consumed before the single-marker rules can match
# inside them.  Each is applied over the already-escaped text.
_DIALOGUE_RE = re.compile(r'"([^"\n]+)"')
_BOLD_RE = re.compile(r'\*\*([^*\n]+)\*\*')
_STRONG_EMPH_RE = re.compile(r'(?<!\w)__([^_\n]+)__(?!\w)')
_ACTION_RE = re.compile(r'\*([^*\n]+)\*')
_EMPHASIS_RE = re.compile(r'(?<!\w)_([^_\n]+)_(?!\w)')

# Image-URL detection (for in-chat image previews).
_IMAGE_EXT_RE = re.compile(r'\.(?:png|jpe?g|gif|webp|bmp)(?:$|[?#])', re.IGNORECASE)
_MARKDOWN_IMG_RE = re.compile(r'!\[[^\]]*\]\(\s*([^)\s]+)\s*\)')
_HTML_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_BARE_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.IGNORECASE)


def _is_http_url(url: str) -> bool:
    return url.lower().startswith(('http://', 'https://'))


def extract_image_urls(text: str) -> list[str]:
    """Return the HTTP(S) image URLs referenced by *text*, deduplicated.

    Recognizes markdown ``![alt](url)``, HTML ``<img src="url">``, and bare
    URLs whose path ends in an image extension.  Pure function so it can be
    unit-tested without Qt.
    """
    if not text:
        return []
    urls: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        url = url.strip().rstrip('.,;:!)]}')
        if not _is_http_url(url):
            return
        if url not in seen:
            seen.add(url)
            urls.append(url)

    for m in _MARKDOWN_IMG_RE.finditer(text):
        _add(m.group(1))
    for m in _HTML_IMG_RE.finditer(text):
        _add(m.group(1))
    for m in _BARE_URL_RE.finditer(text):
        raw = m.group(0).rstrip('.,;:!')
        if _IMAGE_EXT_RE.search(raw):
            _add(raw)
    return urls


def _escape_html(text: str) -> str:
    return (text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _safe_color(color: str, default: str) -> str:
    """Return *color* if it's a plain hex/named color, else *default*.

    The value is interpolated into a style attribute; restricting it to
    safe characters prevents a corrupted settings value from injecting
    markup into the generated HTML.
    """
    import re as _re
    c = (color or '').strip()
    if _re.fullmatch(r'#[0-9a-fA-F]{3,8}|[a-zA-Z]+', c):
        return c
    return default


def format_chat_message(
    text: str,
    dialogue_color: str = DEFAULT_DIALOGUE_COLOR,
    action_color: str = DEFAULT_ACTION_COLOR,
    emphasis_color: str = DEFAULT_EMPHASIS_COLOR,
) -> str:
    """Convert chat *text* to HTML with colored inline formatting.

    - ``"text"`` (double quotes)  -> dialogue color; quote marks are kept
    - ``**text**``                -> bold, action color
    - ``*text*`` (asterisks)      -> italic, action color
    - ``__text__``                -> bold, emphasis color
    - ``_text_`` (underscores)    -> italic, emphasis color

    Asterisk/underscore markers are always consumed (hidden) so the chat
    only shows the color/font change; edit dialogs show the raw text.
    Newlines become ``<br>``.  The result is safe to insert into a QTextEdit
    document.  Pure function so it can be unit-tested without Qt.
    """
    dialogue_color = _safe_color(dialogue_color, DEFAULT_DIALOGUE_COLOR)
    action_color = _safe_color(action_color, DEFAULT_ACTION_COLOR)
    emphasis_color = _safe_color(emphasis_color, DEFAULT_EMPHASIS_COLOR)
    escaped = _escape_html(text)

    def _dialogue(m: re.Match) -> str:
        return f'<span style="color:{dialogue_color}">&quot;{m.group(1)}&quot;</span>'

    def _bold(m: re.Match) -> str:
        return f'<b><span style="color:{action_color}">{m.group(1)}</span></b>'

    def _strong_emph(m: re.Match) -> str:
        return f'<b><span style="color:{emphasis_color}">{m.group(1)}</span></b>'

    def _action(m: re.Match) -> str:
        return f'<i><span style="color:{action_color}">{m.group(1)}</span></i>'

    def _emphasis(m: re.Match) -> str:
        return f'<i><span style="color:{emphasis_color}">{m.group(1)}</span></i>'

    html = _DIALOGUE_RE.sub(_dialogue, escaped)
    html = _BOLD_RE.sub(_bold, html)
    html = _STRONG_EMPH_RE.sub(_strong_emph, html)
    html = _ACTION_RE.sub(_action, html)
    html = _EMPHASIS_RE.sub(_emphasis, html)
    return html.replace('\n', '<br>')

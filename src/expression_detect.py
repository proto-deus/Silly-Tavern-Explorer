"""Local emotion detection for expression sprites (pure, no Qt).

Given the text of a finished reply and the expression names a character
actually has images for, pick the expression the reply "sounds like".
The scoring is SillyTavern-"local" style: count keyword hits per emotion
class and take the strongest - instant, offline, and deterministic.

The same whole-word scoring drives custom, user-editable keyword maps for
gallery/backdrop switching (see :func:`detect_keyword_match`): each image
carries a keyword list and the reply text picks the best-matching image.

LLM classification (see ``ai_prompts`` / ``test_tab``) is the more accurate
alternative; it falls back to this module whenever the request fails or
returns an unknown label, so this code is always the safety net.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from src.gallery_store import DEFAULT_EXPRESSIONS

# Keyword sets per expression, matched as case-insensitive whole words
# (word boundaries, so ``hug`` does not hit ``hugging``'s stem only -
# either spelling needs its own entry).  Multi-word phrases are fine.
# Expressions that describe a costume/pose rather than a tone
# (daily_clothes, hat_glasses, mid_delta, ...) simply have no keywords and
# can only be picked by LLM classification or shown as the fallback.
EXPRESSION_KEYWORDS: dict[str, tuple[str, ...]] = {
    'anger': (
        'angry', 'anger', 'furious', 'mad', 'irritated', 'glare', 'glared',
        'scowl', 'snapped', 'seething',
    ),
    'annoyed': (
        'annoyed', 'annoying', 'irritable', 'grumble', 'grumbled', 'hmph',
        'ugh', 'bothered', 'eye roll', 'rolled her eyes', 'rolled his eyes',
    ),
    'care': (
        'gentle', 'tender', 'tenderly', 'soothe', 'soothed', 'comforting',
        'comforted', 'nurturing', 'took care', 'warmly',
    ),
    'confused': (
        'confused', 'confusion', 'puzzled', 'bewildered', 'perplexed',
        'what do you mean', 'lost track', 'furrowed',
    ),
    'crying': (
        'crying', 'cried', 'tears', 'teary', 'weeping', 'wept', 'sobbing',
        'sobbed', 'bawling', 'tears streaming',
    ),
    'cute': (
        'cute', 'adorable', 'charming', 'blushing', 'blushed', 'bashful',
        'sweetly',
    ),
    'dejected': (
        'dejected', 'downcast', 'disheartened', 'discouraged', 'sullen',
        'drooping', 'spirit broken',
    ),
    'doubt': (
        'doubt', 'doubtful', 'distrustful', 'skeptical', 'suspicious',
        'hesitant', 'hesitated', 'unsure',
    ),
    'excited': (
        'excited', 'excitement', 'thrilled', 'eager', 'enthusiastic',
        'pumped', 'can\'t wait', 'buzzing',
    ),
    'happy': (
        'happy', 'happiness', 'smile', 'smiled', 'smiling', 'grin', 'grinned',
        'grinning', 'cheerful', 'glad', 'joy', 'joyful', 'delighted',
    ),
    'laughing': (
        'laughing', 'laughed', 'laugh', 'giggling', 'giggled', 'giggle',
        'chuckling', 'chuckled', 'chuckle', 'haha', 'hehe', 'lol', 'rofl',
    ),
    'like': (
        'like', 'liked', 'fond', 'fondness', 'enjoy', 'enjoyed', 'appreciate',
        'appreciated', 'favour', 'favor',
    ),
    'love': (
        'love', 'loved', 'loving', 'adore', 'adored', 'darling', 'sweetheart',
        'beloved', 'kiss', 'kissed', 'embraced', 'held close', 'my heart',
    ),
    'pride': (
        'proud', 'pride', 'triumphant', 'smug', 'smugly', 'boast', 'boasted',
        'accomplished', 'victorious',
    ),
    'rage': (
        'rage', 'enraged', 'wrath', 'livid', 'fury', 'furious', 'exploded',
        'screaming', 'screamed', 'yelled', 'roared',
    ),
    'sad': (
        'sad', 'sadness', 'unhappy', 'sorrow', 'sorrowful', 'mournful',
        'melancholy', 'heartbroken', 'depressed', 'gloom',
    ),
    'shame': (
        'shame', 'ashamed', 'embarrassed', 'embarrassment', 'mortified',
        'guilt', 'guilty', 'humiliated', 'wanted to disappear',
    ),
    'shock': (
        'shock', 'shocked', 'stunned', 'gasped', 'gasp', 'horrified',
        'disbelief', 'jaw dropped', 'wide-eyed',
    ),
    'sleep': (
        'sleep', 'sleeping', 'asleep', 'sleepy', 'drowsy', 'yawn', 'yawned',
        'snooze', 'napped', 'slumber', 'dozed',
    ),
    'surprise': (
        'surprise', 'surprised', 'startled', 'unexpected', 'astonished',
        'amazed', 'whoa', 'wow', 'taken aback',
    ),
    'turn_away': (
        'turned away', 'averted', 'looked away', 'look away', 'turned her back',
        'turned his back', 'facing away',
    ),
    'wink': ('wink', 'winked', 'winking'),
    'naughty': (
        'naughty', 'mischievous', 'teasing', 'teased', 'playful', 'cheeky',
        'seductive', 'flirty', 'flirted',
    ),
    'tsundere': (
        'tsundere', 'baka', 'don\'t get the wrong idea', 'stupid', 'idiot',
        'it\'s not like',
    ),
    'netorare': ('netorare', 'ntr', 'stolen away'),
    'misconduct': ('misconduct', 'prank', 'misbehaved', 'troublemaker'),
}

# Word boundaries around every pattern: ``mad`` must not hit ``made``, and
# multi-word phrases must match as whole units.
def compile_keywords(words: Iterable[str]) -> tuple[re.Pattern, ...]:
    """Compile *words* into whole-word, case-insensitive patterns.

    Multi-word phrases are fine (they match as whole units); an empty or
    whitespace-only entry compiles to nothing.  A raw comma-separated
    string is accepted and split first.
    """
    if isinstance(words, str):
        words = parse_keywords(words)
    patterns: list[re.Pattern] = []
    for word in words or ():
        text = str(word).strip()
        if not text:
            continue
        patterns.append(
            re.compile(r'(?<!\w)' + re.escape(text) + r'(?!\w)', re.IGNORECASE),
        )
    return tuple(patterns)


_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    name: compile_keywords(words)
    for name, words in EXPRESSION_KEYWORDS.items()
}


def parse_keywords(text: str) -> list[str]:
    """Split a user-edited keyword field into normalized keywords.

    Commas and newlines separate entries; surrounding whitespace is
    stripped, empties dropped, and duplicates removed (first spelling wins).
    """
    seen: set[str] = set()
    result: list[str] = []
    for part in re.split(r'[,;\n]+', str(text or '')):
        word = ' '.join(part.split())
        key = word.casefold()
        if not word or key in seen:
            continue
        seen.add(key)
        result.append(word)
    return result


def format_keywords(words: Iterable[str]) -> str:
    """Render keywords for the single-line edit field (comma-separated)."""
    return ', '.join(parse_keywords(', '.join(str(w) for w in words or ())))


def _priority(name: str) -> tuple[int, str]:
    """Sort key: DEFAULT_EXPRESSIONS order first, custom names after.

    Ties in keyword score are broken by this so detection is deterministic
    (a reply mentioning both "smile" and "cry" always resolves the same way).
    """
    try:
        return (DEFAULT_EXPRESSIONS.index(name), name)
    except ValueError:
        return (len(DEFAULT_EXPRESSIONS), name)


def score_expressions(text: str, available: Iterable[str]) -> dict[str, int]:
    """Count distinct keyword hits per expression name.

    Only names in *available* (the expressions the character has images for)
    are scored - there is no point switching to an image that does not
    exist.  Distinct keywords are counted rather than raw occurrences so a
    reply that says "haha" ten times does not out-score one that mixes three
    different laughing words.
    """
    names = {str(name).lower() for name in available or ()}
    scores: dict[str, int] = {}
    for name, patterns in _PATTERNS.items():
        if name not in names:
            continue
        hits = sum(1 for pattern in patterns if pattern.search(text or ''))
        if hits:
            scores[name] = hits
    return scores


def detect_expression(
    text: str, available: Iterable[str], default: str = 'normal',
) -> str:
    """Return the best-scoring expression name in *available*.

    With no keyword hits (or no scores at all) the result is *default* when
    that name is available, else the first available name in
    :data:`~src.gallery_store.DEFAULT_EXPRESSIONS` order (custom names last),
    else ``''`` when *available* is empty.  Callers treat ``''`` or a name
    without an image as "no sprite".
    """
    names = sorted({str(name).lower() for name in available or ()}, key=_priority)
    if not names:
        return ''
    scores = score_expressions(text, names)
    if scores:
        return max(sorted(scores), key=lambda n: (scores[n], -_priority(n)[0]))
    if default.lower() in names:
        return default.lower()
    return names[0]


def normalize_expression_label(text: str) -> str:
    """Normalize an LLM classification reply to an expression-style label.

    "Happy!" becomes ``happy`` so it can be compared against expression
    names; anything unparseable becomes ``''``.
    """
    cleaned = re.sub(r'[^a-z0-9_]+', '_', str(text or '').strip().lower())
    return cleaned.strip('_')


def match_expression_label(reply: str, available: Iterable[str]) -> str:
    """Map an LLM reply to an available expression name (``''`` when none).

    Exact normalized matches win; a reply that names a missing expression -
    models do that - resolves to ``''`` so the caller can fall back to
    keyword detection instead of guessing.
    """
    names = {str(name).lower() for name in available or ()}
    label = normalize_expression_label(reply)
    return label if label in names else ''


def _keyword_list(words) -> list[str]:
    """Normalize a keyword field that may be a raw string or an iterable."""
    if words is None:
        return []
    if isinstance(words, str):
        return parse_keywords(words)
    return parse_keywords(' '.join(str(w) for w in words))


def score_keyword_maps(text: str, mapping: Mapping[str, Iterable[str]]) -> dict[str, int]:
    """Count distinct keyword hits per key in a user-editable keyword map.

    *mapping* is ``{key: keywords}`` (e.g. image id -> its keyword list, as a
    list of words or a comma-separated string).  Only keys with at least one
    hit are returned; distinct keywords count rather than raw occurrences,
    mirroring :func:`score_expressions`.
    """
    scores: dict[str, int] = {}
    for key, words in (mapping or {}).items():
        patterns = compile_keywords(_keyword_list(words))
        hits = sum(1 for pattern in patterns if pattern.search(text or ''))
        if hits:
            scores[str(key)] = hits
    return scores


def detect_keyword_match(text: str, mapping: Mapping[str, Iterable[str]]) -> str:
    """Return the key in *mapping* whose keywords best match *text*.

    ``''`` when nothing matches.  Ties are broken by fewer keywords on the
    winner (a more specific list beats a sprawling one at equal hits) and
    then by the key name, so the result is deterministic.  The returned key
    is the stringified input key (callers normally use string keys already).
    """
    scores = score_keyword_maps(text, mapping)
    if not scores:
        return ''
    sizes = {
        str(key): len(_keyword_list(words)) for key, words in mapping.items()
    }
    return min(scores, key=lambda k: (-scores[k], sizes.get(k, 0), k))

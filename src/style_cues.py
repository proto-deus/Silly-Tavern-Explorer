"""Rotating per-reply style cues for the Test chat.

A style cue is a short directive ("open mid-action", "keep it clipped")
that is appended to the request's trailing instructions for one reply
only.  Rotating the cue every generation keeps replies from settling into
one fixed shape (same opening, same paragraph beats, same closing
question) while the long-term writing guidance stays in the main prompt.

The picker is a pure Python class so it can be unit-tested without Qt;
:class:`src.ui.test_tab.TestTab` owns one instance per chat tab.
"""
from __future__ import annotations

import random
from typing import Optional

#: One-reply writing directives.  ``{{char}}`` / ``{{user}}`` are macro
#: substituted by the caller, like every other prompt text.
STYLE_CUES: tuple[str, ...] = (
    'Open this reply with {{char}} speaking; no lead-in narration.',
    'Open mid-action: the scene is already moving when the reply starts.',
    'Open on a concrete sensory detail (a sound, a texture, a smell), not on '
    'an emotion.',
    'Open with a thought or observation before anyone speaks.',
    'Keep this reply short and clipped; let subtext do the work.',
    'Let this reply breathe: longer, flowing sentences with a natural rhythm.',
    'Let sentence length swing - a very short sentence right after a long one.',
    'End on an image or an action, not on dialogue and not on a question.',
    'Do not end with a question to {{user}} this time.',
    'Let the dialogue carry the scene; keep narration minimal.',
    "Show {{char}}'s mood through a small physical habit instead of naming "
    'the feeling.',
    'Vary the paragraph shape; a single short line is allowed if it lands.',
    'Give {{char}} a beat of silence - a reaction - before they speak.',
    'Include one imperfect, human detail: an interruption, a trailing-off, a '
    'mundane observation.',
    'Focus on one specific object or detail in the environment this reply.',
    'Skip the tidy metaphor; keep the imagery plain and grounded.',
)


class StyleCuePicker:
    """Turn-to-turn style cue rotation that never repeats in a row.

    :meth:`peek` returns the cue the *next* request will use without
    advancing the rotation, so a prompt preview can show exactly what will
    be sent; :meth:`next` consumes that cue, and the following generation
    gets a different one.
    """

    def __init__(
        self,
        rng: Optional[random.Random] = None,
        cues: Optional[tuple[str, ...]] = None,
    ) -> None:
        self._rng = rng if rng is not None else random.Random()
        self._cues = tuple(cues) if cues is not None else STYLE_CUES
        if not self._cues:
            raise ValueError('need at least one style cue')
        self._last: Optional[int] = None
        self._upcoming: Optional[int] = None

    def peek(self) -> str:
        """The cue the next :meth:`next` call will return (without advancing)."""
        if self._upcoming is None:
            self._upcoming = self._draw()
        return self._cues[self._upcoming]

    def next(self) -> str:
        """Consume the upcoming cue and advance the rotation."""
        cue = self.peek()
        self._last = self._upcoming
        self._upcoming = None
        return cue

    def _draw(self) -> int:
        """A random cue index that differs from the last used one."""
        n = len(self._cues)
        if n == 1:
            return 0
        if self._last is None:
            return self._rng.randrange(n)
        choice = self._rng.randrange(n - 1)
        # Sample n-1 slots and skip over the last-used index so every other
        # cue stays equally likely.
        return choice + 1 if choice >= self._last else choice

"""Safe JSON parsing for untrusted input.

``json.loads`` raises :class:`RecursionError` for deeply nested documents -
and ``RecursionError`` is a ``RuntimeError``, *not* a ``ValueError``, so the
``except (json.JSONDecodeError, ValueError)`` guard that most call sites use
does not catch it.  A crafted card / chat / lorebook file with 5 000 nested
arrays therefore escaped the guard and, in an unhandled Qt slot, aborted the
whole process (PyQt6 turns an unhandled exception in a slot into a fatal
error).

``MemoryError`` is caught for the same reason: a huge document must not take
the application down.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The nesting depth CPython's JSON decoder will attempt before giving up.
#: 1000 is far deeper than any real character card or lorebook needs.
MAX_JSON_DEPTH = 400


class JsonTooDeepError(ValueError):
    """The document nests deeper than :data:`MAX_JSON_DEPTH`."""


def _depth_guard(value: Any, _depth: int = 0) -> Any:
    """Raise :class:`JsonTooDeepError` for over-deeply nested documents."""
    if _depth > MAX_JSON_DEPTH:
        raise JsonTooDeepError(
            f'The JSON document nests deeper than {MAX_JSON_DEPTH} levels.'
        )
    if isinstance(value, dict):
        for item in value.values():
            _depth_guard(item, _depth + 1)
    elif isinstance(value, list):
        for item in value:
            _depth_guard(item, _depth + 1)
    return value


def loads_safe(text: str | bytes, max_depth: int = MAX_JSON_DEPTH) -> Any:
    """Parse *text* as JSON, raising ``ValueError`` on any failure.

    Every failure mode of ``json.loads`` is normalised to ``ValueError`` (or
    its subclasses ``json.JSONDecodeError`` and ``JsonTooDeepError``) so a
    single ``except ValueError`` is enough at the call site:
    malformed syntax, a wrong encoding, a document that is too deeply nested
    (``RecursionError``) and a document that is too large to hold in memory
    (``MemoryError``).
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = text.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError(f'Invalid UTF-8: {exc}') from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise
    except RecursionError as exc:
        raise JsonTooDeepError(
            'The JSON document is nested too deeply.'
        ) from exc
    except MemoryError as exc:
        raise ValueError('The JSON document is too large to parse.') from exc
    try:
        _depth_guard(value, 0)
    except RecursionError as exc:
        raise JsonTooDeepError('The JSON document is nested too deeply.') from exc
    return value


def loads_safe_or_none(text: str | bytes, default: Any = None) -> Optional[Any]:
    """Like :func:`loads_safe` but returns *default* instead of raising."""
    try:
        return loads_safe(text)
    except (ValueError, TypeError) as exc:
        logger.debug('Could not parse JSON: %s', exc)
        return default

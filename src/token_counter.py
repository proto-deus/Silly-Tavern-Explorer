from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.card_models import CharacterCard

logger = logging.getLogger(__name__)

try:
    import tiktoken
    _HAS_TIKTOKEN = True
except ImportError:
    _HAS_TIKTOKEN = False

BYTES_PER_TOKEN = 3.35

_encoding_cache: dict[str, object] = {}
# Encodings whose load failed. Without this, an offline machine re-attempted the
# BPE download on *every* call - and count_tokens runs on the GUI thread via the
# Edit tab's debounce timer, so each keystroke tick retried the network.
_encoding_failed: set[str] = set()
_fallback_logged = False


def _get_encoding(encoding_name: str = 'cl100k_base'):
    if encoding_name in _encoding_failed:
        raise RuntimeError(f"encoding {encoding_name!r} is unavailable")
    if encoding_name not in _encoding_cache:
        try:
            _encoding_cache[encoding_name] = tiktoken.get_encoding(encoding_name)
        except Exception:
            _encoding_failed.add(encoding_name)
            raise
    return _encoding_cache[encoding_name]


def _guesstimate(text: str) -> int:
    # errors='replace' rather than raising: a card may legitimately contain a
    # lone surrogate (from a \udXXX escape in third-party JSON), and the
    # fallback must not replace the real error with UnicodeEncodeError.
    byte_length = len(text.encode('utf-8', errors='replace'))
    return max(1, int(byte_length / BYTES_PER_TOKEN))


def _log_fallback_once() -> None:
    global _fallback_logged
    if not _fallback_logged:
        _fallback_logged = True
        logger.warning(
            "tiktoken unavailable or failed to load; token counts are now "
            "byte-length estimates and may differ between machines"
        )


def count_tokens(text: str, encoding_name: str = 'cl100k_base') -> int:
    if not text:
        return 0
    if _HAS_TIKTOKEN and encoding_name not in _encoding_failed:
        try:
            return _encode_len(text, encoding_name)
        except Exception:
            # Usually a failed vocab download (offline machine / frozen
            # build without a bundled cache). Log once so the degraded
            # mode is visible in the app log. Exceptions are deliberately
            # *not* cached (lru_cache keeps only successful results), so a
            # per-text encode failure is retried on the next call.
            _log_fallback_once()
            return _guesstimate(text)
    _log_fallback_once()
    return _guesstimate(text)


@lru_cache(maxsize=512)
def _encode_len(text: str, encoding_name: str) -> int:
    """Tokenizer count of *text*, memoized on (text, encoding).

    The same field texts, system prompts and message lines are re-counted on
    every load, save and render; the BPE encode is the expensive part, and
    identical strings are the common case.
    """
    enc = _get_encoding(encoding_name)
    return len(enc.encode(text))


def count_card_tokens(card: CharacterCard) -> int:
    permanent = '\n'.join(filter(None, [
        card.name,
        card.description,
        card.personality,
        card.scenario,
    ]))
    return count_tokens(permanent)


def count_card_tokens_full(card: CharacterCard) -> int:
    return count_tokens(card.full_text_for_tokens())

from __future__ import annotations

import logging
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
_fallback_logged = False


def _get_encoding(encoding_name: str = 'cl100k_base'):
    if encoding_name not in _encoding_cache:
        _encoding_cache[encoding_name] = tiktoken.get_encoding(encoding_name)
    return _encoding_cache[encoding_name]


def _guesstimate(text: str) -> int:
    byte_length = len(text.encode('utf-8'))
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
    if _HAS_TIKTOKEN:
        try:
            enc = _get_encoding(encoding_name)
            return len(enc.encode(text))
        except Exception:
            # Usually a failed vocab download (offline machine / frozen
            # build without a bundled cache). Log once so the degraded
            # mode is visible in the app log.
            _log_fallback_once()
            return _guesstimate(text)
    _log_fallback_once()
    return _guesstimate(text)


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

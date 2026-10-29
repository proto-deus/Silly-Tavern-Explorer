from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSettings

logger = logging.getLogger(__name__)


def _get_settings() -> QSettings:
    return QSettings('STExplorer', 'STExplorer')


_PROVIDER_KEYS = (
    'base_url', 'model', 'temperature', 'max_tokens', 'top_p', 'top_k',
    'min_p', 'context_size', 'frequency_penalty', 'presence_penalty', 'seed',
    'retry_attempts', 'stop', 'strict_chat_template', 'repeat_penalty',
    'mirostat', 'mirostat_tau', 'mirostat_eta',
    'dry_multiplier', 'dry_base', 'dry_allowed_length', 'dry_penalty_range',
    'xtc_threshold', 'xtc_probability', 'typical_p',
    'dynatemp_range', 'dynatemp_exponent', 'smoothing_factor',
    'min_tokens_to_keep',
)

_LEGACY_KEYS = _PROVIDER_KEYS + ('preset_name',)

_PROVIDER_DEFAULTS: dict = {
    'base_url': '',
    'model': '',
    'temperature': 1.0,
    'max_tokens': 2048,
    'top_p': 1.0,
    'top_k': 0,
    'min_p': 0.0,
    'context_size': 8192,
    'frequency_penalty': 0.0,
    'presence_penalty': 0.0,
    'seed': -1,
    'retry_attempts': 2,
    'stop': '',
    'strict_chat_template': False,
    'repeat_penalty': 1.0,
    'mirostat': 0,
    'mirostat_tau': 5.0,
    'mirostat_eta': 0.1,
    'dry_multiplier': 0.0,
    'dry_base': 1.75,
    'dry_allowed_length': 2,
    'dry_penalty_range': 0,
    'xtc_threshold': 0.1,
    'xtc_probability': 0.0,
    'typical_p': 1.0,
    'dynatemp_range': 0.0,
    'dynatemp_exponent': 1.0,
    'smoothing_factor': 0.0,
    'min_tokens_to_keep': 1,
}


def _provider_prefix(name: str) -> str:
    return f'api/providers/{name}/'


def _read_api_key(s: QSettings, prefix: str) -> str:
    """Return the stored API key, or '' if none can be recovered.

    A decryption failure must be distinguishable from "no key saved": the
    dialog needs to tell the user their key could not be read, otherwise
    saving settings writes an empty key over the ciphertext and the real key
    is lost irrecoverably.
    """
    encrypted = s.value(prefix + 'api_key_encrypted', '')
    if encrypted:
        decrypted = _decrypt_secret(encrypted)
        if decrypted is not None:
            return decrypted
        logger.error(
            "The saved API key for '%s' could not be decrypted. It has been "
            "left untouched - re-enter it to replace it.", prefix.strip('/'),
        )
        _record_undecryptable_key(prefix.strip('/'))
        return ''
    plaintext = s.value(prefix + 'api_key', '')
    if plaintext:
        logger.warning(
            "API key for '%s' is stored unencrypted; re-saving settings will encrypt it",
            prefix.strip('/'),
        )
    return plaintext


# Providers whose stored key could not be decrypted in this session. Saving
# while one of these is unresolved would destroy the ciphertext.
_UNDECRYPTABLE_KEYS: set[str] = set()


def _record_undecryptable_key(provider: str) -> None:
    _UNDECRYPTABLE_KEYS.add(provider)


def undecryptable_api_key_providers() -> list[str]:
    """Providers whose saved API key could not be decrypted (see :func:`_read_api_key`)."""
    return sorted(_UNDECRYPTABLE_KEYS)


def is_provider_undecryptable(provider: str) -> bool:
    """True when *provider*'s saved key could not be decrypted.

    Accepts either the bare provider name (``'LM Studio'``) or the internal
    storage key (``'api/providers/LM Studio'``) - the two forms have been
    used interchangeably and callers must not have to know which is stored.
    """
    wanted = {
        provider,
        f'api/providers/{provider}',
        _provider_prefix(provider).strip('/'),
    }
    return bool(wanted & _UNDECRYPTABLE_KEYS)


def clear_undecryptable_api_key_providers() -> None:
    """Forget the undecryptable-key warnings (after the user re-enters keys)."""
    _UNDECRYPTABLE_KEYS.clear()


def _is_preset_placeholder(prefix: str, api_key: str) -> bool:
    """True when *api_key* is a PRESETS default, not a user-supplied secret.

    Local-server presets (LM Studio, Ollama) carry a dummy key like
    ``'lm-studio'`` purely so ``Authorization: Bearer`` is sent. Those
    defaults must never be *stored*: the settings dialog round-trips them
    (``preset_from_saved`` fills the empty field), and writing one over an
    undecryptable ciphertext destroys the real key irrecoverably.
    """
    if not api_key:
        return False
    try:
        from src.ai_client import PRESETS
    except Exception:  # pragma: no cover - import always succeeds in-app
        return False
    name = prefix.strip('/').rsplit('/', 1)[-1]
    base = PRESETS.get(name)
    return base is not None and bool(base.api_key) and api_key == base.api_key


def _write_api_key(s: QSettings, prefix: str, api_key: str) -> None:
    if api_key:
        if _is_preset_placeholder(prefix, api_key):
            # A transport default, not a secret: keep whatever is stored.
            # Replacing a real (possibly undecryptable) key with the
            # placeholder would destroy it.
            logger.debug(
                "Not storing the preset placeholder key for '%s'", prefix.strip('/'),
            )
            return
        encrypted = _encrypt_secret(api_key)
        if encrypted is not None:
            s.setValue(prefix + 'api_key_encrypted', encrypted)
            s.remove(prefix + 'api_key')
            _UNDECRYPTABLE_KEYS.discard(prefix.strip('/'))
            return
        # Encryption unavailable (missing cryptography package on macOS/Linux
        # or a failed OS call). Fall back to plain text, but never destroy the
        # ciphertext that may still be recoverable: a transient DPAPI failure
        # would otherwise silently downgrade the key *and* discard the good
        # copy, leaving the user with a plaintext key and no way back.
        logger.warning(
            "Storing API key for '%s' in plain text (encryption unavailable); "
            "the previous encrypted copy is kept", prefix.strip('/'),
        )
        s.setValue(prefix + 'api_key', api_key)
        stale = s.value(prefix + 'api_key_encrypted', '')
        if stale:
            # Keep the old ciphertext, but not under the key _read_api_key
            # consults first: it would decrypt to the PREVIOUS key and shadow
            # the new plaintext value forever (requests then 401).
            s.setValue(prefix + 'api_key_encrypted.bak', stale)
            s.remove(prefix + 'api_key_encrypted')
    else:
        if prefix.strip('/') in _UNDECRYPTABLE_KEYS:
            # Refuse to wipe a key we merely failed to read.
            logger.error(
                "Refusing to clear the unreadable API key for '%s'; re-enter it "
                "to replace it.", prefix.strip('/'),
            )
            return
        s.remove(prefix + 'api_key')
        s.remove(prefix + 'api_key_encrypted')


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Corrupted settings value %r, falling back to %r", value, default)
        return default


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Corrupted settings value %r, falling back to %r", value, default)
        return default


def _safe_bool(value, default: bool) -> bool:
    """Coerce a QSettings value to bool.

    The native Windows backend stores booleans as the strings "true"/"false",
    and ``bool("false")`` is ``True`` - so a naive cast silently turns every
    saved-off flag on.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('true', '1', 'yes', 'on'):
            return True
        if text in ('false', '0', 'no', 'off', ''):
            return False
    logger.warning("Corrupted settings value %r, falling back to %r", value, default)
    return default


def _clamp(value, low, high, default):
    """Constrain *value* to [low, high], falling back to *default*."""
    return max(low, min(high, value))


def _read_config(s: QSettings, prefix: str, name: str = '') -> dict:
    # Values are clamped here, not just in the dialog: QSettings is a plain
    # registry/INI file that can be hand-edited or corrupted, and an
    # out-of-range retry count turns the client's retry loop unbounded.
    result: dict = {
        'preset_name': name or s.value(prefix + 'preset_name', ''),
        'base_url': s.value(prefix + 'base_url', ''),
        'api_key': _read_api_key(s, prefix),
        'model': s.value(prefix + 'model', ''),
        'temperature': _clamp(_safe_float(s.value(prefix + 'temperature', 1.0), 1.0), 0.0, 2.0, 1.0),
        'max_tokens': _clamp(_safe_int(s.value(prefix + 'max_tokens', 2048), 2048), 1, 1_000_000, 2048),
        'top_p': _clamp(_safe_float(s.value(prefix + 'top_p', 1.0), 1.0), 0.0, 1.0, 1.0),
        'top_k': _clamp(_safe_int(s.value(prefix + 'top_k', 0), 0), 0, 10_000, 0),
        'min_p': _clamp(_safe_float(s.value(prefix + 'min_p', 0.0), 0.0), 0.0, 1.0, 0.0),
        'context_size': _clamp(_safe_int(s.value(prefix + 'context_size', 8192), 8192), 256, 10_000_000, 8192),
        'frequency_penalty': _clamp(_safe_float(s.value(prefix + 'frequency_penalty', 0.0), 0.0), -2.0, 2.0, 0.0),
        'presence_penalty': _clamp(_safe_float(s.value(prefix + 'presence_penalty', 0.0), 0.0), -2.0, 2.0, 0.0),
        'seed': _safe_int(s.value(prefix + 'seed', -1), -1),
        # Matches the Settings spinbox range so a corrupted value can't produce
        # an effectively infinite retry loop.
        'retry_attempts': _clamp(_safe_int(s.value(prefix + 'retry_attempts', 2), 2), 0, 5, 2),
        'stop': _parse_stop_value(s.value(prefix + 'stop', '')),
        'strict_chat_template': _safe_bool(
            s.value(prefix + 'strict_chat_template', False), False,
        ),
        'repeat_penalty': _clamp(_safe_float(s.value(prefix + 'repeat_penalty', 1.0), 1.0), 1.0, 3.0, 1.0),
        'mirostat': _clamp(_safe_int(s.value(prefix + 'mirostat', 0), 0), 0, 2, 0),
        'mirostat_tau': _clamp(_safe_float(s.value(prefix + 'mirostat_tau', 5.0), 5.0), 0.0, 10.0, 5.0),
        'mirostat_eta': _clamp(_safe_float(s.value(prefix + 'mirostat_eta', 0.1), 0.1), 0.0, 1.0, 0.1),
        'dry_multiplier': _clamp(_safe_float(s.value(prefix + 'dry_multiplier', 0.0), 0.0), 0.0, 10.0, 0.0),
        'dry_base': _clamp(_safe_float(s.value(prefix + 'dry_base', 1.75), 1.75), 1.0, 10.0, 1.75),
        'dry_allowed_length': _clamp(_safe_int(s.value(prefix + 'dry_allowed_length', 2), 2), 0, 100, 2),
        'dry_penalty_range': _clamp(_safe_int(s.value(prefix + 'dry_penalty_range', 0), 0), 0, 10_000, 0),
        'xtc_threshold': _clamp(_safe_float(s.value(prefix + 'xtc_threshold', 0.1), 0.1), 0.0, 1.0, 0.1),
        'xtc_probability': _clamp(_safe_float(s.value(prefix + 'xtc_probability', 0.0), 0.0), 0.0, 1.0, 0.0),
        'typical_p': _clamp(_safe_float(s.value(prefix + 'typical_p', 1.0), 1.0), 0.0, 1.0, 1.0),
        'dynatemp_range': _clamp(_safe_float(s.value(prefix + 'dynatemp_range', 0.0), 0.0), 0.0, 5.0, 0.0),
        'dynatemp_exponent': _clamp(_safe_float(s.value(prefix + 'dynatemp_exponent', 1.0), 1.0), 0.0, 10.0, 1.0),
        'smoothing_factor': _clamp(_safe_float(s.value(prefix + 'smoothing_factor', 0.0), 0.0), 0.0, 10.0, 0.0),
        'min_tokens_to_keep': _clamp(_safe_int(s.value(prefix + 'min_tokens_to_keep', 1), 1), 1, 100, 1),
    }
    return result


def _parse_stop_value(value) -> list[str]:
    """Normalise a stored ``stop`` value into a list of stop strings.

    Accepts the JSON-array form written by :func:`save_provider_settings`, a
    QSettings string list, or a plain comma/newline separated string.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith('['):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            raw_items = parsed if isinstance(parsed, list) else [text]
        else:
            raw_items = re.split(r'[,;\n]', text)
    out: list[str] = []
    for item in raw_items:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


def save_provider_settings(name: str, config: dict) -> None:
    s = _get_settings()
    prefix = _provider_prefix(name)
    for key in _PROVIDER_KEYS:
        s.setValue(prefix + key, config.get(key, _PROVIDER_DEFAULTS[key]))
    stop = config.get('stop', _PROVIDER_DEFAULTS['stop'])
    if isinstance(stop, (list, tuple)):
        stop = json.dumps([str(x).strip() for x in stop if str(x).strip()])
    s.setValue(prefix + 'stop', stop if stop else '')
    _write_api_key(s, prefix, config.get('api_key', ''))


def load_provider_settings(name: str) -> dict:
    s = _get_settings()
    return _read_config(s, _provider_prefix(name), name=name)


def save_all_provider_settings(configs: dict[str, dict]) -> None:
    for name, config in configs.items():
        save_provider_settings(name, config)


def set_active_provider(name: str) -> None:
    _get_settings().setValue('api/active_provider', name)


def load_active_provider() -> str:
    name = _get_settings().value('api/active_provider', '')
    return name or 'LM Studio'


def _migrate_legacy_settings(s: QSettings) -> None:
    legacy_name = s.value('api/preset_name', '') or ''
    if not legacy_name and not any(s.value('api/' + k) for k in _PROVIDER_KEYS):
        return
    legacy = _read_config(s, 'api/', name=legacy_name or 'LM Studio')
    save_provider_settings(legacy_name or 'LM Studio', legacy)
    s.setValue('api/active_provider', legacy_name or 'LM Studio')
    for key in _LEGACY_KEYS:
        s.remove('api/' + key)
    if 'api' in _UNDECRYPTABLE_KEYS:
        # The ciphertext could not be decrypted, so the migrated copy holds no
        # key.  A readable plaintext copy may therefore be the ONLY recoverable
        # key - deleting it would lose the credential for good.  Keep it.
        if s.value('api/api_key', ''):
            logger.warning(
                "The legacy API key could not be decrypted; the plaintext copy "
                "was kept so it can be recovered manually."
            )
        else:
            s.remove('api/api_key')
    else:
        s.remove('api/api_key')
    if 'api' not in _UNDECRYPTABLE_KEYS:
        # Only drop the legacy ciphertext once it has been read and re-stored
        # under the provider prefix. If decryption failed the copy may still
        # be recoverable later (e.g. after restoring an old machine profile) -
        # deleting it here would be exactly the loss _write_api_key refuses.
        s.remove('api/api_key_encrypted')


def save_api_settings(
    preset_name: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int,
    top_p: float = 1.0,
    top_k: int = 0,
    min_p: float = 0.0,
    context_size: int = 8192,
    frequency_penalty: float = 0.0,
    presence_penalty: float = 0.0,
    seed: int = -1,
    retry_attempts: int = 2,
) -> None:
    save_provider_settings(preset_name, {
        'base_url': base_url,
        'api_key': api_key,
        'model': model,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'top_p': top_p,
        'top_k': top_k,
        'min_p': min_p,
        'context_size': context_size,
        'frequency_penalty': frequency_penalty,
        'presence_penalty': presence_penalty,
        'seed': seed,
        # Was omitted, so every caller silently reset the user's retry count
        # to the default.
        'retry_attempts': retry_attempts,
    })
    set_active_provider(preset_name)


def save_active_sampling(
    temperature: float,
    min_p: float,
    *,
    model: str | None = None,
    context_size: int | None = None,
) -> None:
    """Persist Test-tab chat overrides into the active provider's settings.

    The Test tab's sampling/model/context widgets write through here so they
    stay in sync with the settings dialog (both read/write the same provider
    store).  ``model``/``context_size`` of ``None`` leave those keys untouched.
    """
    config = load_api_settings()
    config['temperature'] = float(temperature)
    config['min_p'] = float(min_p)
    if model is not None:
        config['model'] = str(model)
    if context_size is not None:
        config['context_size'] = int(context_size)
    save_provider_settings(
        config.get('preset_name') or load_active_provider(), config,
    )


def save_provider_models(name: str, models: list[str], source_url: str = '') -> None:
    """Cache the model list fetched from provider *name*'s endpoint.

    The list is stored separately from :data:`_PROVIDER_KEYS` so it survives
    settings saves and is available without re-fetching; ``source_url`` records
    which endpoint produced it so a changed base URL invalidates the cache.
    """
    s = _get_settings()
    prefix = _provider_prefix(name)
    s.setValue(prefix + 'models', json.dumps([str(m) for m in (models or []) if m]))
    s.setValue(prefix + 'models_source_url', (source_url or '').rstrip('/'))


def load_provider_models(name: str, base_url: str | None = None) -> list[str]:
    """Return the cached model list for provider *name*.

    When *base_url* is given and it differs from the endpoint the cache was
    fetched from, the stale list is discarded (returns ``[]``).
    """
    s = _get_settings()
    prefix = _provider_prefix(name)
    if base_url is not None:
        source = s.value(prefix + 'models_source_url', '')
        if not isinstance(source, str):
            source = ''
        if source.rstrip('/') != (base_url or '').rstrip('/'):
            return []
    raw = s.value(prefix + 'models', '')
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupted model cache for '%s'; resetting", name)
    return []


def load_api_settings() -> dict:
    s = _get_settings()
    active = s.value('api/active_provider', '')
    if not active:
        _migrate_legacy_settings(s)
        active = s.value('api/active_provider', '') or 'LM Studio'
    return load_provider_settings(active)


def save_macro_settings(user_name: str, custom_macros: dict[str, str]) -> None:
    """Persist the user-name macro value and any custom ``{{macro}}`` overrides."""
    s = _get_settings()
    s.setValue('macros/user_name', user_name or '')
    s.setValue('macros/custom', json.dumps(custom_macros or {}))


def load_macro_settings() -> dict:
    """Return ``{'user_name': str, 'macros': dict[str, str]}``."""
    s = _get_settings()
    user_name = s.value('macros/user_name', 'User')
    if not isinstance(user_name, str):
        user_name = 'User'
    custom: dict[str, str] = {}
    raw = s.value('macros/custom', '')
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                custom = {str(k): str(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupted macro settings; resetting to defaults")
            custom = {}
    return {'user_name': user_name, 'macros': custom}


DEFAULT_TEST_SETTINGS: dict = {
    'dialogue_color': '#9ad8ff',
    'action_color': '#e0a060',
    'emphasis_color': '#9be8a0',
    'show_timestamps': False,
    'auto_scroll': True,
    'include_first_message': True,
    # Where the card's few-shot example dialogue rides in the request:
    # 'system' (SillyTavern-style labelled block in the system prompt),
    # 'post_history' (block after the history), or 'history' (real turns).
    'example_placement': 'system',
    # Rotate a small per-reply style directive (opening, rhythm, focus) into
    # the trailing instructions so consecutive replies do not follow the
    # same structure.
    'style_variation': True,
    # Test-tab presentation: 'chat' is the classic scrolling conversation,
    # 'visual_novel' puts a gallery backdrop behind a smaller chat window.
    'view_mode': 'chat',
    # Which gallery image opens with a chat: first (by sort order), the
    # latest added, or a random pick.  A pinned image (per chat or per
    # character) takes precedence over this.
    'vn_image_source': 'first',
    # Which images fill the VN stage: 'both' (shared backdrops first, then
    # the character's gallery), 'global' (shared backdrops only) or
    # 'gallery' (the character's own gallery images only).
    'vn_backdrop_pool': 'both',
    # Backdrop scaling: 'contain' letterboxes, 'cover' crops to fill,
    # 'stretch' ignores the aspect ratio.
    'vn_image_fit': 'contain',
    # Height share (percent) of the VN chat panel below the backdrop image.
    'vn_chat_height_pct': 30,
    # Opacity (percent) of the scrim behind the chat panel.
    'vn_overlay_opacity': 60,
    # Cycle to the next gallery image whenever an assistant reply lands.
    'vn_advance_on_reply': False,
    # Show the image caption and the "2/7" counter over the backdrop.
    'vn_show_caption': True,
    # Use the card portrait as the backdrop when the gallery is empty.
    'vn_use_portrait_fallback': True,
    # Expression sprites: auto-pick a SillyTavern expression image from the
    # emotional tone of each assistant reply.
    'vn_expression_auto': True,
    # Classify the tone with a small LLM request instead of local keywords.
    # Costs one extra request per reply; falls back to keywords on failure.
    'vn_expression_llm': False,
    # Switch the backdrop when a reply matches an image's keyword list
    # (Gallery tab -> Edit Keywords...).  Off by default: the keywords only
    # fire once the user has actually written some.
    'vn_backdrop_keywords': False,
    # Sprite alignment over the backdrop: 'left' | 'center' | 'right'.
    'vn_sprite_position': 'center',
    # Sprite height share (percent) of the backdrop area height.
    'vn_sprite_height_pct': 75,
}

# Keys a character may override with its own value (see ``card_settings``).
# They share the global key names (plus ``backdrop_file``, which is purely
# per-character) so :func:`merge_vn_overrides` is a plain dict update.
# Everything else in the test settings stays global.
VN_OVERRIDE_KEYS: tuple[str, ...] = (
    'backdrop_file',
    'vn_image_source',
    'vn_image_fit',
    'vn_advance_on_reply',
    'vn_expression_auto',
    'vn_backdrop_keywords',
)


def merge_vn_overrides(global_settings: dict, overrides: dict | None) -> dict:
    """Return *global_settings* with per-character VN overrides applied.

    *overrides* is the decoded ``card_settings`` blob for one character; only
    keys listed in :data:`VN_OVERRIDE_KEYS` are honoured and only when they
    carry a usable value, so ``None``/absent means "keep the global setting".
    ``backdrop_file`` has no global counterpart; it rides along in the result
    so callers can resolve the backdrop from one dict.
    """
    result = dict(global_settings or {})
    for key in VN_OVERRIDE_KEYS:
        if not overrides or key not in overrides:
            continue
        value = overrides[key]
        if value is None:
            continue
        result[key] = value
    return result


def load_test_settings() -> dict:
    """Return the Test-tab display/history settings merged over defaults."""
    s = _get_settings()
    result = dict(DEFAULT_TEST_SETTINGS)
    raw = s.value('test/settings', '')
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                result.update({str(k): v for k, v in parsed.items()})
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupted test settings; resetting to defaults")
    return result


def save_test_settings(settings: dict) -> None:
    """Persist the Test-tab display/history settings dict."""
    s = _get_settings()
    s.setValue('test/settings', json.dumps(settings or {}))


# ---------------------------------------------------------------------------
# User personas (named; the active one backs the {{user}} persona description)
# ---------------------------------------------------------------------------

_PERSONAS_KEY = 'chat/personas'
_ACTIVE_PERSONA_KEY = 'chat/active_persona_id'
_LEGACY_PERSONA_KEY = 'chat/user_persona'
_DEFAULT_PERSONA_ID = 'default'


def _read_personas() -> list[dict]:
    raw = _get_settings().value(_PERSONAS_KEY, '')
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupted persona list; resetting to default")
        return []
    personas: list[dict] = []
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            personas.append({
                'id': str(item.get('id') or ''),
                'name': str(item.get('name') or 'Persona'),
                'text': str(item.get('text') or ''),
            })
    return [p for p in personas if p['id']]


def save_personas(personas: list[dict]) -> None:
    """Persist the named persona list (id/name/text triples)."""
    clean: list[dict] = []
    for item in personas or []:
        if not isinstance(item, dict):
            continue
        clean.append({
            'id': str(item.get('id') or ''),
            'name': str(item.get('name') or 'Persona'),
            'text': str(item.get('text') or ''),
        })
    _get_settings().setValue(_PERSONAS_KEY, json.dumps(clean, ensure_ascii=False))


def load_personas() -> list[dict]:
    """Return the named user personas (always at least one).

    On first use the legacy single ``chat/user_persona`` string migrates into
    a "Default" persona so existing setups keep their text.
    """
    personas = _read_personas()
    if personas:
        return personas
    legacy = _get_settings().value(_LEGACY_PERSONA_KEY, '')
    legacy = legacy if isinstance(legacy, str) else ''
    personas = [{'id': _DEFAULT_PERSONA_ID, 'name': 'Default', 'text': legacy}]
    save_personas(personas)
    return personas


def load_active_persona_id() -> str:
    """Return the id of the active persona (falls back to the first one)."""
    pid = _get_settings().value(_ACTIVE_PERSONA_KEY, '')
    if isinstance(pid, str) and pid:
        return pid
    personas = load_personas()
    return personas[0]['id'] if personas else _DEFAULT_PERSONA_ID


def save_active_persona_id(persona_id: str) -> None:
    """Persist which persona is active (used as the chat's {{user}} persona)."""
    _get_settings().setValue(_ACTIVE_PERSONA_KEY, str(persona_id or ''))


def get_persona(persona_id: str | None = None) -> dict:
    """Return the persona dict for *persona_id* (default: the active one)."""
    pid = persona_id or load_active_persona_id()
    for p in load_personas():
        if p['id'] == pid:
            return p
    personas = load_personas()
    return personas[0] if personas else {'id': _DEFAULT_PERSONA_ID, 'name': 'Default', 'text': ''}


def save_user_persona(persona: str) -> None:
    """Persist the {{user}} persona description shown to the model.

    Writes the text of the active persona (the Settings dialog edits that
    one) and keeps the legacy key in sync for older builds.
    """
    text = persona or ''
    _get_settings().setValue(_LEGACY_PERSONA_KEY, text)
    personas = load_personas()
    pid = load_active_persona_id()
    for p in personas:
        if p['id'] == pid:
            p['text'] = text
            break
    else:
        personas.append({'id': pid or _DEFAULT_PERSONA_ID, 'name': 'Default', 'text': text})
    save_personas(personas)


def load_user_persona() -> str:
    """Return the active persona's text (the model's {{user}} persona)."""
    return get_persona().get('text', '')


# ---------------------------------------------------------------------------
# System prompt templates (named main prompts for the Test chat)
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATES_KEY = 'prompts/system_templates'
_ACTIVE_SYSTEM_TEMPLATE_KEY = 'prompts/active_system_template'
_DEFAULT_SYSTEM_TEMPLATE_ID = 'custom'


def save_system_templates(templates: list[dict]) -> None:
    """Persist the user-defined system prompt templates.

    Each item is ``{'id', 'name', 'content', 'post_history'}``; the list
    replaces whatever was saved before.
    """
    clean: list[dict] = []
    for item in templates or []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get('id') or '')
        if not tid:
            continue
        clean.append({
            'id': tid,
            'name': str(item.get('name') or 'Template'),
            'content': str(item.get('content') or ''),
            'post_history': str(item.get('post_history') or ''),
        })
    _get_settings().setValue(_SYSTEM_TEMPLATES_KEY, json.dumps(clean, ensure_ascii=False))


def load_system_templates() -> list[dict]:
    """Return the user-defined system prompt templates (may be empty)."""
    raw = _get_settings().value(_SYSTEM_TEMPLATES_KEY, '')
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupted system prompt template list; resetting")
        return []
    out: list[dict] = []
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            tid = str(item.get('id') or '')
            if not tid:
                continue
            out.append({
                'id': tid,
                'name': str(item.get('name') or 'Template'),
                'content': str(item.get('content') or ''),
                'post_history': str(item.get('post_history') or ''),
            })
    return out


def save_active_system_template(template_id: str) -> None:
    """Persist which system prompt template new chats use by default."""
    _get_settings().setValue(
        _ACTIVE_SYSTEM_TEMPLATE_KEY, str(template_id or _DEFAULT_SYSTEM_TEMPLATE_ID),
    )


def load_active_system_template() -> str:
    """Return the active system prompt template id (default: 'custom')."""
    tid = _get_settings().value(_ACTIVE_SYSTEM_TEMPLATE_KEY, '')
    if isinstance(tid, str) and tid.strip():
        return tid.strip()
    return _DEFAULT_SYSTEM_TEMPLATE_ID


# Context templates (chat-context framing presets)
_CONTEXT_TEMPLATES_KEY = 'prompts/context_templates'
_ACTIVE_CONTEXT_TEMPLATE_KEY = 'prompts/active_context_template'
_DEFAULT_CONTEXT_TEMPLATE_ID = 'default'

# Keys a stored context template may carry; anything else is dropped so a
# schema change can't resurrect stale fields.
_CONTEXT_TEMPLATE_FIELDS = (
    'id', 'name', 'block_format', 'main_format', 'block_separator',
    'names_behavior', 'name_format', 'example_separator',
    'example_user_prefix', 'example_char_prefix',
    'post_history_format', 'jailbreak_format', 'conversation_start_header',
)


def save_context_templates(templates: list[dict]) -> None:
    """Persist the user-defined context templates (list replaces previous)."""
    clean: list[dict] = []
    for item in templates or []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get('id') or '')
        if not tid:
            continue
        row = {'id': tid, 'name': str(item.get('name') or 'Context Template')}
        for key in _CONTEXT_TEMPLATE_FIELDS:
            if key in ('id', 'name'):
                continue
            if key in item and item[key] is not None:
                row[key] = item[key]
        clean.append(row)
    _get_settings().setValue(
        _CONTEXT_TEMPLATES_KEY, json.dumps(clean, ensure_ascii=False),
    )


def load_context_templates() -> list[dict]:
    """Return the user-defined context templates (may be empty)."""
    raw = _get_settings().value(_CONTEXT_TEMPLATES_KEY, '')
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupted context template list; resetting")
        return []
    out: list[dict] = []
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            tid = str(item.get('id') or '')
            if not tid:
                continue
            row = {'id': tid, 'name': str(item.get('name') or 'Context Template')}
            for key in _CONTEXT_TEMPLATE_FIELDS:
                if key in ('id', 'name'):
                    continue
                if key in item and item[key] is not None:
                    row[key] = item[key]
            out.append(row)
    return out


def save_active_context_template(template_id: str) -> None:
    """Persist which context template new chats use by default."""
    _get_settings().setValue(
        _ACTIVE_CONTEXT_TEMPLATE_KEY,
        str(template_id or _DEFAULT_CONTEXT_TEMPLATE_ID),
    )


def load_active_context_template() -> str:
    """Return the active context template id (default: 'default')."""
    tid = _get_settings().value(_ACTIVE_CONTEXT_TEMPLATE_KEY, '')
    if isinstance(tid, str) and tid.strip():
        return tid.strip()
    return _DEFAULT_CONTEXT_TEMPLATE_ID


def save_active_lorebooks(filenames: list[str]) -> None:
    """Persist which standalone lorebooks are injected into Test chats."""
    s = _get_settings()
    s.setValue('test/active_lorebooks', json.dumps([str(f) for f in (filenames or [])]))


def save_lorebook_use_card_context(use: bool) -> None:
    """Persist whether lorebook AI generation includes the selected-card context."""
    # ``type=bool`` required: the native Windows backend serializes booleans
    # as "true"/"false" strings and bool("false") == True.
    _get_settings().setValue('lorebooks/use_card_context', bool(use))


def load_lorebook_use_card_context() -> bool:
    """Return whether lorebook AI generation includes the card context."""
    return _get_settings().value(
        'lorebooks/use_card_context', True, type=bool,
    )


def load_active_lorebooks() -> list[str]:
    """Return the filenames of lorebooks active for Test-tab injection."""
    raw = _get_settings().value('test/active_lorebooks', '')
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupted active-lorebook settings; resetting")
    return []


def save_st_characters_path(path: str) -> None:
    s = _get_settings()
    s.setValue('st/characters_path', path)


def load_st_characters_path() -> str:
    s = _get_settings()
    return s.value('st/characters_path', '')


def save_st_worlds_path(path: str) -> None:
    """Persist the SillyTavern world-info (lorebooks) directory."""
    s = _get_settings()
    s.setValue('st/worlds_path', path)


def load_st_worlds_path() -> str:
    s = _get_settings()
    return s.value('st/worlds_path', '')


def save_st_user_images_path(path: str) -> None:
    """Persist the SillyTavern user-images (expression images) directory."""
    s = _get_settings()
    s.setValue('st/user_images_path', path)


def load_st_user_images_path() -> str:
    s = _get_settings()
    return s.value('st/user_images_path', '')


def save_st_auto_detect_done(done: bool) -> None:
    s = _get_settings()
    s.setValue('st/auto_detect_done', done)


def load_st_auto_detect_done() -> bool:
    s = _get_settings()
    # ``type=bool`` is required: the native Windows backend serializes
    # booleans as "true"/"false" strings and ``bool("false") == True``.
    return s.value('st/auto_detect_done', False, type=bool)


def clear_st_settings() -> None:
    """Remove all SillyTavern integration settings.

    Offered as "Forget SillyTavern setup" in the ST config dialog: the cards
    keep their ``st_avatar_url`` links, so only the local paths are cleared and
    the user can re-point at a moved install without re-linking everything.
    """
    s = _get_settings()
    s.remove('st/characters_path')
    s.remove('st/worlds_path')
    s.remove('st/user_images_path')
    s.remove('st/auto_detect_done')


def save_window_geometry(geometry: bytes) -> None:
    s = _get_settings()
    s.setValue('ui/window_geometry', bytes(geometry))


def load_window_geometry() -> Optional[bytes]:
    s = _get_settings()
    geo = s.value('ui/window_geometry')
    if geo is None:
        return None
    if isinstance(geo, (bytes, bytearray)):
        return bytes(geo)
    # PyQt6 may store the value as a QByteArray on some platforms.
    try:
        from PyQt6.QtCore import QByteArray
        if isinstance(geo, QByteArray):
            return bytes(geo)
    except ImportError:
        pass
    if isinstance(geo, str):
        try:
            return bytes.fromhex(geo)
        except ValueError:
            logger.warning("Corrupted window geometry, ignoring saved value")
            return None
    logger.warning("Unexpected window geometry type %r, ignoring", type(geo).__name__)
    return None


def save_font_size(size: int) -> None:
    s = _get_settings()
    s.setValue('ui/font_size', size)
    # Keep the portable config in sync, so the two never disagree about
    # what the app is supposed to look like.
    try:
        from src.portable_config import set_value
        set_value('ui.font_size', int(size))
    except Exception:
        logger.debug("Could not update the portable config", exc_info=True)


def load_font_size() -> int:
    # The portable config wins: it is the explicit, hand-edited override.
    try:
        from src.portable_config import get
        override = get('ui.font_size', None)
        if override is not None:
            return min(32, max(8, _safe_int(override, 13)))
    except Exception:
        logger.debug("Could not read the portable config", exc_info=True)
    s = _get_settings()
    val = _safe_int(s.value('ui/font_size', 13), 13)
    # Clamp to the same range the Font Size dialog allows so a corrupted
    # registry value can never produce a <= 0 font size downstream.
    return min(32, max(8, val))


def save_theme(name: str) -> None:
    """Persist the UI theme name ('dark' or 'light')."""
    _get_settings().setValue('ui/theme', str(name or 'dark'))
    try:
        from src.portable_config import set_value
        set_value('ui.theme', str(name or 'dark'))
    except Exception:
        logger.debug("Could not update the portable config", exc_info=True)


def load_theme() -> str:
    """The configured UI theme name.

    The portable config wins over the registry, because it is the explicit
    override.  An unknown value is coerced to 'dark' so a corrupted setting
    can never leave the app unstyled.
    """
    value = ''
    try:
        from src.portable_config import get
        value = str(get('ui.theme', '') or '').strip().lower()
    except Exception:
        value = ''
    if not value:
        value = str(_get_settings().value('ui/theme', '') or '').strip().lower()
    return value if value in ('dark', 'light') else 'dark'


def save_spellcheck_enabled(enabled: bool) -> None:
    """Persist whether text fields underline misspelled words."""
    _get_settings().setValue('ui/spellcheck_enabled', bool(enabled))


def load_spellcheck_enabled() -> bool:
    """Return whether spell checking is on for editable text fields."""
    # ``type=bool`` is required: the native Windows backend serializes
    # booleans as "true"/"false" strings and ``bool("false") == True``.
    return _get_settings().value('ui/spellcheck_enabled', True, type=bool)


def save_logging_enabled(enabled: bool) -> None:
    """Persist whether the app writes the app.log file."""
    _get_settings().setValue('logging/enabled', bool(enabled))


def load_logging_enabled() -> bool:
    """Return whether the app log (app.log) is written (on by default)."""
    # ``type=bool`` for the same "true"/"false" string reason as above.
    return _get_settings().value('logging/enabled', True, type=bool)


def app_log_path() -> Path:
    """The location of the app log file."""
    from src.app_paths import data_dir

    return data_dir() / 'app.log'


def apply_logging_enabled(enabled: bool) -> None:
    """Add or remove the app.log FileHandler on the root logger.

    Called with the stored value at startup and again whenever the setting
    changes, so the checkbox takes effect without a restart.
    """
    log_path = app_log_path()
    root = logging.getLogger()
    fmt = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    for handler in list(root.handlers):
        filename = getattr(handler, 'baseFilename', None)
        if filename and Path(filename) == log_path:
            root.removeHandler(handler)
            handler.close()
    if enabled:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(log_path, encoding='utf-8')
            handler.setFormatter(logging.Formatter(fmt))
            root.addHandler(handler)
        except OSError:
            logger.warning("Could not open the app log for writing", exc_info=True)


def save_library_selected_ids(ids: list[int]) -> None:
    s = _get_settings()
    s.setValue('ui/library_selected_ids', ids)


def load_library_selected_ids() -> list[int]:
    s = _get_settings()
    raw = s.value('ui/library_selected_ids', [])
    if isinstance(raw, list):
        return [_safe_int(x, 0) for x in raw if _safe_int(x, 0) > 0]
    return []


def save_library_scroll_position(position: int) -> None:
    s = _get_settings()
    s.setValue('ui/library_scroll_position', position)


def load_library_scroll_position() -> int:
    s = _get_settings()
    return _safe_int(s.value('ui/library_scroll_position', 0), 0)


def save_edit_selected_id(char_id: int | None) -> None:
    s = _get_settings()
    if char_id is not None:
        s.setValue('ui/edit_selected_id', char_id)
    else:
        s.remove('ui/edit_selected_id')


def load_edit_selected_id() -> int | None:
    s = _get_settings()
    raw = s.value('ui/edit_selected_id')
    if raw is None:
        return None
    val = _safe_int(raw, 0)
    return val if val > 0 else None


# Headless/library context: never allow DPAPI to pop UI prompts.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

# Tagged payload prefixes so _decrypt_secret can dispatch on format and
# legacy (prefix-less DPAPI hex) values keep decrypting on Windows.
_DPAPI_PREFIX = 'dpapi1:'
_FERNET_PREFIX = 'fernet1:'


def encrypt_api_key(plaintext: str) -> Optional[str]:
    """Encrypt *plaintext* with the platform-native scheme.

    Returns a tagged ciphertext string (``dpapi1:…`` on Windows,
    ``fernet1:…`` elsewhere) or ``None`` when no secure storage is
    available (caller decides whether to fall back to plain text).
    """
    if sys.platform == 'win32':
        encrypted = _encrypt_dpapi(plaintext)
        return _DPAPI_PREFIX + encrypted if encrypted is not None else None
    return _encrypt_fernet(plaintext)


def decrypt_api_key(payload: str) -> Optional[str]:
    """Decrypt a payload produced by :func:`encrypt_api_key`.

    Also understands legacy prefix-less DPAPI hex blobs written by older
    versions so existing Windows settings keep working.  Returns ``None``
    when the payload cannot be decrypted.
    """
    if not payload:
        return None
    if payload.startswith(_DPAPI_PREFIX):
        return _decrypt_dpapi(payload[len(_DPAPI_PREFIX):])
    if payload.startswith(_FERNET_PREFIX):
        return _decrypt_fernet(payload[len(_FERNET_PREFIX):])
    # Legacy format: raw DPAPI hex without a prefix.
    return _decrypt_dpapi(payload)


# Internal names kept for backwards compatibility with older tests/tools.
_encrypt_secret = encrypt_api_key
_decrypt_secret = decrypt_api_key


def _fernet_key_path() -> Path:
    from src.app_paths import data_dir
    return data_dir() / 'secret.key'


def _is_fernet_key(key: bytes) -> bool:
    """True when *key* is a well-formed 32-byte urlsafe-base64 Fernet key."""
    if len(key) != 44:
        return False
    try:
        return len(base64.urlsafe_b64decode(key)) == 32
    except (ValueError, TypeError):
        return False


def _load_or_create_fernet():
    """Return a Fernet instance backed by a local key file, or ``None``.

    The key is generated on first use and stored with ``0600`` permissions
    (POSIX).  On Windows the mode argument of ``os.open`` has no effect but
    the path is equally user-private under ``%USERPROFILE%``.
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning(
            "The 'cryptography' package is required for API-key encryption "
            "on this platform; install it to avoid plain-text keys"
        )
        return None

    path = _fernet_key_path()
    key = b''
    try:
        key = path.read_bytes().strip()
    except OSError:
        pass

    if key and not _is_fernet_key(key):
        # A truncated/garbled key file (crash or disk-full mid-write) can never
        # decrypt anything. Regenerating would orphan every stored ciphertext,
        # so refuse instead: the user keeps their recoverable key material.
        logger.error(
            "Encryption key %s is corrupt (%d bytes); refusing to regenerate it "
            "because that would permanently lose every stored API key.", path, len(key),
        )
        return None

    if not key:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            # Write to a temp file first (a crash mid-write can never leave a
            # truncated key at the real path), then publish it with a hard
            # link, which is create-exclusive and atomic. ``os.replace`` in
            # place of the link would silently overwrite a key another
            # process created first, orphaning every ciphertext it wrote.
            fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix='.secret.key.')
            try:
                os.write(fd, key)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            try:
                os.link(tmp_name, str(path))
            except FileExistsError:
                # Raced with another process that created the key first:
                # adopt ITS key, never overwrite it.
                try:
                    key = path.read_bytes().strip()
                except OSError:
                    logger.warning("Failed reading freshly created %s", path)
                    return None
            except (OSError, NotImplementedError, AttributeError):
                # Filesystem without hard-link support: fall back to replace.
                # The single-instance lock makes a race here unlikely.
                os.replace(tmp_name, str(path))
                tmp_name = ''
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        except OSError as e:
            logger.warning("Cannot write encryption key %s: %s", path, e)
            return None

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    try:
        return Fernet(key)
    except Exception as e:
        logger.warning("Corrupted encryption key in %s: %s", path, e)
        return None


def _encrypt_fernet(plaintext: str) -> Optional[str]:
    fernet = _load_or_create_fernet()
    if fernet is None:
        return None
    try:
        token = fernet.encrypt(plaintext.encode('utf-8')).decode('ascii')
    except Exception as e:
        logger.warning("Fernet encryption failed: %s", e)
        return None
    return _FERNET_PREFIX + token


def _decrypt_fernet(token_str: str) -> Optional[str]:
    fernet = _load_or_create_fernet()
    if fernet is None:
        return None
    try:
        return fernet.decrypt(token_str.encode('ascii')).decode('utf-8')
    except Exception as e:
        logger.warning("Fernet decryption failed; saved API key cannot be recovered: %s", e)
        return None


def _encrypt_dpapi(plaintext: str) -> Optional[str]:
    if sys.platform != 'win32':
        return None
    try:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', ctypes.wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

        CryptProtectData = ctypes.windll.crypt32.CryptProtectData
        LocalFree = ctypes.windll.kernel32.LocalFree
        # Declare prototypes so pointer-sized returns (LocalFree) aren't
        # truncated to c_int by ctypes' default conversion.
        CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        CryptProtectData.restype = ctypes.wintypes.BOOL
        LocalFree.argtypes = [ctypes.c_void_p]
        LocalFree.restype = ctypes.c_void_p

        data_in = plaintext.encode('utf-8')
        blob_in = DATA_BLOB(len(data_in), ctypes.create_string_buffer(data_in, len(data_in)))
        blob_out = DATA_BLOB()

        if CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
        ):
            encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            LocalFree(blob_out.pbData)
            return encrypted.hex()
        logger.warning("CryptProtectData returned failure; API key will be stored in plain text")
    except Exception as e:
        logger.warning("DPAPI encryption failed: %s", e)
    return None


def _decrypt_dpapi(hex_str: str) -> Optional[str]:
    if sys.platform != 'win32':
        return None
    try:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [('cbData', ctypes.wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

        CryptUnprotectData = ctypes.windll.crypt32.CryptUnprotectData
        LocalFree = ctypes.windll.kernel32.LocalFree
        CryptUnprotectData.argtypes = [
            # The second parameter is an *out* pointer (LPWSTR *ppszDataDescr);
            # declaring it as a plain c_wchar_p would make ctypes marshal a
            # string where a pointer-to-pointer is expected.
            ctypes.POINTER(DATA_BLOB), ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        CryptUnprotectData.restype = ctypes.wintypes.BOOL
        LocalFree.argtypes = [ctypes.c_void_p]
        LocalFree.restype = ctypes.c_void_p

        encrypted = bytes.fromhex(hex_str)
        blob_in = DATA_BLOB(len(encrypted), ctypes.create_string_buffer(encrypted, len(encrypted)))
        blob_out = DATA_BLOB()

        if CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
        ):
            decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            LocalFree(blob_out.pbData)
            return decrypted.decode('utf-8')
        logger.warning("CryptUnprotectData returned failure; saved API key cannot be decrypted")
    except Exception as e:
        logger.warning("DPAPI decryption failed: %s", e)
    return None


def reset_all_settings() -> None:
    """Restore every Settings-dialog-managed setting to its built-in default.

    The stored overrides are removed so the ``load_*`` helpers fall back to
    their defaults: the provider connections (base URLs, models, API keys,
    model-list caches) and all LLM sampling parameters, the user name,
    custom macros and user personas, every AI prompt template override,
    the user-created system and context templates together with the active
    selections, the Test-tab settings, and the App toggles (spell check,
    logging, font size - including the portable-config font override).

    Deliberately untouched: library data (cards, chats, gallery), the
    encryption vault, the UI theme, window layout, the SillyTavern paths,
    the lorebook selections, and per-character card settings.
    """
    s = _get_settings()
    s.remove('api')
    s.remove('macros')
    s.remove('ai/prompts')
    s.remove(_PERSONAS_KEY)
    s.remove(_ACTIVE_PERSONA_KEY)
    s.remove(_LEGACY_PERSONA_KEY)
    s.remove(_SYSTEM_TEMPLATES_KEY)
    s.remove(_ACTIVE_SYSTEM_TEMPLATE_KEY)
    s.remove(_CONTEXT_TEMPLATES_KEY)
    s.remove(_ACTIVE_CONTEXT_TEMPLATE_KEY)
    s.remove('test/settings')
    s.remove('ui/spellcheck_enabled')
    s.remove('logging/enabled')
    s.remove('ui/font_size')
    try:
        from src.portable_config import remove_value
        remove_value('ui.font_size')
    except Exception:
        logger.debug("Could not clear the portable font override", exc_info=True)
    # The wiped providers no longer have unreadable keys; stale warnings
    # would otherwise block re-saving a fresh key for them.
    clear_undecryptable_api_key_providers()
    logger.info('All settings were reset to their built-in defaults')

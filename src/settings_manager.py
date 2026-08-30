from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSettings

logger = logging.getLogger(__name__)


def _get_settings() -> QSettings:
    return QSettings('STExplorer', 'STExplorer')


_PROVIDER_KEYS = (
    'base_url', 'model', 'temperature', 'max_tokens', 'top_p', 'top_k',
    'min_p', 'context_size', 'frequency_penalty', 'presence_penalty', 'seed',
    'retry_attempts',
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
}


def _provider_prefix(name: str) -> str:
    return f'api/providers/{name}/'


def _read_api_key(s: QSettings, prefix: str) -> str:
    encrypted = s.value(prefix + 'api_key_encrypted', '')
    if encrypted:
        decrypted = _decrypt_secret(encrypted)
        if decrypted is not None:
            return decrypted
    plaintext = s.value(prefix + 'api_key', '')
    if plaintext:
        logger.warning(
            "API key for '%s' is stored unencrypted; re-saving settings will encrypt it",
            prefix.strip('/'),
        )
    return plaintext


def _write_api_key(s: QSettings, prefix: str, api_key: str) -> None:
    if api_key:
        encrypted = _encrypt_secret(api_key)
        if encrypted is not None:
            s.setValue(prefix + 'api_key_encrypted', encrypted)
            s.remove(prefix + 'api_key')
        else:
            # Encryption unavailable (missing cryptography package on
            # macOS/Linux or a failed OS call): the documented fallback
            # stores the key in plain text. Warn loudly so users on shared
            # machines know.
            logger.warning(
                "Storing API key for '%s' in plain text (encryption unavailable)",
                prefix.strip('/'),
            )
            s.setValue(prefix + 'api_key', api_key)
            s.remove(prefix + 'api_key_encrypted')
    else:
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


def _read_config(s: QSettings, prefix: str, name: str = '') -> dict:
    result: dict = {
        'preset_name': name or s.value(prefix + 'preset_name', ''),
        'base_url': s.value(prefix + 'base_url', ''),
        'api_key': _read_api_key(s, prefix),
        'model': s.value(prefix + 'model', ''),
        'temperature': _safe_float(s.value(prefix + 'temperature', 1.0), 1.0),
        'max_tokens': _safe_int(s.value(prefix + 'max_tokens', 2048), 2048),
        'top_p': _safe_float(s.value(prefix + 'top_p', 1.0), 1.0),
        'top_k': _safe_int(s.value(prefix + 'top_k', 0), 0),
        'min_p': _safe_float(s.value(prefix + 'min_p', 0.0), 0.0),
        'context_size': _safe_int(s.value(prefix + 'context_size', 8192), 8192),
        'frequency_penalty': _safe_float(s.value(prefix + 'frequency_penalty', 0.0), 0.0),
        'presence_penalty': _safe_float(s.value(prefix + 'presence_penalty', 0.0), 0.0),
        'seed': _safe_int(s.value(prefix + 'seed', -1), -1),
        'retry_attempts': _safe_int(s.value(prefix + 'retry_attempts', 2), 2),
    }
    return result


def save_provider_settings(name: str, config: dict) -> None:
    s = _get_settings()
    prefix = _provider_prefix(name)
    for key in _PROVIDER_KEYS:
        s.setValue(prefix + key, config.get(key, _PROVIDER_DEFAULTS[key]))
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
    s.remove('api/api_key')
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
    })
    set_active_provider(preset_name)


def save_active_sampling(temperature: float, min_p: float) -> None:
    """Persist Temp/Min-P into the active provider's saved settings.

    The Test tab's sampling spinboxes write through here so they stay in
    sync with the LLM tab of the settings dialog (both read/write the
    same provider store).
    """
    config = load_api_settings()
    config['temperature'] = float(temperature)
    config['min_p'] = float(min_p)
    save_provider_settings(
        config.get('preset_name') or load_active_provider(), config,
    )


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
}


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


def save_user_persona(persona: str) -> None:
    """Persist the {{user}} persona description shown to the model."""
    _get_settings().setValue('chat/user_persona', persona or '')


def load_user_persona() -> str:
    persona = _get_settings().value('chat/user_persona', '')
    return persona if isinstance(persona, str) else ''


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


def save_st_auto_detect_done(done: bool) -> None:
    s = _get_settings()
    s.setValue('st/auto_detect_done', done)


def load_st_auto_detect_done() -> bool:
    s = _get_settings()
    # ``type=bool`` is required: the native Windows backend serializes
    # booleans as "true"/"false" strings and ``bool("false") == True``.
    return s.value('st/auto_detect_done', False, type=bool)


def clear_st_settings() -> None:
    """Remove all SillyTavern integration settings."""
    s = _get_settings()
    s.remove('st/characters_path')
    s.remove('st/worlds_path')
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


def load_font_size() -> int:
    s = _get_settings()
    val = _safe_int(s.value('ui/font_size', 13), 13)
    # Clamp to the same range the Font Size dialog allows so a corrupted
    # registry value can never produce a <= 0 font size downstream.
    return min(32, max(8, val))


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

    if not key:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
        except FileExistsError:
            # Raced with another process that created the key first.
            try:
                key = path.read_bytes().strip()
            except OSError:
                logger.warning("Failed reading freshly created %s", path)
                return None
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
            ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.POINTER(DATA_BLOB),
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

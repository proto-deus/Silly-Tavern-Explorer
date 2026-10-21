"""Redacted crash-report bundle.

Collects what a bug report needs - the app and Python versions, the
platform, the tail of the app log, the current settings and a few
library counts - and packages it as a JSON file or a zip archive.

The bundle is redacted twice over: every string goes through
:func:`src.ai_client.redact_secrets`, and any setting whose *name*
mentions a key, password, secret or token is replaced wholesale.  A
crash report is meant to be posted on a public issue tracker, so it must
never carry an API key, a vault password or a decryption key.

Pure logic (no Qt at import time) so a report can be collected from a
crash handler on a worker thread.
"""
from __future__ import annotations

import io
import json
import logging
import platform
import sqlite3
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from src.ai_client import redact_secrets

logger = logging.getLogger(__name__)

#: Name of the log file inside the app data dir.
LOG_FILENAME = 'app.log'

#: How many log lines a report carries.
LOG_TAIL_LINES = 200

#: Name of the report inside the zip archive.
REPORT_NAME = 'crash-report.json'

#: Setting names that hold a credential.  Their value is replaced
#: wholesale, whatever the value looks like.
SECRET_NAME_MARKERS = (
    'key', 'password', 'secret', 'token', 'credential', 'auth', 'bearer',
    'passphrase', 'session', 'cookie', 'otp', 'pin',
)


def _data_dir() -> Path:
    """The app data dir (imported lazily so this module stays Qt-free)."""
    from src.app_paths import data_dir

    return data_dir()


def _app_version() -> str:
    """The installed app version ('unknown' when it cannot be determined)."""
    try:
        from importlib.metadata import version

        return version('st-explorer')
    except Exception:
        logger.debug('The app version could not be determined', exc_info=True)
        return 'unknown'


def _known_secrets() -> list[str]:
    """The literal secret values the app knows about.

    Pattern matching alone can never be complete (a key can have any
    format), so the literal values are removed as well.  The API keys are
    *decrypted* for this: the stored value is the ciphertext, and what can
    end up in the log is the plaintext key.
    """
    secrets: list[str] = []
    try:
        from src.settings_manager import _get_settings, _read_api_key, _provider_prefix

        settings = _get_settings()
        for key in _settings_keys(settings):
            if not is_secret_name(key):
                continue
            try:
                value = settings.value(key)
            except Exception:
                logger.debug('The setting %s could not be read', key, exc_info=True)
                continue
            if isinstance(value, str) and value.strip():
                secrets.append(value.strip())
        # The decrypted keys: a server error message quotes the *plaintext*
        # key back, and that is what ends up in the log.
        for name in _provider_names(settings):
            try:
                key = _read_api_key(settings, _provider_prefix(name))
            except Exception:
                logger.debug('The API key of %s could not be read', name,
                             exc_info=True)
                continue
            if key and key.strip():
                secrets.append(key.strip())
    except Exception:
        logger.debug('The API keys could not be read for the crash report',
                     exc_info=True)
    return secrets


def _provider_names(settings: Any) -> list[str]:
    """The names of the configured API providers."""
    names: list[str] = []
    try:
        for key in _settings_keys(settings):
            if key.startswith('api/providers/') and key.endswith('/api_key'):
                names.append(key.split('/')[2])
    except Exception:
        logger.debug('The provider names could not be read', exc_info=True)
    return names


def _log_tail(limit: int = LOG_TAIL_LINES) -> list[str]:
    """The last *limit* lines of the app log, redacted.

    The log can carry an echoed API key (a server error message quotes
    the key back), so the lines are redacted like every other string in
    the report - and the *literal* keys are removed too, because a key
    echoed in an unusual format would survive the pattern rules.
    """
    path = _data_dir() / LOG_FILENAME
    lines: list[str] = []
    try:
        if not path.is_file():
            return []
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            lines = [line.rstrip('\n') for line in deque(handle, maxlen=max(1, limit))]
    except OSError as exc:
        logger.warning('Could not read the app log: %s', exc)
        return []
    secrets = _known_secrets()
    return [redact_secrets(line, secrets) for line in lines]


def is_secret_name(key: str) -> bool:
    """True when a setting called *key* holds a credential."""
    lowered = str(key or '').casefold()
    return any(marker in lowered for marker in SECRET_NAME_MARKERS)


def _redact_value(value: Any, secrets: list[str]) -> Any:
    """Redact a setting value, recursing into dicts and lists.

    A nested value (``{"api_key": "..."}``) was serialised to JSON and then
    pattern-matched: the label rule expects ``key:`` / ``key=`` and did not
    match the JSON spelling ``"key":``, so the value survived.  The
    redaction is therefore done by *name*, before serialisation.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for name, item in value.items():
            if is_secret_name(str(name)):
                out[str(name)] = '[redacted]'
            else:
                out[str(name)] = _redact_value(item, secrets)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_secrets(value, secrets)
    return value


def _redact_setting(key: str, value: Any, secrets: list[str] | None = None) -> str:
    """The string form of *value*, redacted when *key* is a credential."""
    if is_secret_name(key):
        return '[redacted]'
    secrets = secrets or []
    # A QSettings value is always a string, so a JSON document stored in it
    # has to be parsed before the nested credentials can be found by name.
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith('{') or stripped.startswith('['):
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                redacted = _redact_value(parsed, secrets)
                return redact_secrets(
                    json.dumps(redacted, ensure_ascii=False, default=str), secrets,
                )
    redacted = _redact_value(value, secrets)
    if isinstance(redacted, (dict, list, tuple)):
        text = json.dumps(redacted, ensure_ascii=False, default=str)
    else:
        text = str(redacted)
    return redact_secrets(text, secrets)


def _settings_keys(settings: Any) -> list[str]:
    """The keys of *settings* (empty when it cannot list them)."""
    lister = getattr(settings, 'allKeys', None)
    if not callable(lister):
        return []
    try:
        return [str(key) for key in lister()]
    except Exception:
        logger.warning('Could not list the settings keys', exc_info=True)
        return []


def _collect_settings() -> dict[str, str]:
    """The current settings, with every credential redacted.

    The settings live in QSettings, so this import pulls in Qt.  It is
    done lazily (and defensively) so the rest of the module stays usable
    without a GUI toolkit.
    """
    secrets = _known_secrets()
    try:
        from src.settings_manager import _get_settings
    except Exception:
        logger.warning('The settings are unavailable (no Qt?)')
        return {}
    try:
        settings = _get_settings()
        out: dict[str, str] = {}
        for key in _settings_keys(settings):
            try:
                value = settings.value(key)
            except Exception:
                value = ''
            out[key] = _redact_setting(key, value, secrets)
        return out
    except Exception:
        logger.exception('Could not read the settings for the crash report')
        return {}


def _count_cards() -> int:
    """The number of cards in the library (0 when the database is unusable)."""
    db_path = _data_dir() / 'library.db'
    if not db_path.is_file():
        return 0
    conn = None
    try:
        # Read-only: a crash report must not create or modify anything.
        uri = db_path.resolve().as_uri() + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        row = conn.execute('SELECT COUNT(*) FROM characters').fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, OSError, ValueError):
        logger.debug('Could not count the cards', exc_info=True)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _count_files(directory: Path, pattern: str = '*.json') -> int:
    """The number of files matching *pattern* under *directory*."""
    if not directory.is_dir():
        return 0
    try:
        return sum(1 for path in directory.rglob(pattern) if path.is_file())
    except OSError:
        return 0


def _library_counts() -> dict[str, int]:
    """The card, session and lorebook counts of the library."""
    root = _data_dir()
    return {
        'cards': _count_cards(),
        'sessions': _count_files(root / 'sessions'),
        'lorebooks': _count_files(root / 'lorebooks'),
    }


def collect_crash_report(app_version: str = '') -> dict:
    """Collect the redacted crash report.

    Args:
        app_version: The version to report; the installed one is used
            when it is empty.

    Returns:
        The report as a dict (app and Python version, platform, the log
        tail, the redacted settings and the library counts).  It never
        contains an API key, a vault password or a decryption key.
    """
    return {
        'app': 'ST Explorer',
        'app_version': str(app_version or _app_version()),
        'python_version': platform.python_version(),
        'platform': platform.platform(),
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'log_tail': _log_tail(),
        'settings': _collect_settings(),
        'library': _library_counts(),
    }


def write_crash_report(path: str | Path, app_version: str = '') -> Path:
    """Write the crash report as a JSON document to *path*.

    Args:
        path: Where to write the report.
        app_version: The version to report.

    Returns:
        The path that was written.
    """
    report = collect_crash_report(app_version)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: a crash while writing left a truncated (and useless) report.
    from src.fs_utils import atomic_write_bytes

    atomic_write_bytes(
        target,
        json.dumps(report, indent=2, ensure_ascii=False, default=str).encode('utf-8'),
    )
    return target


def crash_report_zip(app_version: str = '') -> bytes:
    """The crash report as a zip archive.

    Args:
        app_version: The version to report.

    Returns:
        The bytes of a zip archive holding ``crash-report.json`` and the
        redacted log tail as ``app.log``.
    """
    report = collect_crash_report(app_version)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            REPORT_NAME,
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
        )
        archive.writestr(LOG_FILENAME, '\n'.join(report.get('log_tail', [])))
    return buffer.getvalue()

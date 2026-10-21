"""Portable TOML configuration file.

A hand-editable ``config.toml`` in the app data dir that overrides a
small set of settings.  It exists so a configuration can be carried
between machines (or dropped next to a portable install) without
touching the registry-based QSettings store.

The file is read with :mod:`tomllib` (Python 3.11+) and written by
hand - only flat key/value tables are supported.  Every read is
defensive: a missing, unreadable or corrupt file simply means "no
overrides", so a typo in the file can never stop the app from starting.

Supported keys::

    [ui]
    font_size = 14
    theme = "dark"

    [library]
    # Cards built at once by the Library grid; 0 shows all of them.
    page_size = 100

    [backup]
    auto_backup = true
    keep = 5

Pure logic (no Qt) so it is unit-testable and safe to use off the GUI
thread.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import tomllib
except ImportError:     # pragma: no cover - Python < 3.11
    tomllib = None      # type: ignore[assignment]

#: Name of the config file inside the app data dir.
CONFIG_FILENAME = 'config.toml'

#: The settings a config file may override, with their default values.
#: The keys are dotted paths into the nested TOML tables.
OVERRIDABLE_SETTINGS: dict[str, Any] = {
    'ui.font_size': 14,
    'ui.theme': 'dark',
    'library.page_size': 100,
    'backup.auto_backup': True,
    'backup.keep': 5,
}


def config_path() -> Path:
    """Path of the portable config file inside the app data dir."""
    from src.app_paths import data_dir

    return data_dir() / CONFIG_FILENAME


def _lookup(data: dict, key: str, default: Any = None) -> Any:
    """Return the value of the dotted *key* inside *data*."""
    node: Any = data
    for part in str(key or '').split('.'):
        part = part.strip()
        if not part or not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def load_config() -> dict:
    """The parsed config file.

    Returns:
        The nested dict of the file, or ``{}`` when the file is absent,
        unreadable or invalid (a warning is logged in that case).
    """
    path = config_path()
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    if tomllib is None:     # pragma: no cover - Python < 3.11
        logger.warning('tomllib is unavailable; the portable config is ignored')
        return {}
    try:
        data = tomllib.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning('Ignoring the invalid portable config %s: %s', path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning('Ignoring the invalid portable config %s', path)
        return {}
    return data


def _format_key(key: Any) -> str:
    """Render *key* as a TOML key (quoted when it is not a bare one)."""
    text = str(key)
    # A bare TOML key is limited to ASCII letters, digits, '_' and '-'.
    if text and all(c.isascii() and (c.isalnum() or c in '_-') for c in text):
        return text
    return json.dumps(text, ensure_ascii=False)


def _format_value(value: Any) -> str:
    """Render *value* as a TOML value."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_format_value(item) for item in value) + ']'
    if value is None:
        return '""'
    return json.dumps(str(value), ensure_ascii=False)


def _dump_toml(data: dict) -> str:
    """Render the nested dict *data* as a TOML document."""
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    for key, value in scalars.items():
        lines.append(f'{_format_key(key)} = {_format_value(value)}')
    for name, table in tables.items():
        if lines:
            lines.append('')
        lines.append(f'[{_format_key(name)}]')
        for key, value in table.items():
            if isinstance(value, dict):
                # Nested tables are not supported by the flat format.
                continue
            lines.append(f'{_format_key(key)} = {_format_value(value)}')
    return '\n'.join(lines).rstrip() + '\n'


def save_config(data: dict) -> bool:
    """Write *data* to the config file.

    Args:
        data: The nested dict to store (the shape :func:`load_config` reads).

    Returns:
        True when the file was written, False when it could not be.
    """
    try:
        path = config_path()
    except OSError:       # pragma: no cover - data_dir() does not raise
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump_toml(data or {}), encoding='utf-8')
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning('Could not write the portable config %s: %s', path, exc)
        return False


def get(key: str, default: Any = None) -> Any:
    """The value of the dotted *key* in the config file.

    Args:
        key: A dotted key such as ``'ui.font_size'``.
        default: Returned when the key is not set.

    Returns:
        The stored value, or *default*.
    """
    return _lookup(load_config(), key, default)


def set_value(key: str, value: Any) -> bool:
    """Set the dotted *key* to *value* and save the config file.

    Args:
        key: A dotted key such as ``'ui.font_size'``.
        value: The value to store.

    Returns:
        True when the file was written, False when it could not be.
    """
    parts = [part.strip() for part in str(key or '').split('.') if part.strip()]
    if not parts:
        logger.warning('Refusing to set an empty config key')
        return False
    data = load_config()
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value
    return save_config(data)


def config_overrides() -> dict[str, Any]:
    """The effective values of the known overridable settings.

    A setting the config file sets wins; every other known setting keeps
    its default.  The mapping is flat and dotted
    (``{'ui.font_size': 14, ...}``), so a caller can look a value up
    without walking the nested tables.
    """
    data = load_config()
    out: dict[str, Any] = {}
    for key, default in OVERRIDABLE_SETTINGS.items():
        value = _lookup(data, key, None)
        out[key] = default if value is None else value
    return out

"""Small plugin / event-hook system.

Lets other parts of the app (or an add-on) react to what the app does:
a card was imported, a card was saved, a backup was created, a sync
finished, a generation finished.

A callback is registered for an event name and is called with a
:class:`HookEvent` when the event is emitted.  Callbacks run in
descending priority order, so a high-priority callback sees the event
first.  :func:`emit_safe` swallows (and logs) a failing callback -
the event bus must never be the reason a chat breaks - while
:func:`emit` lets the exception through for callers that want to know.

Pure logic (no Qt) so it is unit-testable and safe to use from worker
threads.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: A card was imported into the library.
EVENT_CARD_IMPORTED = 'card_imported'

#: A card was saved.
EVENT_CARD_SAVED = 'card_saved'

#: A card was deleted.
EVENT_CARD_DELETED = 'card_deleted'

#: A backup archive was created.
EVENT_BACKUP_CREATED = 'backup_created'

#: A SillyTavern sync run finished.
EVENT_SYNC_COMPLETED = 'sync_completed'

#: A generation (a chat turn) finished.
EVENT_GENERATION_FINISHED = 'generation_finished'

#: The events the app itself emits.
BUILTIN_EVENTS = (
    EVENT_CARD_IMPORTED,
    EVENT_CARD_SAVED,
    EVENT_CARD_DELETED,
    EVENT_BACKUP_CREATED,
    EVENT_SYNC_COMPLETED,
    EVENT_GENERATION_FINISHED,
)


@dataclass
class HookEvent:
    """An event handed to the callbacks of one hook."""

    name: str
    payload: dict = field(default_factory=dict)


#: The signature of a hook callback: it takes the event and returns
#: whatever it wants (the return value is collected by :func:`emit`).
HookCallback = Callable[[HookEvent], Any]

#: Registered callbacks per event name: (priority, sequence, callback).
_registry: dict[str, list[tuple[int, int, HookCallback]]] = {}
_lock = threading.RLock()
_sequence = 0


def register(name: str, callback: HookCallback, *, priority: int = 0) -> None:
    """Register *callback* for the event *name*.

    Args:
        name: The event name (see :data:`BUILTIN_EVENTS`).
        callback: Called with the :class:`HookEvent` when the event is
            emitted.
        priority: Callbacks with a higher priority run first.

    Returns:
        Nothing.  Registering the same callback twice registers it twice.
    """
    global _sequence
    if not name or not callable(callback):
        logger.warning('Ignoring a hook registration without a name or callback')
        return
    try:
        weight = int(priority)
    except (TypeError, ValueError):
        weight = 0
    with _lock:
        _sequence += 1
        _registry.setdefault(str(name), []).append((weight, _sequence, callback))


def unregister(name: str, callback: HookCallback) -> bool:
    """Remove *callback* from the event *name*.

    Args:
        name: The event name.
        callback: The callback to remove.

    Returns:
        True when the callback was registered (and is now removed).
    """
    with _lock:
        entries = _registry.get(str(name))
        if not entries:
            return False
        for index, (_weight, _seq, registered) in enumerate(entries):
            if registered is callback:
                del entries[index]
                if not entries:
                    _registry.pop(str(name), None)
                return True
    return False


def clear() -> None:
    """Remove every registered callback."""
    with _lock:
        _registry.clear()


def _ordered(name: str) -> list[HookCallback]:
    """The callbacks of *name*, highest priority first."""
    with _lock:
        entries = list(_registry.get(str(name), ()))
    entries.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in entries]


def registered(name: str) -> list[HookCallback]:
    """The callbacks registered for *name*, in the order they are called."""
    return _ordered(name)


def emit(event_name: str, **payload: Any) -> list:
    """Emit the event *event_name* and return the results of every callback.

    Args:
        event_name: The event name.
        **payload: The payload of the event.

    Returns:
        One result per callback, in call order.

    Raises:
        Exception: Whatever a callback raises is propagated to the
            caller (use :func:`emit_safe` to skip a failing callback).

    Note:
        The event name is a separate parameter so the payload can carry a
        ``name`` key of its own (the app announces cards by name).
    """
    event = HookEvent(name=str(event_name), payload=dict(payload))
    results: list[Any] = []
    for callback in _ordered(event_name):
        results.append(callback(event))
    return results


def emit_safe(event_name: str, **payload: Any) -> list:
    """Emit the event *event_name*, skipping (and logging) a failing callback.

    Args:
        event_name: The event name.
        **payload: The payload of the event.

    Returns:
        The results of the callbacks that ran.
    """
    event = HookEvent(name=str(event_name), payload=dict(payload))
    results: list[Any] = []
    for callback in _ordered(event_name):
        try:
            results.append(callback(event))
        except Exception:
            logger.exception('A hook callback for %s failed', event_name)
        except BaseException:
            # A plugin that raises SystemExit (or KeyboardInterrupt) would
            # otherwise take the whole application down.
            logger.exception('A hook callback for %s failed fatally', event_name)
    return results

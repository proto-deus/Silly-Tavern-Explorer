"""The events the application itself emits.

Thin wrappers around :mod:`src.hooks` for the built-in events: a card was
imported, saved or deleted, a backup was written, a sync run finished or
a generation was produced.

The hook module is imported lazily - it is an optional cross-cutting
module and must not be pulled in at start-up - and every error is
swallowed: a failing user plugin must never break the operation the
event belongs to.  The call sites wrap the call in a ``try``/``except``
of their own, so the guarantee holds even when this module is replaced.

The payload of an event is documented on the function that emits it.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.sillytavern_sync import SyncSummary

logger = logging.getLogger(__name__)


def card_imported(card_id: int, name: str) -> None:
    """Announce a card that was imported into the library.

    Args:
        card_id: The database id of the imported card.
        name: The name of the imported card.
    """
    try:
        from src import hooks
        hooks.emit_safe(hooks.EVENT_CARD_IMPORTED, card_id=card_id, name=name)
    except Exception:
        logger.exception("Could not emit the card_imported event")


def card_saved(card_id: int, name: str) -> None:
    """Announce a card that was saved.

    Args:
        card_id: The database id of the saved card.
        name: The name of the saved card.
    """
    try:
        from src import hooks
        hooks.emit_safe(hooks.EVENT_CARD_SAVED, card_id=card_id, name=name)
    except Exception:
        logger.exception("Could not emit the card_saved event")


def card_deleted(card_id: int, name: str) -> None:
    """Announce a card that was deleted from the library.

    Args:
        card_id: The database id of the deleted card.
        name: The name of the deleted card.
    """
    try:
        from src import hooks
        hooks.emit_safe(hooks.EVENT_CARD_DELETED, card_id=card_id, name=name)
    except Exception:
        logger.exception("Could not emit the card_deleted event")


def backup_created(path: str) -> None:
    """Announce a library backup archive that was written.

    Args:
        path: The path of the written archive.
    """
    try:
        from src import hooks
        hooks.emit_safe(hooks.EVENT_BACKUP_CREATED, path=path)
    except Exception:
        logger.exception("Could not emit the backup_created event")


def sync_completed(summary: 'SyncSummary') -> None:
    """Announce a finished SillyTavern sync run.

    Args:
        summary: The :class:`~src.sillytavern_sync.SyncSummary` of the run;
            its counters are passed on as the payload.
    """
    try:
        from src import hooks
        hooks.emit_safe(
            hooks.EVENT_SYNC_COMPLETED,
            pushed=summary.pushed,
            pulled=summary.pulled,
            linked=summary.linked,
            deleted=summary.deleted,
            skipped=summary.skipped,
            errors=list(summary.errors),
        )
    except Exception:
        logger.exception("Could not emit the sync_completed event")


def generation_finished(card_id: int | None, provider: str, model: str) -> None:
    """Announce a finished chat generation.

    Args:
        card_id: The id of the card the chat belongs to.
        provider: The name of the provider that served the generation.
        model: The model that served the generation.
    """
    try:
        from src import hooks
        hooks.emit_safe(
            hooks.EVENT_GENERATION_FINISHED,
            card_id=card_id,
            provider=provider or '',
            model=model or '',
        )
    except Exception:
        logger.exception("Could not emit the generation_finished event")


__all__ = [
    'backup_created',
    'card_deleted',
    'card_imported',
    'card_saved',
    'generation_finished',
    'sync_completed',
]

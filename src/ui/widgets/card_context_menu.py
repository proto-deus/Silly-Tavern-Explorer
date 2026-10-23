"""Shared right-click menu for character-card thumbnails.

Both the Library grid and the character sidebar right-click the same
``CardThumbnail`` widget, so the menu structure lives here once and each
call site only supplies the callbacks it can service.  Keys with no
callback are left out of the menu entirely.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QMenu, QWidget

# Menu groups, in order.  ``None`` starts a new group (rendered as a
# separator); the tuples are (handler key, label) with ``{n}`` replaced by
# the selection count in multi-select mode.
_GROUPS: tuple[tuple[tuple[str, str], ...] | None, ...] = (
    (('edit', 'Edit'), ('favorite', 'Favorite'), ('collections', 'Add to Collection...')),
    (
        ('full_image', 'View Full Image'),
        ('open_folder', 'Reveal in Folder'),
        ('export_png', 'Export PNG...'),
        ('export_json', 'Export JSON...'),
        ('duplicate', 'Duplicate'),
    ),
    (('delete', 'Delete'),),
)

# Label variants when several cards are selected: the bulk handlers apply
# to the whole selection, so the labels say how many cards are affected.
_MULTI_LABELS: dict[str, str] = {
    'delete': 'Delete ({n})',
}


def card_context_menu_entries(
    handlers: Mapping[str, Callable[[], None]],
    *,
    favorite_on: bool = False,
    multi_count: int = 1,
) -> list[tuple[str, Callable[[], None]] | None]:
    """Build the (label, callback) entries of the card context menu.

    *favorite_on* selects the single-card "Add to Favorites" / "Remove from
    Favorites" label.  When *multi_count* > 1 the favorite and delete items
    become explicit bulk actions: the menu offers both "Add to Favorites
    (n)" and "Remove from Favorites (n)" (the selection can mix states) and
    "Delete (n)".
    """
    entries: list[tuple[str, Callable[[], None]] | None] = []
    multi = multi_count > 1
    for group in _GROUPS:
        group_entries: list[tuple[str, Callable[[], None]]] = []
        for key, label in group:
            if key == 'favorite' and multi:
                add = handlers.get('favorite_add')
                remove = handlers.get('favorite_remove')
                if add is not None:
                    group_entries.append((f'Add to Favorites ({multi_count})', add))
                if remove is not None:
                    group_entries.append((f'Remove from Favorites ({multi_count})', remove))
                continue
            callback = handlers.get(key)
            if callback is None:
                continue
            if key == 'favorite' and not favorite_on:
                label = 'Add to Favorites'
            elif key == 'favorite':
                label = 'Remove from Favorites'
            elif key in _MULTI_LABELS and multi:
                label = _MULTI_LABELS[key].format(n=multi_count)
            group_entries.append((label, callback))
        if not group_entries:
            continue
        if entries:
            entries.append(None)
        entries.extend(group_entries)
    return entries


def show_card_context_menu(
    parent: QWidget,
    global_pos: QPoint,
    handlers: Mapping[str, Callable[[], None]],
    *,
    favorite_on: bool = False,
    multi_count: int = 1,
) -> None:
    """Build and run the card context menu at *global_pos*.

    *handlers* maps the keys used by :data:`_GROUPS` (plus ``favorite_add``
    / ``favorite_remove`` for multi-select) to zero-argument callbacks.
    Keys without a callback are omitted, so a caller can show a subset.
    """
    entries = card_context_menu_entries(
        handlers, favorite_on=favorite_on, multi_count=multi_count,
    )
    if not entries:
        return
    menu = QMenu(parent)
    for entry in entries:
        if entry is None:
            menu.addSeparator()
            continue
        label, callback = entry
        action = menu.addAction(label)
        # ``QAction.triggered`` carries a ``checked`` bool; never bind the
        # callback directly or it would receive it as an argument.
        action.triggered.connect(lambda _checked=False, cb=callback: cb())
    menu.exec(global_pos)

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShortcutAction:
    """Declarative definition of a menu action and its keyboard shortcut.

    Pure-data class so the registry can be unit-tested without a Qt event
    loop.  ``menu_path`` uses ``/`` to denote sub-menus (e.g. ``"View/Sort"``).
    ``shortcut_aliases`` holds extra key sequences that trigger the same
    action (e.g. ``Ctrl+P`` next to ``Ctrl+K``); they are never unique to
    one action.
    """

    action_id: str
    label: str
    shortcut: str
    menu_path: str
    separator_before: bool = False
    shortcut_aliases: tuple[str, ...] = ()


ACTIONS: list[ShortcutAction] = [
    # --- File ---
    ShortcutAction('file.import', 'Import Cards...', 'Ctrl+I', 'File'),
    ShortcutAction('file.export_png', 'Export as PNG', 'Ctrl+E', 'File'),
    ShortcutAction('file.export_json', 'Export as JSON', '', 'File'),
    ShortcutAction('file.backup', 'Backup Now', '', 'File', separator_before=True),
    ShortcutAction('file.backup_library', 'Backup Library...', '', 'File'),
    ShortcutAction('file.backup_encrypted', 'Encrypted Backup...', '', 'File'),
    ShortcutAction('file.restore_library', 'Restore Library...', '', 'File'),
    ShortcutAction('file.check_library', 'Check Library for Problems...', '', 'File'),
    ShortcutAction('file.quit', 'Quit', 'Ctrl+Q', 'File', separator_before=True),

    # --- Edit ---
    ShortcutAction('edit.save', 'Save', 'Ctrl+S', 'Edit'),
    ShortcutAction('edit.revert', 'Revert', 'Ctrl+R', 'Edit'),
    ShortcutAction('edit.undo', 'Undo', 'Ctrl+Z', 'Edit', separator_before=True),
    ShortcutAction('edit.redo', 'Redo', 'Ctrl+Shift+Z', 'Edit'),
    ShortcutAction('edit.duplicate', 'Duplicate Card', 'Ctrl+D', 'Edit'),
    ShortcutAction('edit.delete', 'Delete Card', 'Del', 'Edit', separator_before=True),
    ShortcutAction('edit.find', 'Find', 'Ctrl+F', 'Edit', separator_before=True),

    # --- View ---
    ShortcutAction('view.favorites', 'Toggle Favorites Only', 'F', 'View'),
    ShortcutAction('view.sort_name', 'Sort by Name', '', 'View/Sort', separator_before=True),
    ShortcutAction('view.sort_date', 'Sort by Date Added', '', 'View/Sort'),
    ShortcutAction('view.sort_tokens', 'Sort by Token Count', '', 'View/Sort'),
    ShortcutAction('view.sort_favorites', 'Sort by Favorites', '', 'View/Sort'),
    ShortcutAction('view.sort_rating', 'Sort by Rating', '', 'View/Sort'),
    ShortcutAction('view.sort_random', 'Sort by Random', '', 'View/Sort'),
    ShortcutAction('view.refresh', 'Refresh', 'F5', 'View', separator_before=True),
    ShortcutAction(
        'view.zoom_in', 'Zoom In', 'Ctrl+=', 'View',
        shortcut_aliases=('Ctrl++',),
    ),
    ShortcutAction('view.zoom_out', 'Zoom Out', 'Ctrl+-', 'View'),
    ShortcutAction('view.zoom_reset', 'Reset Zoom', 'Ctrl+0', 'View'),
    ShortcutAction('view.font_size', 'Font Size...', '', 'View'),
    ShortcutAction('view.find_duplicates', 'Find Duplicates...', '', 'View', separator_before=True),
    ShortcutAction('view.statistics', 'Statistics...', '', 'View'),
    ShortcutAction('view.lint_card', 'Check Card...', '', 'View'),
    ShortcutAction('view.batch_edit', 'Batch Edit...', '', 'View'),
    ShortcutAction('view.semantic_search', 'Semantic Search...', '', 'View'),
    ShortcutAction('view.usage_stats', 'Usage & Cost...', '', 'View'),
    ShortcutAction(
        'view.command_palette', 'Command Palette...', 'Ctrl+K', 'View',
        separator_before=True, shortcut_aliases=('Ctrl+P',),
    ),

    # --- Tab navigation (Ctrl+1 .. Ctrl+6) ---
    ShortcutAction('tab.library', 'Library', 'Ctrl+1', 'View/Tabs', separator_before=True),
    ShortcutAction('tab.edit', 'Edit', 'Ctrl+2', 'View/Tabs'),
    ShortcutAction('tab.gallery', 'Gallery', 'Ctrl+3', 'View/Tabs'),
    ShortcutAction('tab.generate', 'Generate', 'Ctrl+4', 'View/Tabs'),
    ShortcutAction('tab.lorebooks', 'Lorebooks', 'Ctrl+5', 'View/Tabs'),
    ShortcutAction('tab.test', 'Test', 'Ctrl+6', 'View/Tabs'),

    # --- SillyTavern ---
    ShortcutAction('st.configure', 'Configure SillyTavern...', '', 'SillyTavern'),
    ShortcutAction('st.sync', 'Sync Library...', 'Ctrl+Shift+L', 'SillyTavern', separator_before=True),
    ShortcutAction('st.sync_lorebooks', 'Sync Lorebooks...', 'Ctrl+Shift+W', 'SillyTavern'),
    ShortcutAction('st.push_all', 'Push All to SillyTavern', '', 'SillyTavern', separator_before=True),
    ShortcutAction('st.pull_all', 'Pull All from SillyTavern', '', 'SillyTavern'),
    ShortcutAction('st.push_selected', 'Push Selected to SillyTavern', '', 'SillyTavern', separator_before=True),
    ShortcutAction('st.pull_selected', 'Pull Selected from SillyTavern', '', 'SillyTavern'),
    ShortcutAction('st.refresh', 'Refresh ST Status', '', 'SillyTavern', separator_before=True),

    # --- Settings ---
    ShortcutAction('settings.open', 'Settings...', 'Ctrl+,', 'Settings'),

    # --- Help ---
    ShortcutAction('help.about', 'About ST Explorer', '', 'Help'),
    ShortcutAction('help.view_log', 'View Log', '', 'Help'),
    ShortcutAction('help.crash_report', 'Create Crash Report...', '', 'Help'),
]

_ACTION_INDEX: dict[str, ShortcutAction] = {a.action_id: a for a in ACTIONS}


def get_action(action_id: str) -> ShortcutAction | None:
    """Look up a single action by id, or ``None`` if not found."""
    return _ACTION_INDEX.get(action_id)


def get_shortcut(action_id: str) -> str:
    """Return the shortcut string for *action_id*, or ``''`` if not found."""
    action = _ACTION_INDEX.get(action_id)
    return action.shortcut if action else ''


def action_ids() -> list[str]:
    """Return all registered action ids in declaration order."""
    return [a.action_id for a in ACTIONS]


def unique_ids() -> bool:
    """True if every action id in the registry is unique."""
    return len(_ACTION_INDEX) == len(ACTIONS)

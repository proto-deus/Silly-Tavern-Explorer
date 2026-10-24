import sys


def _default_font_family() -> str:
    """Return the native UI font family for the current platform.

    Qt substitutes automatically when a family is unavailable, so the
    fallback chain stays safe on unusual installs.
    """
    if sys.platform == 'win32':
        return 'Segoe UI'
    if sys.platform == 'darwin':
        return 'SF Pro Text'
    return 'Noto Sans'


def _logical_dpi() -> float:
    """Logical DPI of the primary screen (96 where unavailable)."""
    try:
        from PyQt6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            dpi = screen.logicalDotsPerInch()
            if dpi and dpi > 0:
                return dpi
    except Exception:
        pass
    return 96.0


def px_to_pt(px: int) -> int:
    """Convert a pixel font size to points at the current logical DPI.

    Pure helper (dpi injectable in tests via monkeypatching ``_logical_dpi``).
    Always returns >= 1 so QFont::setPointSize never receives an invalid value.
    """
    dpi = max(1.0, float(_logical_dpi()))
    return max(1, round(int(px) * 72.0 / dpi))


def apply_theme(app, font_size: int = 13, theme: str = 'dark') -> None:
    """Apply the theme and application font for the given size.

    Sets the application-wide default font (so menus, buttons, dialogs,
    tooltips, and native windows all scale) in addition to the stylesheet,
    then re-applies the theme at the application level.

    The font uses a *point* size derived from the configured pixel size:
    a pixel-only font leaves ``QFont.pointSize() == -1``, and Qt-internal
    code that derives a point-based font from the app font (native file /
    message dialogs on Windows, tooltip sizing, ...) then trips
    "QFont::setPointSize: Point size <= 0 (-1)".  Widget-level rendering
    stays pixel-exact via the stylesheet's ``font-size: Npx`` rule.
    """
    from PyQt6.QtGui import QFont

    set_base_font_size(font_size)
    font = QFont(_default_font_family())
    font.setPointSize(px_to_pt(font_size))
    app.setFont(font)
    app.setStyleSheet(get_theme(theme, font_size))


# The configured base font size. Widgets that want secondary/heading text call
# ui_font_px() instead of hardcoding a pixel size: a widget's own stylesheet
# takes precedence over the application-wide `QWidget { font-size }` rule, so
# a literal `font-size: 11px` silently ignored the View > Font Size setting for
# that widget.
_base_font_size = 13


def set_base_font_size(size: int) -> None:
    """Record the configured base font size used by :func:`ui_font_px`."""
    global _base_font_size
    _base_font_size = max(6, int(size))


def base_font_size() -> int:
    return _base_font_size


def ui_font_px(delta: int = 0) -> int:
    """Return a per-widget font size that tracks the global Font Size setting.

    *delta* shifts it relative to the configured base, so secondary text stays
    proportionally smaller (e.g. ``ui_font_px(-2)``). Never returns less than
    7px, which would be unreadable.
    """
    return max(7, _base_font_size + delta)


def _arrow_path(name: str) -> str:
    """Absolute POSIX-style path of a bundled arrow image.

    Works both from source and frozen (PyInstaller keeps the icons
    directory under ``sys._MEIPASS`` with the same relative layout).
    """
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / 'resources' / 'icons' / name).as_posix()


#: Colour palettes for the built-in themes.  The stylesheet itself is a
#: single template; only the palette changes between the themes, so a new
#: theme never needs a copy of the 300-line QSS.
DARK_PALETTE = {'bg': '#1e1e1e', 'bg_alt': '#252525', 'panel': '#2a2a2a', 'bg_deep': '#1a1a1a', 'border_dim': '#3a3a3a', 'border': '#3d3d3d', 'border_strong': '#444444', 'border_subtle': '#333333', 'muted': '#555555', 'muted_dim': '#666666', 'fg': '#e0e0e0', 'fg_dim': '#d0d0d0', 'fg_alt': '#cccccc', 'fg_muted': '#aaaaaa', 'fg_bright': '#ffffff', 'hover': '#3a3f4b', 'select': '#3a5068', 'accent_dim': '#4a5060', 'accent': '#5b9bd5', 'ok_dim': '#2a5a3a', 'ok': '#4a8a5a', 'ok_border': '#3a6a4a', 'ok_fg': '#5a9a6a', 'input': '#2a3040'}

LIGHT_PALETTE = {'bg': '#f4f4f4', 'bg_alt': '#eaeaea', 'panel': '#ffffff', 'bg_deep': '#e0e0e0', 'border_dim': '#d6d6d6', 'border': '#cccccc', 'border_strong': '#b4b4b4', 'border_subtle': '#dcdcdc', 'muted': '#9a9a9a', 'muted_dim': '#8a8a8a', 'fg': '#1e1e1e', 'fg_dim': '#333333', 'fg_alt': '#444444', 'fg_muted': '#666666', 'fg_bright': '#000000', 'hover': '#dde4ee', 'select': '#cfe0f2', 'accent_dim': '#9db4cf', 'accent': '#1f6fc0', 'ok_dim': '#dcecdc', 'ok': '#2e7d32', 'ok_border': '#a8d5ab', 'ok_fg': '#1b5e20', 'input': '#ffffff'}

#: The stylesheet template.  ``$token`` placeholders are replaced with the
#: palette colours; ``string.Template`` is used because CSS braces would
#: break ``str.format``.
_THEME_TEMPLATE = """
QMainWindow {
    background-color: $bg;
}

QWidget {
    background-color: $bg;
    color: $fg;
    font-size: __FONT_SIZE__px;
}

QMenuBar {
    background-color: $bg;
    color: $fg;
    padding: 2px;
}

QMenuBar::item {
    background-color: transparent;
    padding: 6px 14px;
}

QMenuBar::item:selected {
    background-color: $hover;
    border-radius: 4px;
}

QMenu {
    background-color: $panel;
    color: $fg;
    border: 1px solid $border_strong;
}

QMenu::item {
    padding: 6px 28px 6px 20px;
}

QMenu::item:selected {
    background-color: $select;
}

/* Checkable menu items (the multi-select collection dropdown) need a
   checkbox that is visible on the dark menu background. */
QMenu::indicator {
    width: 14px;
    height: 14px;
    margin-left: 2px;
    border: 1px solid $muted;
    border-radius: 3px;
    background-color: $panel;
}

QMenu::indicator:checked {
    background-color: $accent;
    border: 1px solid $accent;
}

QMenu::separator {
    height: 1px;
    background-color: $border_dim;
    margin: 4px 8px;
}

QTabWidget::pane {
    border: 1px solid $border_dim;
    background-color: $bg_alt;
}

QTabBar::tab {
    background-color: $panel;
    color: $fg_muted;
    border: 1px solid $border_dim;
    border-bottom: none;
    padding: 8px 20px;
    margin-right: 2px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}

QTabBar::tab:selected {
    background-color: $bg_alt;
    color: $fg;
    border-bottom: 2px solid $accent;
}

QTabBar::tab:hover:!selected {
    background-color: $border_subtle;
    color: $fg_alt;
}

QGroupBox {
    border: 1px solid $border_dim;
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 16px;
    font-weight: bold;
    color: $fg_alt;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 8px;
    color: $fg_muted;
}

QLineEdit, QComboBox {
    background-color: $panel;
    border: 1px solid $border_strong;
    border-radius: 4px;
    padding: 5px 8px;
    color: $fg;
    selection-background-color: $accent;
}

QLineEdit:focus, QComboBox:focus {
    border: 1px solid $accent;
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 22px;
    border: none;
    padding-right: 8px;
}

QComboBox::down-arrow {
    image: url("__COMBO_ARROW_DOWN__");
    width: 10px;
    height: 6px;
}

QComboBox QAbstractItemView {
    background-color: $panel;
    border: 1px solid $border_strong;
    color: $fg;
    selection-background-color: $select;
}

/* The multi-select collection dropdown is a QToolButton standing in for a
   combo: give it the combo's look, not the push-button's.  Text sits at
   the left edge like a combo's, which matters in the sidebar where the
   button is stretched to the column width. */
QToolButton {
    background-color: $panel;
    border: 1px solid $border_strong;
    border-radius: 4px;
    padding: 5px 8px;
    color: $fg;
    text-align: left;
}

QToolButton:hover {
    background-color: $hover;
    border: 1px solid $muted_dim;
}

QToolButton:focus {
    border: 1px solid $accent;
}

QToolButton:pressed, QToolButton::menu-button:pressed {
    background-color: $input;
}

QToolButton::menu-indicator {
    image: url("__COMBO_ARROW_DOWN__");
    width: 10px;
    height: 6px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
    padding-right: 6px;
}

QTextEdit, QPlainTextEdit {
    background-color: $bg_deep;
    border: 1px solid $border_dim;
    border-radius: 4px;
    padding: 4px;
    color: $fg_dim;
    selection-background-color: $accent;
}

QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid $accent;
}

QPushButton {
    background-color: $hover;
    border: 1px solid $muted;
    border-radius: 4px;
    padding: 6px 16px;
    color: $fg;
    font-weight: bold;
}

QPushButton:hover {
    background-color: $accent_dim;
    border: 1px solid $muted_dim;
}

QPushButton:focus {
    border: 1px solid $accent;
}

QPushButton:checked {
    background-color: $ok_dim;
    border: 1px solid $ok;
}

QPushButton:checked:hover {
    background-color: $ok_border;
    border: 1px solid $ok_fg;
}

QPushButton:pressed {
    background-color: $input;
}

QPushButton:disabled {
    background-color: $panel;
    color: $muted_dim;
    border: 1px solid $border_dim;
}

QScrollArea {
    border: none;
    background-color: transparent;
}

QScrollBar:vertical {
    background-color: $bg;
    width: 10px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background-color: $border_strong;
    border-radius: 5px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background-color: $muted;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar:horizontal {
    background-color: $bg;
    height: 10px;
    margin: 0;
}

QScrollBar::handle:horizontal {
    background-color: $border_strong;
    border-radius: 5px;
    min-width: 30px;
}

QScrollBar::handle:horizontal:hover {
    background-color: $muted;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}

QSplitter::handle {
    background-color: $border_dim;
}

QSplitter::handle:horizontal {
    width: 2px;
}

QSplitter::handle:vertical {
    height: 2px;
}

QLabel {
    background-color: transparent;
}

QRadioButton {
    spacing: 6px;
    color: $fg_dim;
    background-color: transparent;
}

QRadioButton:checked {
    color: $fg_bright;
    font-weight: bold;
}

QRadioButton::indicator {
    width: 16px;
    height: 16px;
}

QRadioButton::indicator:checked {
    background-color: $accent;
    border: 2px solid $accent;
    border-radius: 8px;
}

QRadioButton::indicator:unchecked {
    background-color: $panel;
    border: 2px solid $muted;
    border-radius: 8px;
}

QDoubleSpinBox, QSpinBox {
    background-color: $panel;
    border: 1px solid $border_strong;
    border-radius: 4px;
    padding: 4px 8px;
    color: $fg;
}

/* Style the spin box buttons explicitly: without this, the native
   windows11 style lays the buttons out side by side while the stylesheet
   box model sizes the line-edit child across their left half, so real
   mouse clicks on the visible up arrow land on the editor and do nothing. */
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    border: none;
    border-left: 1px solid $border_strong;
    background-color: $border_subtle;
    border-top-right-radius: 4px;
}

QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    border: none;
    border-left: 1px solid $border_strong;
    background-color: $border_subtle;
    border-bottom-right-radius: 4px;
}

QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: $border;
}

QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {
    background-color: $panel;
}

QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: url("__SPIN_ARROW_UP__");
    width: 10px;
    height: 6px;
}

QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: url("__SPIN_ARROW_DOWN__");
    width: 10px;
    height: 6px;
}

QDoubleSpinBox:focus, QSpinBox:focus {
    border: 1px solid $accent;
}

QMessageBox {
    background-color: $bg_alt;
}

QDialog {
    background-color: $bg_alt;
}

QDialogButtonBox {
    background-color: transparent;
}

QToolTip {
    background-color: $panel;
    color: $fg;
    border: 1px solid $border_strong;
    padding: 4px;
}

/* --- Check boxes ------------------------------------------------------ */

QCheckBox {
    spacing: 6px;
    background-color: transparent;
    color: $fg_dim;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid $muted;
    border-radius: 3px;
    background-color: $panel;
}

QCheckBox::indicator:hover {
    border: 1px solid $accent;
}

QCheckBox::indicator:checked {
    background-color: $accent;
    border: 1px solid $accent;
}

QCheckBox:focus {
    color: $fg_bright;
}

QCheckBox:disabled {
    color: $muted_dim;
}

/* --- Item views (lists, trees, tables) -------------------------------- */

QAbstractItemView {
    background-color: $bg_deep;
    alternate-background-color: $bg_alt;
    border: 1px solid $border_dim;
    border-radius: 4px;
    color: $fg;
    selection-background-color: $select;
    selection-color: $fg_bright;
    outline: none;
}

QListWidget, QTreeWidget, QTableWidget,
QListView, QTreeView, QTableView {
    background-color: $bg_deep;
    alternate-background-color: $bg_alt;
    border: 1px solid $border_dim;
    border-radius: 4px;
    color: $fg;
    selection-background-color: $select;
    selection-color: $fg_bright;
    outline: none;
}

QListWidget::item, QTreeWidget::item, QTableWidget::item {
    padding: 3px;
    color: $fg;
}

QListWidget::item:hover, QTreeWidget::item:hover, QTableWidget::item:hover {
    background-color: $hover;
}

QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected {
    background-color: $select;
    color: $fg_bright;
}

QListWidget::item:disabled, QTreeWidget::item:disabled {
    color: $muted_dim;
}

QHeaderView::section {
    background-color: $panel;
    color: $fg_muted;
    border: none;
    border-right: 1px solid $border_dim;
    border-bottom: 1px solid $border_dim;
    padding: 4px 6px;
}

QHeaderView::section:hover {
    background-color: $border_subtle;
}

/* --- Progress and the status bar -------------------------------------- */

QProgressBar {
    background-color: $bg_deep;
    border: 1px solid $border_dim;
    border-radius: 4px;
    color: $fg;
    text-align: center;
    min-height: 18px;
}

QProgressBar::chunk {
    background-color: $accent;
    border-radius: 3px;
}

QStatusBar {
    background-color: $bg_alt;
    border-top: 1px solid $border_dim;
    color: $fg_muted;
}

QStatusBar::item {
    border: none;
}

QFrame[frameShape="4"] {
    background-color: $accent;
}

/* --- Library filter chips and dividers -------------------------------- */

QLabel#filterChip, QCheckBox#filterChip {
    background-color: $hover;
    border: 1px solid $muted;
    border-radius: 4px;
    /* Vertical padding stays small: the chip's height is fixed to the
       push-button height at runtime, and the checkbox indicator needs the
       leftover room not to clip. */
    padding: 2px 10px;
    color: $fg;
    font-weight: bold;
}

QWidget#gridSeparator {
    background-color: $border_dim;
}

/* --- Focus and disabled states ---------------------------------------- */

QPushButton:focus {
    border: 1px solid $accent;
}

QTabBar::tab:focus {
    color: $fg_bright;
}

QTabBar::tab:disabled {
    background-color: $bg;
    color: $muted_dim;
}

QMenu::item:disabled {
    color: $muted_dim;
}

QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {
    background-color: $bg_alt;
    border: 1px solid $border_dim;
    color: $muted_dim;
}
"""


def get_theme(theme: str = 'dark', font_size: int = 13) -> str:
    """Return the stylesheet for *theme* with the given base font size.

    Args:
        theme: ``'dark'`` or ``'light'``.  An unknown name falls back to
            ``'dark'`` so a bad setting can never leave the app unstyled.
        font_size: the base pixel font size.

    Returns:
        The complete Qt stylesheet.
    """
    palette = LIGHT_PALETTE if str(theme).strip().lower() == 'light' else DARK_PALETTE
    from string import Template
    css = Template(_THEME_TEMPLATE).safe_substitute(palette)
    return (css.replace('__FONT_SIZE__', str(font_size))
               .replace('__SPIN_ARROW_UP__', _arrow_path('spin_arrow_up.png'))
               .replace('__SPIN_ARROW_DOWN__', _arrow_path('spin_arrow_down.png'))
               .replace('__COMBO_ARROW_DOWN__', _arrow_path('combo_arrow_down.png')))


def get_dark_theme(font_size: int = 13) -> str:
    """Return the dark theme stylesheet with the given base font size."""
    return get_theme('dark', font_size)


def get_light_theme(font_size: int = 13) -> str:
    """Return the light theme stylesheet with the given base font size."""
    return get_theme('light', font_size)


def available_themes() -> list[str]:
    """The names of the built-in themes."""
    return ['dark', 'light']


def palette_for(theme: str) -> dict[str, str]:
    """Return the colour palette of *theme*.

    An unknown name falls back to the dark palette.  Widgets that paint
    themselves (the command palette) take their colours from here so they
    follow the active theme instead of hard-coding the dark one.
    """
    return LIGHT_PALETTE if str(theme).strip().lower() == 'light' else DARK_PALETTE


def current_palette() -> dict[str, str]:
    """The palette of the theme the user has configured."""
    try:
        from src.settings_manager import load_theme
        theme = load_theme()
    except Exception:
        theme = 'dark'
    return palette_for(theme)

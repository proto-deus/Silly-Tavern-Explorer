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


def apply_theme(app, font_size: int = 13) -> None:
    """Apply the dark theme and application font for the given size.

    Sets the application-wide default font (so menus, buttons, dialogs,
    tooltips, and native windows all scale) in addition to the stylesheet,
    then re-applies the dark theme at the application level.

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
    app.setStyleSheet(get_dark_theme(font_size))


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


def get_dark_theme(font_size: int = 13) -> str:
    """Return the dark theme stylesheet with the given base font size."""
    return """
QMainWindow {
    background-color: #1e1e1e;
}

QWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
    font-size: __FONT_SIZE__px;
}

QMenuBar {
    background-color: #1e1e1e;
    color: #e0e0e0;
    padding: 2px;
}

QMenuBar::item {
    background-color: transparent;
    padding: 6px 14px;
}

QMenuBar::item:selected {
    background-color: #3a3f4b;
    border-radius: 4px;
}

QMenu {
    background-color: #2a2a2a;
    color: #e0e0e0;
    border: 1px solid #444;
}

QMenu::item {
    padding: 6px 28px 6px 20px;
}

QMenu::item:selected {
    background-color: #3a5068;
}

QMenu::separator {
    height: 1px;
    background-color: #3a3a3a;
    margin: 4px 8px;
}

QTabWidget::pane {
    border: 1px solid #3a3a3a;
    background-color: #252525;
}

QTabBar::tab {
    background-color: #2a2a2a;
    color: #aaa;
    border: 1px solid #3a3a3a;
    border-bottom: none;
    padding: 8px 20px;
    margin-right: 2px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}

QTabBar::tab:selected {
    background-color: #252525;
    color: #e0e0e0;
    border-bottom: 2px solid #5b9bd5;
}

QTabBar::tab:hover:!selected {
    background-color: #333;
    color: #ccc;
}

QGroupBox {
    border: 1px solid #3a3a3a;
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 16px;
    font-weight: bold;
    color: #ccc;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 8px;
    color: #aaa;
}

QLineEdit, QComboBox {
    background-color: #2a2a2a;
    border: 1px solid #444;
    border-radius: 4px;
    padding: 5px 8px;
    color: #e0e0e0;
    selection-background-color: #5b9bd5;
}

QLineEdit:focus, QComboBox:focus {
    border: 1px solid #5b9bd5;
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
    background-color: #2a2a2a;
    border: 1px solid #444;
    color: #e0e0e0;
    selection-background-color: #3a5068;
}

QTextEdit, QPlainTextEdit {
    background-color: #1a1a1a;
    border: 1px solid #3a3a3a;
    border-radius: 4px;
    padding: 4px;
    color: #d0d0d0;
    selection-background-color: #5b9bd5;
}

QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid #5b9bd5;
}

QPushButton {
    background-color: #3a3f4b;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 6px 16px;
    color: #e0e0e0;
    font-weight: bold;
}

QPushButton:hover {
    background-color: #4a5060;
    border: 1px solid #666;
}

QPushButton:checked {
    background-color: #2a5a3a;
    border: 1px solid #4a8a5a;
}

QPushButton:checked:hover {
    background-color: #3a6a4a;
    border: 1px solid #5a9a6a;
}

QPushButton:pressed {
    background-color: #2a3040;
}

QPushButton:disabled {
    background-color: #2a2a2a;
    color: #666;
    border: 1px solid #3a3a3a;
}

QScrollArea {
    border: none;
    background-color: transparent;
}

QScrollBar:vertical {
    background-color: #1e1e1e;
    width: 10px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background-color: #444;
    border-radius: 5px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background-color: #555;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar:horizontal {
    background-color: #1e1e1e;
    height: 10px;
    margin: 0;
}

QScrollBar::handle:horizontal {
    background-color: #444;
    border-radius: 5px;
    min-width: 30px;
}

QScrollBar::handle:horizontal:hover {
    background-color: #555;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}

QSplitter::handle {
    background-color: #3a3a3a;
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
    color: #d0d0d0;
    background-color: transparent;
}

QRadioButton:checked {
    color: #ffffff;
    font-weight: bold;
}

QRadioButton::indicator {
    width: 16px;
    height: 16px;
}

QRadioButton::indicator:checked {
    background-color: #5b9bd5;
    border: 2px solid #5b9bd5;
    border-radius: 8px;
}

QRadioButton::indicator:unchecked {
    background-color: #2a2a2a;
    border: 2px solid #555;
    border-radius: 8px;
}

QDoubleSpinBox, QSpinBox {
    background-color: #2a2a2a;
    border: 1px solid #444;
    border-radius: 4px;
    padding: 4px 8px;
    color: #e0e0e0;
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
    border-left: 1px solid #444;
    background-color: #333333;
    border-top-right-radius: 4px;
}

QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    border: none;
    border-left: 1px solid #444;
    background-color: #333333;
    border-bottom-right-radius: 4px;
}

QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #3d3d3d;
}

QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {
    background-color: #2a2a2a;
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
    border: 1px solid #5b9bd5;
}

QMessageBox {
    background-color: #252525;
}

QDialog {
    background-color: #252525;
}

QToolTip {
    background-color: #2a2a2a;
    color: #e0e0e0;
    border: 1px solid #444;
    padding: 4px;
}
""".replace('__FONT_SIZE__', str(font_size)) \
   .replace('__SPIN_ARROW_UP__', _arrow_path('spin_arrow_up.png')) \
   .replace('__SPIN_ARROW_DOWN__', _arrow_path('spin_arrow_down.png')) \
   .replace('__COMBO_ARROW_DOWN__', _arrow_path('combo_arrow_down.png'))

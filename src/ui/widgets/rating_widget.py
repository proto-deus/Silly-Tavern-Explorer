from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

_FILLED = '★'
_EMPTY = '☆'
_COLOR_FILLED = '#f5c542'
_COLOR_EMPTY = '#666'
_FONT_SIZE = 16


def clamp_rating(value: int) -> int:
    """Clamp a rating to the valid 0-5 range. Pure function."""
    return max(0, min(5, int(value)))


def next_rating(current: int, clicked_star: int) -> int:
    """Rating after clicking *clicked_star* (1-5).

    Clicking the star that equals the current rating clears it back to 0.
    Pure function.
    """
    clicked_star = clamp_rating(clicked_star)
    if clicked_star == 0:
        return 0
    return 0 if current == clicked_star else clicked_star


def filled_star_count(rating: int, hover: int = 0) -> int:
    """How many stars should render filled (hover preview wins). Pure."""
    hover = clamp_rating(hover)
    if hover > 0:
        return hover
    return clamp_rating(rating)


class _StarLabel(QLabel):
    def __init__(self, index: int, on_hover, on_click, parent=None):
        super().__init__(_EMPTY, parent)
        self._index = index
        self._on_hover_cb = on_hover
        self._on_click_cb = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f'font-size: {_FONT_SIZE}px; color: {_COLOR_EMPTY};')

    def mousePressEvent(self, event) -> None:
        self._on_click_cb(self._index)

    def enterEvent(self, event) -> None:
        self._on_hover_cb(self._index)

    def leaveEvent(self, event) -> None:
        self._on_hover_cb(0)


class RatingWidget(QWidget):
    """Five clickable stars for the 0-5 library rating."""

    ratingChanged = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._rating = 0
        self._hover = 0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._stars: list[_StarLabel] = []
        for i in range(1, 6):
            star = _StarLabel(i, self._on_hover, self._on_clicked, self)
            self._stars.append(star)
            layout.addWidget(star)

    def rating(self) -> int:
        return self._rating

    def set_rating(self, value: int) -> None:
        """Set the displayed rating without emitting ratingChanged."""
        self._rating = clamp_rating(value)
        self._apply_stars()

    def _apply_stars(self) -> None:
        filled = filled_star_count(self._rating, self._hover)
        for i, star in enumerate(self._stars, start=1):
            if i <= filled:
                star.setText(_FILLED)
                star.setStyleSheet(f'font-size: {_FONT_SIZE}px; color: {_COLOR_FILLED};')
            else:
                star.setText(_EMPTY)
                star.setStyleSheet(f'font-size: {_FONT_SIZE}px; color: {_COLOR_EMPTY};')

    def _on_hover(self, index: int) -> None:
        self._hover = index
        self._apply_stars()

    def _on_clicked(self, index: int) -> None:
        new_value = next_rating(self._rating, index)
        changed = new_value != self._rating
        self._rating = new_value
        self._apply_stars()
        if changed:
            self.ratingChanged.emit(new_value)

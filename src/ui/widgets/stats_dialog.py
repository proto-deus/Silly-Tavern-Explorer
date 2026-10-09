from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.database import LibraryDatabase

logger = logging.getLogger(__name__)

# Palette for the horizontal bars — cycled so adjacent bars contrast.
_BAR_COLORS = (
    '#5b9bd5', '#ed7d31', '#a5a5a5', '#ffc000',
    '#4472c4', '#70ad47', '#9e480e', '#636363',
)

# Maximum pixel width of a bar when the largest value fills it.
_MAX_BAR_WIDTH = 260


def compute_bar_width(value: int | float, max_value: int | float, max_width: int = _MAX_BAR_WIDTH) -> int:
    """Compute the pixel width of a bar for *value* relative to *max_value*.

    Pure function so the rendering math can be unit-tested without Qt.
    Returns at least 2px for any non-zero value so the bar is visible.
    """
    if max_value <= 0 or value <= 0:
        return 0
    return max(2, int((value / max_value) * max_width))


def top_n_tags(counts: dict[str, int], n: int = 10) -> list[tuple[str, int]]:
    """Return the top-*n* tags by count, sorted descending then by name.

    Pure function so the ranking logic can be unit-tested without Qt.
    """
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def _color_for(index: int) -> str:
    return _BAR_COLORS[index % len(_BAR_COLORS)]


class _BarRow(QWidget):
    """A label + colored bar + value, laid out horizontally."""

    def __init__(self, label: str, value: int, max_value: int, color: str, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)

        name_label = QLabel(label)
        name_label.setFixedWidth(160)
        name_label.setStyleSheet('color: #d0d0d0;')
        layout.addWidget(name_label)

        bar_container = QFrame()
        bar_container.setFixedHeight(18)
        bar_layout = QHBoxLayout(bar_container)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(0)
        width = compute_bar_width(value, max_value)
        bar = QFrame()
        bar.setFixedWidth(width)
        bar.setStyleSheet(f'background-color: {color}; border-radius: 3px;')
        bar_layout.addWidget(bar)
        bar_layout.addStretch()
        layout.addWidget(bar_container, 1)

        val_label = QLabel(f'{value:,}')
        val_label.setFixedWidth(60)
        val_label.setStyleSheet('color: #aaa;')
        layout.addWidget(val_label)


class _Section(QWidget):
    """A titled section with a vertical list of bar rows."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        header = QLabel(title)
        header.setStyleSheet('font-weight: bold; color: #e0e0e0; border-bottom: 1px solid #3a3a3a;')
        layout.addWidget(header)
        self._body = QVBoxLayout()
        self._body.setSpacing(0)
        layout.addLayout(self._body)

    def add_bar(self, label: str, value: int, max_value: int, index: int) -> None:
        row = _BarRow(label, value, max_value, _color_for(index))
        self._body.addWidget(row)

    def add_text(self, text: str) -> None:
        label = QLabel(text)
        label.setStyleSheet('color: #b0b0b0; padding: 4px 0;')
        label.setWordWrap(True)
        self._body.addWidget(label)

    def add_spacer(self) -> None:
        self._body.addSpacing(8)


class StatsDialog(QDialog):
    """Statistics dashboard showing aggregate library metrics.

    Renders bars as colored ``QFrame`` widgets (no external charting
    dependency).  Launched from the View menu.
    """

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle('Library Statistics')
        self.setMinimumSize(640, 700)

        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(12, 12, 12, 12)
        self._content_layout.setSpacing(4)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        refresh_btn = QPushButton('Refresh')
        refresh_btn.clicked.connect(self._refresh)
        btn_row.addWidget(refresh_btn)
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

        self._refresh()

    def _refresh(self) -> None:
        # Clear previous content.
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        stats = self.db.get_detailed_stats()
        counts = self.db.get_tag_counts()
        creators = self.db.get_creator_counts()
        specs = self.db.get_spec_version_counts()
        per_week = self.db.get_cards_per_week()

        # --- Summary ---
        summary = _Section('Summary')
        summary.add_text(f"Total cards: {stats['count']:,}")
        summary.add_text(f"Total tokens: {stats['total_tokens']:,}")
        summary.add_text(f"Average tokens per card: {stats['avg_tokens']:,}")
        summary.add_text(f"Min tokens: {stats['min_tokens']:,}  |  Max tokens: {stats['max_tokens']:,}")
        summary.add_text(f"Favorites: {stats['favorites']:,}")
        self._content_layout.addWidget(summary)

        # --- Token distribution (per-card) ---
        token_section = _Section('Token Count Distribution')
        all_cards = self.db.get_all(sort_by='token_count')
        if all_cards:
            # Bucket counts into ranges for a histogram-like view.
            buckets = _bucket_tokens(all_cards)
            for i, (label, count) in enumerate(buckets):
                token_section.add_bar(label, count, max((c for _, c in buckets), default=1), i)
        else:
            token_section.add_text('No cards in library.')
        self._content_layout.addWidget(token_section)

        # --- Top tags ---
        tag_section = _Section('Top Tags')
        top_tags = top_n_tags(counts, 10)
        if top_tags:
            max_tag = top_tags[0][1]
            for i, (tag, count) in enumerate(top_tags):
                tag_section.add_bar(tag, count, max_tag, i)
        else:
            tag_section.add_text('No tags found.')
        self._content_layout.addWidget(tag_section)

        # --- Spec version distribution ---
        spec_section = _Section('Spec Version Distribution')
        if specs:
            max_spec = max(specs.values())
            for i, (ver, count) in enumerate(specs.items()):
                spec_section.add_bar(f'v{ver}', count, max_spec, i)
        else:
            spec_section.add_text('No cards in library.')
        self._content_layout.addWidget(spec_section)

        # --- Creator leaderboard ---
        creator_section = _Section('Top Creators')
        if creators:
            max_creator = creators[0][1]
            for i, (creator, count) in enumerate(creators[:10]):
                creator_section.add_bar(creator, count, max_creator, i)
        else:
            creator_section.add_text('No cards in library.')
        self._content_layout.addWidget(creator_section)

        # --- Cards added per week ---
        week_section = _Section('Cards Added Per Week')
        if per_week:
            max_week = max(c for _, c in per_week)
            for i, (week, count) in enumerate(per_week):
                week_section.add_bar(week, count, max_week, i)
        else:
            week_section.add_text('No cards in library.')
        self._content_layout.addWidget(week_section)

        self._content_layout.addStretch()


def _bucket_tokens(cards: list[dict]) -> list[tuple[str, int]]:
    """Bucket card token counts into ranges for a histogram view.

    Pure function so the bucketing logic can be unit-tested without Qt.
    Buckets: 0-499, 500-999, 1000-1999, 2000-3999, 4000+.
    """
    ranges = [
        ('0-499', 0, 500),
        ('500-999', 500, 1000),
        ('1000-1999', 1000, 2000),
        ('2000-3999', 2000, 4000),
        ('4000+', 4000, None),
    ]
    buckets: list[tuple[str, int]] = []
    for label, lo, hi in ranges:
        count = 0
        for card in cards:
            tc = card.get('token_count', 0)
            if tc >= lo and (hi is None or tc < hi):
                count += 1
        buckets.append((label, count))
    return buckets

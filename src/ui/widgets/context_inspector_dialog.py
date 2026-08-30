from __future__ import annotations

from html import escape

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from src.chat_builder import ContextPlan

_SECTION_COLOR = '#8ecbff'
_MUTED_COLOR = '#8a8a8a'
_BODY_COLOR = '#d0d0d0'
_WARN_COLOR = '#ff9d5c'


def format_summary(plan: ContextPlan, context_size: int = 0) -> str:
    """One-line summary of total usage, warning when over the context window."""
    if context_size:
        text = f'{plan.total_tokens:,} / {context_size:,} tokens'
        if plan.total_tokens > context_size:
            text += ' — over the configured context size (older messages will be trimmed)'
    else:
        text = f'{plan.total_tokens:,} tokens'
    return text


def format_plan_text(plan: ContextPlan) -> str:
    """Plain-text rendering of the plan (used for the Copy button)."""
    lines: list[str] = []
    for section in plan.sections:
        lines.append(f'== {section.title} ({section.tokens:,} tokens) ==')
        if section.text:
            lines.append(section.text)
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def format_plan_html(plan: ContextPlan) -> str:
    """HTML rendering of the plan for the read-only view."""
    parts: list[str] = []
    for section in plan.sections:
        parts.append(
            f'<div style="margin-top: 12px;">'
            f'<b style="color: {_SECTION_COLOR};">{escape(section.title)}</b>'
            f'<span style="color: {_MUTED_COLOR};"> · {section.tokens:,} tokens</span>'
            f'</div>'
        )
        body = escape(section.text).replace('\n', '<br>')
        parts.append(
            f'<div style="margin: 2px 0 0 12px; color: {_BODY_COLOR};">{body}</div>'
        )
    return ''.join(parts)


class ContextInspectorDialog(QDialog):
    """Read-only view of the exact context a chat request will send."""

    def __init__(
        self,
        plan: ContextPlan,
        context_size: int = 0,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle('Context Inspector')
        self.resize(680, 600)
        self._plan = plan

        layout = QVBoxLayout(self)

        summary_text = format_summary(plan, context_size)
        over = bool(context_size) and plan.total_tokens > context_size
        color = _WARN_COLOR if over else _MUTED_COLOR
        self._summary = QLabel(summary_text)
        self._summary.setStyleSheet(f'color: {color};')
        layout.addWidget(self._summary)

        self._view = QTextBrowser()
        self._view.setOpenExternalLinks(False)
        self._view.setStyleSheet(
            'QTextBrowser { background-color: #1e1e1e; border: 1px solid #3a3a3a; }'
        )
        self._view.setHtml(format_plan_html(plan))
        layout.addWidget(self._view, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        copy_btn = QPushButton('Copy')
        copy_btn.clicked.connect(self._copy_plan)
        buttons.addWidget(copy_btn)
        close_btn = QPushButton('Close')
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    def _copy_plan(self) -> None:
        text = format_plan_text(self._plan)
        if text.strip():
            QGuiApplication.clipboard().setText(text)

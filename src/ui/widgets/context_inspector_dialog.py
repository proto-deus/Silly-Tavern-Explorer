from __future__ import annotations

from collections.abc import Callable
from html import escape

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from src.chat_builder import ContextPlan
from src.ui.widgets.author_note_editor import AuthorNoteEditor

_SECTION_COLOR = '#8ecbff'
_MUTED_COLOR = '#8a8a8a'
_BODY_COLOR = '#d0d0d0'
_WARN_COLOR = '#ff9d5c'

_TAB_CONTEXT = 0
_TAB_NOTE = 1
_TAB_JAILBREAK = 2


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
    """The exact context a chat request will send, plus tabs that edit the
    chat's Author's Note and Jailbreak.

    The Context tab is read-only; the other two tabs hold the prompt knobs the
    toolbar used to expose as separate dialogs.  ``OK`` applies them (see
    :meth:`author_note` / :meth:`jailbreak`), ``Cancel`` discards them.  When a
    ``plan_factory`` is given, the Context tab is rebuilt from the dialog's
    current note/jailbreak on every switch to it, so the preview never shows a
    stale plan.
    """

    def __init__(
        self,
        plan: ContextPlan,
        context_size: int = 0,
        parent=None,
        author_note: dict | None = None,
        jailbreak: str = '',
        plan_factory: Callable[[dict, str], ContextPlan] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle('Context Inspector')
        self.resize(680, 600)
        self._plan = plan
        self._context_size = context_size
        self._plan_factory = plan_factory

        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)

        context_page = QWidget()
        context_layout = QVBoxLayout(context_page)
        self._summary = QLabel('')
        context_layout.addWidget(self._summary)
        self._view = QTextBrowser()
        self._view.setOpenExternalLinks(False)
        self._view.setStyleSheet(
            'QTextBrowser { background-color: #1e1e1e; border: 1px solid #3a3a3a; }'
        )
        context_layout.addWidget(self._view, 1)
        self._tabs.addTab(context_page, 'Context')

        self._note_editor = AuthorNoteEditor(author_note)
        self._tabs.addTab(self._note_editor, "Author's Note")

        jailbreak_page = QWidget()
        jailbreak_layout = QVBoxLayout(jailbreak_page)
        jailbreak_layout.setContentsMargins(0, 0, 0, 0)
        jailbreak_layout.addWidget(QLabel(
            'Your own trailing instructions for this chat, sent after the '
            "card's post-history instructions (depth 0)."
        ))
        self._jailbreak_edit = QPlainTextEdit(jailbreak or '')
        self._jailbreak_edit.setPlaceholderText('e.g. Always reply in character.')
        jailbreak_layout.addWidget(self._jailbreak_edit, 1)
        self._tabs.addTab(jailbreak_page, 'Jailbreak')

        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._note_editor.changed.connect(self._sync_tab_titles)
        self._jailbreak_edit.textChanged.connect(self._sync_tab_titles)

        self._apply_plan(plan)
        self._sync_tab_titles()

        buttons = QHBoxLayout()
        copy_btn = QPushButton('Copy')
        copy_btn.setToolTip('Copy the Context tab to the clipboard.')
        copy_btn.clicked.connect(self._copy_plan)
        buttons.addWidget(copy_btn)
        buttons.addStretch()
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        buttons.addWidget(button_box)
        layout.addLayout(buttons)

    def author_note(self) -> dict:
        """The edited Author's Note as a normalised ``{text, depth, role}`` dict."""
        return self._note_editor.note()

    def jailbreak(self) -> str:
        """The edited jailbreak (user post-history instructions) text."""
        return self._jailbreak_edit.toPlainText().strip()

    def plan(self) -> ContextPlan:
        """The currently displayed context plan."""
        return self._plan

    def _on_tab_changed(self, index: int) -> None:
        if index != _TAB_CONTEXT or self._plan_factory is None:
            return
        self._apply_plan(self._plan_factory(self.author_note(), self.jailbreak()))

    def _apply_plan(self, plan: ContextPlan) -> None:
        self._plan = plan
        over = bool(self._context_size) and plan.total_tokens > self._context_size
        color = _WARN_COLOR if over else _MUTED_COLOR
        self._summary.setText(format_summary(plan, self._context_size))
        self._summary.setStyleSheet(f'color: {color};')
        self._view.setHtml(format_plan_html(plan))

    def _sync_tab_titles(self) -> None:
        has_note = bool(self.author_note()['text'])
        has_jb = bool(self._jailbreak_edit.toPlainText().strip())
        self._tabs.setTabText(_TAB_NOTE, "Author's Note*" if has_note else "Author's Note")
        self._tabs.setTabText(_TAB_JAILBREAK, 'Jailbreak*' if has_jb else 'Jailbreak')

    def _copy_plan(self) -> None:
        text = format_plan_text(self._plan)
        if text.strip():
            QGuiApplication.clipboard().setText(text)
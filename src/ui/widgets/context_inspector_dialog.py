"""Dialog showing the exact prompt a chat request will send.

The dialog has three tabs:

* **Prompt** - the prompt preview: every block with its token cost, the
  context-pressure meter and the payload that would be posted.
* **Author's Note** - the chat's Author's Note (depth, role, text).
* **Jailbreak** - the user's own trailing instructions.

``OK`` applies the note and the jailbreak, ``Cancel`` discards them.  The
Prompt tab is rebuilt from the dialog's current note/jailbreak whenever it
is shown, so the preview never shows a stale prompt.
"""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui.spell_check import SpellCheckPlainTextEdit
from src.ui.widgets.author_note_editor import AuthorNoteEditor
from src.ui.widgets.prompt_preview_widget import (
    PromptPreviewWidget,
    format_preview_text,
)

_TAB_PROMPT = 0
_TAB_NOTE = 1
_TAB_JAILBREAK = 2


class ContextInspectorDialog(QDialog):
    """The exact prompt a chat request will send, plus tabs that edit the
    chat's Author's Note and Jailbreak.

    The Prompt tab is read-only; the other two tabs hold the prompt knobs the
    toolbar used to expose as separate dialogs.  ``OK`` applies them (see
    :meth:`author_note` / :meth:`jailbreak`), ``Cancel`` discards them.  When a
    ``preview_factory`` is given, the Prompt tab is rebuilt from the dialog's
    current note/jailbreak on every switch to it, so the preview never shows a
    stale prompt.
    """

    def __init__(
        self,
        preview,
        context_size: int = 0,
        parent=None,
        author_note: dict | None = None,
        jailbreak: str = '',
        preview_factory: Callable[[dict, str], object] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle('Context Inspector')
        self.resize(760, 620)
        self._preview = preview
        self._context_size = context_size
        self._preview_factory = preview_factory

        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)

        self._preview_widget = PromptPreviewWidget()
        self._tabs.addTab(self._preview_widget, 'Prompt')

        self._note_editor = AuthorNoteEditor(author_note)
        self._tabs.addTab(self._note_editor, "Author's Note")

        jailbreak_page = QWidget()
        jailbreak_layout = QVBoxLayout(jailbreak_page)
        jailbreak_layout.setContentsMargins(0, 0, 0, 0)
        jailbreak_layout.addWidget(QLabel(
            'Your own trailing instructions for this chat, sent after the '
            "card's post-history instructions (depth 0)."
        ))
        self._jailbreak_edit = SpellCheckPlainTextEdit(jailbreak or '')
        self._jailbreak_edit.setPlaceholderText('e.g. Always reply in character.')
        jailbreak_layout.addWidget(self._jailbreak_edit, 1)
        self._tabs.addTab(jailbreak_page, 'Jailbreak')

        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._note_editor.changed.connect(self._on_note_changed)
        self._jailbreak_edit.textChanged.connect(self._on_jailbreak_changed)

        # The prompt preview follows the edited note and jailbreak, so the
        # user sees the effect of an edit without leaving the tab.  The
        # rebuild is debounced: it re-tokenizes the whole prompt.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self._refresh_preview)

        self._apply_preview(preview)
        self._sync_tab_titles()

        buttons = QHBoxLayout()
        copy_btn = QPushButton('Copy')
        copy_btn.setToolTip('Copy the prompt preview to the clipboard.')
        copy_btn.clicked.connect(self._copy_preview)
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

    def preview(self):
        """The currently displayed prompt preview."""
        return self._preview

    def _on_tab_changed(self, index: int) -> None:
        if index != _TAB_PROMPT or self._preview_factory is None:
            return
        self._refresh_timer.stop()
        self._apply_preview(self._preview_factory(self.author_note(), self.jailbreak()))

    def _on_note_changed(self) -> None:
        self._sync_tab_titles()
        self._schedule_refresh()

    def _on_jailbreak_changed(self) -> None:
        self._sync_tab_titles()
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        """Rebuild the prompt preview after the note / jailbreak changed."""
        if self._preview_factory is not None:
            self._refresh_timer.start()

    def _refresh_preview(self) -> None:
        if self._preview_factory is None:
            return
        self._apply_preview(self._preview_factory(self.author_note(), self.jailbreak()))

    def _apply_preview(self, preview) -> None:
        self._preview = preview
        self._preview_widget.set_preview(preview)

    def _sync_tab_titles(self) -> None:
        has_note = bool(self.author_note()['text'])
        has_jb = bool(self._jailbreak_edit.toPlainText().strip())
        self._tabs.setTabText(_TAB_NOTE, "Author's Note*" if has_note else "Author's Note")
        self._tabs.setTabText(_TAB_JAILBREAK, 'Jailbreak*' if has_jb else 'Jailbreak')

    def _copy_preview(self) -> None:
        text = format_preview_text(self._preview)
        if text.strip():
            QGuiApplication.clipboard().setText(text)

from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src import ai_prompts
from src.card_models import CharacterCard

logger = logging.getLogger(__name__)

# Sample fields used for the live preview substitution.
_SAMPLE_CARD = CharacterCard(
    name='Aria the Mage',
    description='A young elven mage with silver hair.',
    personality='Curious, bookish, slightly shy.',
    scenario='Studying at the Arcane Academy.',
    first_mes='*Looks up from a tome* Oh, hello there!',
)


class _PromptTab(QWidget):
    """A single prompt-editing tab with system/user templates + live preview."""

    def __init__(self, title: str, keys: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self._keys = keys
        self._title = title
        layout = QVBoxLayout(self)

        self._edits: dict[str, QPlainTextEdit] = {}
        for key in keys:
            label_text = 'System prompt:' if key.endswith('_system') else 'User prompt:'
            layout.addWidget(self._make_label(label_text))
            edit = QPlainTextEdit()
            edit.setPlainText(ai_prompts.load_prompt(key))
            edit.setMinimumHeight(120)
            layout.addWidget(edit)
            self._edits[key] = edit

        btn_row = QHBoxLayout()
        reset_btn = QPushButton('Reset to Default')
        reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(reset_btn)
        preview_btn = QPushButton('Preview')
        preview_btn.clicked.connect(self._preview)
        btn_row.addWidget(preview_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._preview_label = QLabel('')
        self._preview_label.setWordWrap(True)
        self._preview_label.setStyleSheet(
            'color: #b0b0b0; background-color: #222; '
            'border-radius: 4px; padding: 6px;'
        )
        layout.addWidget(self._preview_label)

        layout.addStretch()

    def _make_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet('font-weight: bold; color: #ccc; margin-top: 6px;')
        return lbl

    def _reset(self) -> None:
        for key in self._keys:
            default = ai_prompts.reset_prompt(key)
            self._edits[key].setPlainText(default)

    def _preview(self) -> None:
        templates = self.get_templates()
        parts = []
        if 'tags_system' in templates:
            sys_p, user_p = ai_prompts.build_tags_prompts(_SAMPLE_CARD, templates)
            parts.append(f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}')
        elif 'missing_tags_system' in templates:
            sys_p, user_p = ai_prompts.build_missing_tags_prompts(_SAMPLE_CARD, templates)
            parts.append(f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}')
        elif 'summary_system' in templates:
            sys_p, user_p = ai_prompts.build_summary_prompts(_SAMPLE_CARD, templates)
            parts.append(f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}')
        elif 'alt_greetings_system' in templates:
            sys_p, user_p = ai_prompts.build_alt_greetings_prompts(_SAMPLE_CARD, templates)
            parts.append(f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}')
        elif 'fill_system' in templates:
            sys_p, user_p = ai_prompts.build_fill_prompts(
                _SAMPLE_CARD, ['description', 'personality', 'tags'], templates,
            )
            parts.append(f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}')
        elif 'wizard_system' in templates:
            sys_p, user_p = ai_prompts.build_wizard_question_prompts(
                "the character's appearance", 'Name: Aria the Mage', templates,
            )
            parts.append(f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}')
        elif 'lorebook_system' in templates:
            sys_p, user_p = ai_prompts.build_lorebook_prompts(
                'A fantasy kingdom with political intrigue',
                card=_SAMPLE_CARD, count='8', templates=templates,
            )
            parts.append(f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}')
        elif 'lorebook_entry_system' in templates:
            sys_p, user_p = ai_prompts.build_lorebook_entry_prompts(
                'The Arcane Academy', ['arcane academy', 'academy'],
                card=_SAMPLE_CARD,
                book_description='Places and factions of the kingdom',
                templates=templates,
            )
            parts.append(f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}')
        elif 'chat_system' in templates:
            sys_p = ai_prompts.build_chat_system(_SAMPLE_CARD, templates)
            parts.append(f'--- System ---\n{sys_p}')
        self._preview_label.setText('\n\n'.join(parts))

    def get_templates(self) -> dict[str, str]:
        return {key: edit.toPlainText() for key, edit in self._edits.items()}


class PromptSettingsDialog(QDialog):
    """Dialog for customizing the AI prompt templates.

    Tabs (Tags, Summary, Fill Fields, Wizard, Chat); each has a system/user
    template editor with reset-to-default and live preview.  Changes are saved
    to QSettings on accept.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('AI Prompt Settings')
        self.setMinimumWidth(560)
        self.setMinimumHeight(480)

        layout = QVBoxLayout(self)

        self._tabs = QTabWidget()
        self._tab_tags = _PromptTab('Tags', ['tags_system', 'tags_user'])
        self._tab_missing = _PromptTab('Missing Tags', ['missing_tags_system', 'missing_tags_user'])
        self._tab_summary = _PromptTab('Summary', ['summary_system', 'summary_user'])
        self._tab_alt_greetings = _PromptTab('Alt Greetings', ['alt_greetings_system', 'alt_greetings_user'])
        self._tab_fill = _PromptTab('Fill Fields', ['fill_system', 'fill_user'])
        self._tab_wizard = _PromptTab('Wizard', ['wizard_system', 'wizard_user'])
        self._tab_chat = _PromptTab('Chat', ['chat_system'])
        self._tab_lorebook = _PromptTab('Lorebook', ['lorebook_system', 'lorebook_user'])
        self._tab_lore_entry = _PromptTab(
            'Lorebook Entry', ['lorebook_entry_system', 'lorebook_entry_user'],
        )
        self._tabs.addTab(self._tab_tags, 'Tags')
        self._tabs.addTab(self._tab_missing, 'Missing Tags')
        self._tabs.addTab(self._tab_summary, 'Summary')
        self._tabs.addTab(self._tab_alt_greetings, 'Alt Greetings')
        self._tabs.addTab(self._tab_fill, 'Fill Fields')
        self._tabs.addTab(self._tab_wizard, 'Wizard')
        self._tabs.addTab(self._tab_chat, 'Chat')
        self._tabs.addTab(self._tab_lorebook, 'Lorebook')
        self._tabs.addTab(self._tab_lore_entry, 'Lorebook Entry')
        layout.addWidget(self._tabs)

        info = QLabel(
            'Placeholders like {name}, {description}, {personality}, {scenario}, '
            '{first_mes}, {extra}, {existing_tags}, {topic}, {context} are substituted '
            'at generation time. Unknown placeholders resolve to empty strings.'
        )
        info.setWordWrap(True)
        info.setStyleSheet('color: #aaa;')
        layout.addWidget(info)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
        )
        btn_box.accepted.connect(self._save)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _save(self) -> None:
        for tab in (self._tab_tags, self._tab_missing, self._tab_summary,
                    self._tab_alt_greetings,
                    self._tab_fill, self._tab_wizard, self._tab_chat,
                    self._tab_lorebook, self._tab_lore_entry):
            for key, value in tab.get_templates().items():
                ai_prompts.save_prompt(key, value)
        logger.info("AI prompt templates updated")
        self.accept()

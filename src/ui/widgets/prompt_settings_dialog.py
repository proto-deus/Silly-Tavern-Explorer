"""The prompt-template editor used by Settings > Prompts.

:class:`_PromptTab` renders one feature's system/user templates with a
live preview, a token cost and a placeholder check.  The Settings panel
puts one of these in a stack widget and switches between them with a
combo box.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src import ai_prompts
from src.card_models import CharacterCard
from src.ui.spell_check import SpellCheckPlainTextEdit

# Sample fields used for the live preview substitution.
_SAMPLE_CARD = CharacterCard(
    name='Aria the Mage',
    description='A young elven mage with silver hair.',
    personality='Curious, bookish, slightly shy.',
    scenario='Studying at the Arcane Academy.',
    first_mes='*Looks up from a tome* Oh, hello there!',
)

#: The names the preview resolves ``{{user}}`` / ``{{char}}`` with.  The
#: Impersonate and Continue prompts are resolved by the macro substitution
#: (not by ``str.format``), so the preview has to do the same - otherwise
#: it shows something different from what the chat actually sends.
_PREVIEW_USER_NAME = 'User'
_PREVIEW_CHAR_NAME = 'Aria'


def _preview_mode_prompt(template: str) -> str:
    """Render an Impersonate/Continue prompt the way the chat renders it."""
    from src.chat_builder import substitute_macros

    return substitute_macros(
        template, _PREVIEW_USER_NAME, _PREVIEW_CHAR_NAME,
    )


class _PromptTab(QWidget):
    """A single prompt-editing tab with system/user templates + live preview.

    The preview refreshes while the user types, the token cost of the
    rendered prompt is shown next to it, and a template that references a
    placeholder it does not support is flagged: an unknown placeholder
    resolves to an empty string, so a typo would otherwise silently empty
    a field of the generated prompt.
    """

    def __init__(self, title: str, keys: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self._keys = keys
        self._title = title
        layout = QVBoxLayout(self)

        self._edits: dict[str, QPlainTextEdit] = {}
        self._hints: dict[str, QLabel] = {}
        for key in keys:
            label_text = 'System prompt:' if key.endswith('_system') else 'User prompt:'
            layout.addWidget(self._make_label(label_text))
            edit = SpellCheckPlainTextEdit()
            edit.setPlainText(ai_prompts.load_prompt(key))
            edit.setMinimumHeight(110)
            edit.textChanged.connect(self._refresh)
            layout.addWidget(edit)
            self._edits[key] = edit

            hint = QLabel(self._placeholder_hint(key))
            hint.setWordWrap(True)
            hint.setStyleSheet('color: #999;')
            layout.addWidget(hint)
            self._hints[key] = hint

        self._warning = QLabel('')
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet('color: #e0a33a;')
        self._warning.setVisible(False)
        layout.addWidget(self._warning)

        btn_row = QHBoxLayout()
        reset_btn = QPushButton('Reset to Default')
        reset_btn.setToolTip(
            'Restore the built-in template.\n'
            'The change is only written when the settings are saved.'
        )
        reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._preview_label = QLabel('')
        self._preview_label.setWordWrap(True)
        self._preview_label.setTextFormat(Qt.TextFormat.PlainText)
        self._preview_label.setStyleSheet(
            'color: #b0b0b0; background-color: #222; '
            'border-radius: 4px; padding: 6px;'
        )
        self._preview_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse,
        )
        layout.addWidget(self._preview_label, 1)

        self._refresh()

    def _make_label(self, label: str) -> QLabel:
        lbl = QLabel(label)
        lbl.setStyleSheet('font-weight: bold; color: #ccc; margin-top: 6px;')
        return lbl

    def _placeholder_hint(self, key: str) -> str:
        allowed = ai_prompts.PLACEHOLDERS.get(key, ())
        if not allowed:
            return 'This template has no placeholders.'
        return 'Placeholders: ' + ', '.join('{' + p + '}' for p in allowed)

    def _reset(self) -> None:
        """Restore the built-in templates in the editors.

        Nothing is written to the settings here: the dialog saves every
        tab when it is accepted, so a reset followed by "Cancel" leaves
        the stored templates untouched.
        """
        for key in self._keys:
            self._edits[key].setPlainText(ai_prompts.default_prompt(key))
        self._refresh()

    def _refresh(self) -> None:
        """Validate the templates and rebuild the live preview."""
        self._check_placeholders()
        self._preview()

    def _check_placeholders(self) -> None:
        problems: list[str] = []
        for key, edit in self._edits.items():
            unknown = ai_prompts.unknown_placeholders(key, edit.toPlainText())
            hint = self._hints.get(key)
            if hint is not None:
                if unknown:
                    hint.setStyleSheet('color: #e0a33a;')
                    hint.setText(
                        'Unknown placeholder(s): '
                        + ', '.join('{' + p + '}' for p in unknown)
                        + ' - they resolve to an empty string.',
                    )
                else:
                    hint.setStyleSheet('color: #999;')
                    hint.setText(self._placeholder_hint(key))
            if unknown:
                problems.append(f'{key}: ' + ', '.join(unknown))
        if problems:
            self._warning.setText('Unknown placeholders: ' + '; '.join(problems))
            self._warning.setVisible(True)
        else:
            self._warning.setVisible(False)

    def _build_preview(self, templates: dict[str, str]) -> list[str]:
        """Render the templates with a sample card / conversation."""
        if 'tags_system' in templates:
            sys_p, user_p = ai_prompts.build_tags_prompts(_SAMPLE_CARD, templates)
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'missing_tags_system' in templates:
            sys_p, user_p = ai_prompts.build_missing_tags_prompts(_SAMPLE_CARD, templates)
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'summary_system' in templates:
            sys_p, user_p = ai_prompts.build_summary_prompts(_SAMPLE_CARD, templates)
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'alt_greetings_system' in templates:
            sys_p, user_p = ai_prompts.build_alt_greetings_prompts(_SAMPLE_CARD, templates)
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'fill_system' in templates:
            sys_p, user_p = ai_prompts.build_fill_prompts(
                _SAMPLE_CARD, ['description', 'personality', 'tags'], templates,
            )
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'wizard_system' in templates:
            sys_p, user_p = ai_prompts.build_wizard_question_prompts(
                "the character's appearance", 'Name: Aria the Mage', templates,
            )
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'lorebook_system' in templates:
            sys_p, user_p = ai_prompts.build_lorebook_prompts(
                'A fantasy kingdom with political intrigue',
                card=_SAMPLE_CARD, count='8', templates=templates,
            )
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'lorebook_entry_system' in templates:
            sys_p, user_p = ai_prompts.build_lorebook_entry_prompts(
                'The Arcane Academy', ['arcane academy', 'academy'],
                card=_SAMPLE_CARD,
                book_description='Places and factions of the kingdom',
                templates=templates,
            )
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'memory_summary_system' in templates:
            sys_p, user_p = ai_prompts.build_memory_summary_prompts(
                'User: Where are we going?\nAria: Behind the fold.', templates,
            )
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'attribute_update_system' in templates:
            sys_p, user_p = ai_prompts.build_attribute_update_prompts(
                [
                    {'name': 'Setting', 'value': 'the cartographer camp',
                     'description': 'Where the scene takes place.'},
                    {'name': 'Mood', 'value': 'focused'},
                ],
                'User: Where are we going?\nAria: Behind the fold.', templates,
            )
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'character_system' in templates:
            sys_p, user_p = ai_prompts.build_character_prompts(
                'a grumpy lighthouse keeper', 'include a friendly cat', templates,
            )
            return [f'--- System ---\n{sys_p}\n\n--- User ---\n{user_p}']
        if 'impersonate_user' in templates:
            user_p = _preview_mode_prompt(templates['impersonate_user'])
            return [f'--- User ---\n{user_p}']
        if 'continue_user' in templates:
            user_p = _preview_mode_prompt(templates['continue_user'])
            return [f'--- User ---\n{user_p}']
        if 'chat_system' in templates:
            sys_p = ai_prompts.build_chat_system(_SAMPLE_CARD, templates)
            return [f'--- System ---\n{sys_p}']
        return []

    def _preview(self) -> None:
        templates = self.get_templates()
        parts = self._build_preview(templates)
        text = '\n\n'.join(parts)
        if not text:
            self._preview_label.setText('(no preview for this template)')
            return
        tokens = 0
        try:
            from src.token_counter import count_tokens
            tokens = count_tokens(text)
        except Exception:
            tokens = 0
        cost = f'\n\n[~{tokens:,} tokens]' if tokens else ''
        self._preview_label.setText(text + cost)

    def get_templates(self) -> dict[str, str]:
        return {key: edit.toPlainText() for key, edit in self._edits.items()}

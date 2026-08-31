from __future__ import annotations

import logging
import json
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.card_models import CharacterCard
from src.card_parser import read_card_data, replace_card_image, write_chara_card_dual
from src.database import LibraryDatabase, sanitize_filename
from src.token_counter import count_card_tokens, count_tokens
from src.ui.widgets.collapsible_section import CollapsibleSection
from src.ui.widgets.tag_widget import TagWidget

logger = logging.getLogger(__name__)


class DirtyState:
    """Track whether the edit form has unsaved changes.

    Pure-Python helper (no Qt) so the guard logic can be unit-tested
    without a running Qt event loop.  The ``_loading`` flag suppresses
    dirty-marking while fields are being populated programmatically
    (e.g. during ``_load_card``).
    """

    def __init__(self) -> None:
        self._dirty: bool = False
        self._loading: bool = False

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def is_loading(self) -> bool:
        return self._loading

    def begin_load(self) -> None:
        """Suppress dirty-marking until ``end_load`` is called."""
        self._loading = True

    def end_load(self) -> None:
        """Stop suppressing and clear any dirty state from loading."""
        self._loading = False
        self._dirty = False

    def begin_update(self) -> None:
        """Suppress dirty-marking WITHOUT clearing existing dirty state.

        For programmatic field mutations that preserve user content (e.g.
        toggling the HTML preview).  Unlike ``end_load``, ``end_update``
        leaves the dirty flag exactly as it was.
        """
        self._loading = True

    def end_update(self) -> None:
        self._loading = False

    def mark_dirty(self) -> None:
        if not self._loading:
            self._dirty = True

    def clear(self) -> None:
        self._dirty = False



def assemble_edited_card(
    base: CharacterCard,
    *,
    name: str,
    description: str,
    personality: str,
    scenario: str,
    first_mes: str,
    mes_example: str,
    creator_notes: str,
    system_prompt: str,
    post_history_instructions: str,
    alternate_greetings: list[str],
    tags: list[str],
    creator: str,
    character_version: str,
    talkativeness: float,
    character_book: dict | None,
) -> CharacterCard:
    """Build the saved :class:`CharacterCard` from form fields over *base*.

    Carries runtime state that the form doesn't edit (``fav``, ``extensions``,
    ``spec``, ``spec_version``, ``create_date``) and — critically — the
    unmodeled V3 ``extra_data`` (nickname, assets, group_only_greetings,
    third-party keys) from *base*, so an edit/save round-trip never strips
    fields the app doesn't model.  Pure function so preservation can be
    unit-tested without Qt.
    """
    return CharacterCard(
        name=name,
        description=description,
        personality=personality,
        scenario=scenario,
        first_mes=first_mes,
        mes_example=mes_example,
        creator_notes=creator_notes,
        system_prompt=system_prompt,
        post_history_instructions=post_history_instructions,
        alternate_greetings=alternate_greetings,
        tags=tags,
        creator=creator,
        character_version=character_version,
        talkativeness=talkativeness,
        fav=base.fav,
        extensions=dict(base.extensions) if base.extensions else {},
        character_book=character_book,
        spec=base.spec,
        spec_version=base.spec_version,
        create_date=base.create_date,
        extra_data=dict(base.extra_data) if base.extra_data else {},
    )


def reconcile_form_tags(
    current_tags: list[str],
    before: set[str],
    after: set[str],
) -> list[str]:
    """Reconcile the edit form's tags with Tag Manager's library-wide changes.

    Comparison is case-insensitive (the DB stores normalized lowercase
    tags).  A tag is dropped only when it existed in the *before* snapshot
    but no longer exists in *after* — i.e. it was deleted or renamed away
    by a Tag Manager operation.  Tags absent from both snapshots are new /
    not-yet-saved entries on this form and are preserved verbatim, as is
    the original casing of every surviving tag.  Pure function so the
    reconciliation can be unit-tested without Qt.
    """
    before_n = {t.lower() for t in before}
    after_n = {t.lower() for t in after}
    result: list[str] = []
    seen: set[str] = set()
    for tag in current_tags:
        key = tag.strip().lower()
        if not key or key in seen:
            continue
        if key in before_n and key not in after_n:
            continue
        seen.add(key)
        result.append(tag)
    return result


class EditTab(QWidget):
    status_message = pyqtSignal(str, int)
    card_deleted = pyqtSignal(int)
    card_updated = pyqtSignal(int)
    card_added = pyqtSignal(int)
    settings_requested = pyqtSignal()

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._current_id: int | None = None
        # Id whose content the form currently displays. ``_current_id`` tracks
        # the sidebar/grid selection and may desynchronize from the form
        # (e.g. when a selection changes while another tab is frontmost);
        # save() refuses to write unless these match.
        self._form_loaded_for_id: int | None = None
        self._current_card: CharacterCard | None = None
        self._dirty_state = DirtyState()
        self._character_book_dict: dict | None = None
        self._html_preview_mode: bool = False
        self._html_sources: dict[int, str] = {}
        self._html_field_formats: dict[int, tuple[QTextCharFormat, QFont]] = {}
        self._html_fields: list[QTextEdit] = []
        self._sections: list[CollapsibleSection] = []
        self._token_timer = QTimer(self)
        self._token_timer.setSingleShot(True)
        self._token_timer.setInterval(300)
        self._token_timer.timeout.connect(self._do_update_token_count)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)

        header_container = QWidget()
        header_vlayout = QVBoxLayout(header_container)
        header_vlayout.setContentsMargins(0, 0, 0, 0)
        header_vlayout.setSpacing(0)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(8, 8, 20, 0)
        self._change_img_btn = QPushButton('Change Image')
        self._change_img_btn.setEnabled(False)
        self._change_img_btn.clicked.connect(self._change_image)
        header_row.addWidget(self._change_img_btn)
        self._open_folder_btn = QPushButton('Open Folder')
        self._open_folder_btn.setEnabled(False)
        self._open_folder_btn.clicked.connect(self._on_open_folder)
        header_row.addWidget(self._open_folder_btn)
        self._preview_html_btn = QPushButton('Preview HTML')
        self._preview_html_btn.setCheckable(True)
        self._preview_html_btn.setEnabled(False)
        self._preview_html_btn.toggled.connect(self._on_toggle_html_preview)
        header_row.addWidget(self._preview_html_btn)
        self._fav_btn = QPushButton('Favorite')
        self._fav_btn.setCheckable(True)
        self._fav_btn.setEnabled(False)
        self._fav_btn.clicked.connect(self._on_toggle_favorite)
        header_row.addWidget(self._fav_btn)
        header_row.addStretch()
        new_character_btn = QPushButton('New Character')
        new_character_btn.clicked.connect(self._on_new_character)
        header_row.addWidget(new_character_btn)
        settings_btn = QPushButton('Settings')
        settings_btn.clicked.connect(self._on_settings_requested)
        header_row.addWidget(settings_btn)
        header_vlayout.addLayout(header_row)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(8, 6, 20, 0)
        header = QLabel('Select a character to edit')
        header.setStyleSheet('font-weight: bold; color: #e0e0e0;')
        self._header = header
        title_row.addWidget(header)
        title_row.addStretch()
        header_vlayout.addLayout(title_row)

        token_row = QHBoxLayout()
        token_row.setContentsMargins(8, 0, 20, 4)
        self._token_label = QLabel('Tokens: 0 (permanent) / 0 (full)')
        self._token_label.setStyleSheet('color: #aaa;')
        token_row.addWidget(self._token_label)
        token_row.addStretch()
        header_vlayout.addLayout(token_row)

        right_layout.addWidget(header_container)

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        form_container = QWidget()
        form_layout = QVBoxLayout(form_container)
        form_layout.setContentsMargins(8, 8, 20, 8)

        self._name_token_label = QLabel('Name: (0 Tokens)')
        self._name_token_label.setStyleSheet('font-weight: bold; color: #ccc; margin-top: 6px;')
        form_layout.addWidget(self._name_token_label)
        self._name_edit = QLineEdit()
        self._name_edit.textChanged.connect(self._update_token_count)
        form_layout.addWidget(self._name_edit)

        self._desc_edit = QTextEdit()
        self._desc_edit.setMaximumHeight(300)
        self._desc_edit.textChanged.connect(self._update_token_count)
        self._desc_section = CollapsibleSection('Description:', self._desc_edit)
        form_layout.addWidget(self._desc_section)
        self._sections.append(self._desc_section)

        self._pers_edit = QTextEdit()
        self._pers_edit.setMaximumHeight(300)
        self._pers_edit.textChanged.connect(self._update_token_count)
        self._pers_section = CollapsibleSection('Personality:', self._pers_edit)
        form_layout.addWidget(self._pers_section)
        self._sections.append(self._pers_section)

        self._scen_edit = QTextEdit()
        self._scen_edit.setMaximumHeight(300)
        self._scen_edit.textChanged.connect(self._update_token_count)
        self._scen_section = CollapsibleSection('Scenario:', self._scen_edit)
        form_layout.addWidget(self._scen_section)
        self._sections.append(self._scen_section)

        self._sys_edit = QTextEdit()
        self._sys_edit.setMaximumHeight(240)
        self._sys_edit.textChanged.connect(self._update_token_count)
        self._sys_section = CollapsibleSection('System Prompt:', self._sys_edit)
        form_layout.addWidget(self._sys_section)
        self._sections.append(self._sys_section)

        self._phi_edit = QTextEdit()
        self._phi_edit.setMaximumHeight(200)
        self._phi_edit.textChanged.connect(self._update_token_count)
        self._phi_section = CollapsibleSection('Post-History Instructions:', self._phi_edit)
        form_layout.addWidget(self._phi_section)
        self._sections.append(self._phi_section)

        self._first_edit = QTextEdit()
        self._first_edit.setMaximumHeight(240)
        self._first_edit.textChanged.connect(self._update_token_count)
        self._first_section = CollapsibleSection('First Message:', self._first_edit)
        form_layout.addWidget(self._first_section)
        self._sections.append(self._first_section)

        self._example_edit = QTextEdit()
        self._example_edit.setMaximumHeight(240)
        self._example_edit.textChanged.connect(self._update_token_count)
        self._example_section = CollapsibleSection('Example Messages:', self._example_edit)
        form_layout.addWidget(self._example_section)
        self._sections.append(self._example_section)

        self._notes_edit = QTextEdit()
        self._notes_edit.setMaximumHeight(160)
        self._notes_edit.textChanged.connect(self._update_token_count)
        self._notes_section = CollapsibleSection('Creator Notes:', self._notes_edit)
        form_layout.addWidget(self._notes_section)
        self._sections.append(self._notes_section)

        # Private user notes: DB-only metadata, never written to the PNG.
        user_notes_container = QWidget()
        user_notes_v = QVBoxLayout(user_notes_container)
        user_notes_v.setContentsMargins(0, 0, 0, 0)
        self._user_notes_edit = QTextEdit()
        self._user_notes_edit.setMaximumHeight(120)
        self._user_notes_edit.setPlaceholderText(
            'Private notes — stored locally, never written to the card file'
        )
        user_notes_v.addWidget(self._user_notes_edit)
        save_notes_btn = QPushButton('Save Notes')
        save_notes_btn.clicked.connect(self._save_user_notes)
        user_notes_v.addWidget(save_notes_btn)
        self._user_notes_section = CollapsibleSection('My Notes:', user_notes_container)
        form_layout.addWidget(self._user_notes_section)
        self._sections.append(self._user_notes_section)

        form_layout.addWidget(self._make_label('Tags:'))
        self._tag_widget = TagWidget(editable=True)
        form_layout.addWidget(self._tag_widget)

        tag_input_row = QHBoxLayout()
        self._tag_input = QLineEdit()
        self._tag_input.setPlaceholderText('Add tag...')
        self._tag_input.returnPressed.connect(self._add_tag)
        tag_input_row.addWidget(self._tag_input)
        add_tag_btn = QPushButton('Add')
        add_tag_btn.clicked.connect(self._add_tag)
        tag_input_row.addWidget(add_tag_btn)
        self._manage_tags_btn = QPushButton('Manage Tags...')
        self._manage_tags_btn.clicked.connect(self._open_tag_manager)
        tag_input_row.addWidget(self._manage_tags_btn)
        form_layout.addLayout(tag_input_row)

        # Tag autocomplete: populated from the DB and refreshed whenever the
        # library changes (the main window wires library_changed -> refresh).
        from PyQt6.QtCore import QStringListModel
        from PyQt6.QtWidgets import QCompleter
        self._tag_completer_model = QStringListModel()
        self._tag_completer = QCompleter()
        self._tag_completer.setModel(self._tag_completer_model)
        self._tag_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._tag_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._tag_input.setCompleter(self._tag_completer)
        self.refresh_tag_completer()

        self._alt_greeting_edits: list[QTextEdit] = []
        self._alt_greeting_rows: list[QWidget] = []
        self._alt_greeting_container = QWidget()
        self._alt_greeting_layout = QVBoxLayout(self._alt_greeting_container)
        self._alt_greeting_layout.setContentsMargins(0, 0, 0, 0)
        self._alt_greeting_layout.setSpacing(0)
        alt_add_btn = QPushButton('Add')
        alt_add_btn.clicked.connect(self._add_greeting)
        self._alt_greeting_layout.addWidget(alt_add_btn)
        self._alt_greeting_section = CollapsibleSection('Alternate Greetings:', self._alt_greeting_container)
        self._alt_greeting_section.set_expanded(False)
        form_layout.addWidget(self._alt_greeting_section)
        self._sections.append(self._alt_greeting_section)

        self._char_book_btn = QPushButton('Character Book...')
        self._char_book_btn.setEnabled(False)
        self._char_book_btn.clicked.connect(self._edit_character_book)
        form_layout.addWidget(self._char_book_btn)

        row = QHBoxLayout()
        row.addWidget(self._make_label('Creator:'))
        self._creator_edit = QLineEdit()
        row.addWidget(self._creator_edit)
        row.addWidget(self._make_label('Version:'))
        self._version_edit = QLineEdit()
        self._version_edit.setMaximumWidth(100)
        row.addWidget(self._version_edit)
        self._extensions_btn = QPushButton('Extensions...')
        self._extensions_btn.setEnabled(False)
        self._extensions_btn.clicked.connect(self._edit_extensions)
        row.addWidget(self._extensions_btn)
        form_layout.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(self._make_label('Talkativeness:'))
        self._talk_spin = QDoubleSpinBox()
        self._talk_spin.setRange(0.0, 1.0)
        self._talk_spin.setSingleStep(0.1)
        self._talk_spin.setValue(0.5)
        row2.addWidget(self._talk_spin)
        row2.addStretch()
        form_layout.addLayout(row2)

        form_layout.addStretch()

        right_scroll.setWidget(form_container)
        right_layout.addWidget(right_scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(8, 4, 8, 8)
        save_btn = QPushButton('Save')
        save_btn.clicked.connect(self.save)
        btn_row.addWidget(save_btn)
        export_png_btn = QPushButton('Export as PNG')
        export_png_btn.clicked.connect(self._export_png)
        btn_row.addWidget(export_png_btn)
        export_json_btn = QPushButton('Export as JSON')
        export_json_btn.clicked.connect(self._export_json)
        btn_row.addWidget(export_json_btn)
        revert_btn = QPushButton('Revert')
        revert_btn.clicked.connect(self.revert)
        btn_row.addWidget(revert_btn)
        delete_btn = QPushButton('Delete')
        delete_btn.clicked.connect(self.delete_card)
        btn_row.addWidget(delete_btn)
        btn_row.addStretch()
        collapse_all_btn = QPushButton('Collapse All')
        collapse_all_btn.clicked.connect(self._collapse_all_sections)
        btn_row.addWidget(collapse_all_btn)
        expand_all_btn = QPushButton('Expand All')
        expand_all_btn.clicked.connect(self._expand_all_sections)
        btn_row.addWidget(expand_all_btn)
        right_layout.addLayout(btn_row)

        layout.addWidget(right_container)

        self._html_fields = [
            self._desc_edit, self._pers_edit, self._scen_edit,
            self._sys_edit, self._phi_edit, self._first_edit,
            self._example_edit, self._notes_edit,
        ]

        self._connect_dirty_signals()

    def _connect_dirty_signals(self) -> None:
        """Wire all edit fields so user input marks the form dirty."""
        for w in (self._name_edit, self._creator_edit, self._version_edit):
            w.textChanged.connect(self._mark_dirty)
        for w in (self._desc_edit, self._pers_edit, self._scen_edit,
                  self._sys_edit, self._phi_edit, self._first_edit,
                  self._example_edit, self._notes_edit):
            w.textChanged.connect(self._mark_dirty)
        self._talk_spin.valueChanged.connect(self._mark_dirty)
        self._tag_widget.tags_changed.connect(self._mark_dirty)

    def _mark_dirty(self, *args) -> None:
        self._dirty_state.mark_dirty()

    def _on_settings_requested(self) -> None:
        self.settings_requested.emit()

    def _on_new_character(self) -> None:
        from src.ui.widgets.new_character_dialog import NewCharacterDialog
        dlg = NewCharacterDialog(self.db, self)
        dlg.card_added.connect(self.card_added)
        dlg.exec()

    def _make_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet('font-size: 12px; font-weight: bold; color: #ccc; margin-top: 6px;')
        return lbl

    def _collapse_all_sections(self) -> None:
        for section in self._sections:
            section.set_expanded(False)

    def _expand_all_sections(self) -> None:
        for section in self._sections:
            section.set_expanded(True)

    def _on_toggle_html_preview(self, checked: bool) -> None:
        """Toggle between editable source view and read-only rendered HTML.

        In preview mode the raw HTML source is stored internally so it can be
        restored exactly when switching back — ``QTextEdit.toPlainText()``
        would strip tags from rendered HTML, so we never rely on it while
        in preview mode.
        """
        self._dirty_state.begin_update()
        try:
            if checked:
                self._html_sources = {}
                self._html_field_formats = {}
                for field in self._html_fields:
                    source = field.toPlainText()
                    self._html_sources[id(field)] = source
                    # Snapshot the pristine plain-text formatting so it can be
                    # fully restored when leaving preview (setHtml can leave
                    # stale colours/fonts behind; see _restore_plain_text).
                    self._html_field_formats[id(field)] = (
                        field.currentCharFormat(),
                        field.document().defaultFont(),
                    )
                    field.setHtml(source if source.strip() else '')
                    field.setReadOnly(True)
                self._html_preview_mode = True
            else:
                for field in self._html_fields:
                    self._restore_plain_text(field)
                self._html_preview_mode = False
        finally:
            # Use end_update (not end_load): the toggle preserves content, so
            # it must not clear the unsaved-changes flag.
            self._dirty_state.end_update()

    def _restore_plain_text(self, field: QTextEdit) -> None:
        """Return *field* from preview mode to its pre-preview state.

        ``QTextEdit.setHtml()`` can permanently alter the widget's formatting
        defaults — the document's default font and the character formats
        carried by its blocks and cursor.  A subsequent ``clear()`` +
        ``setPlainText()`` does not undo that on every Qt build, so leftover
        text colour/font from the rendered HTML bled into the plain source
        view.  Re-applying the snapshot taken when preview was enabled
        guarantees the field looks exactly as it did before previewing.
        """
        source = self._html_sources.pop(id(field), '')
        saved = self._html_field_formats.pop(id(field), None)
        fmt: QTextCharFormat | None = None
        default_font: QFont | None = None
        if saved is not None:
            fmt, default_font = saved
        field.clear()
        if default_font is not None:
            field.document().setDefaultFont(default_font)
        field.setPlainText(source)
        if fmt is not None:
            cursor = field.textCursor()
            cursor.select(QTextCursor.SelectionType.Document)
            # An empty/default char format strips any explicit colour or font
            # the parsed HTML left behind; text falls back to the palette.
            cursor.setCharFormat(fmt)
            field.setCurrentCharFormat(fmt)
            cursor.clearSelection()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            field.setTextCursor(cursor)
        field.setReadOnly(False)

    def _set_field_text(self, field: QTextEdit, text: str) -> None:
        """Set a text field's content, respecting the current preview mode."""
        if self._html_preview_mode:
            self._html_sources[id(field)] = text
            field.setHtml(text if text.strip() else '')
        else:
            field.setPlainText(text)

    def _get_field_text(self, field: QTextEdit) -> str:
        """Get a text field's raw source, respecting the current preview mode."""
        if self._html_preview_mode:
            return self._html_sources.get(id(field), '')
        return field.toPlainText()

    def load_card_by_id(self, char_id: int) -> None:
        self._current_id = char_id
        self._load_card()

    def set_selected_id(self, char_id: int) -> None:
        """Set the selected character id without loading the card (for cross-tab sync)."""
        self._current_id = char_id

    def _load_card(self) -> None:
        if self._current_id is None:
            return
        entry = self.db.get_by_id(self._current_id)
        if not entry:
            self._form_loaded_for_id = None
            return

        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            QMessageBox.warning(self, 'Error', 'Source file not found.')
            self._form_loaded_for_id = None
            return

        raw = read_card_data(source)
        if not raw:
            QMessageBox.warning(self, 'Error', 'Could not read character card data.')
            self._form_loaded_for_id = None
            return

        self._dirty_state.begin_load()
        try:
            card = CharacterCard.from_spec_dict(raw, source)
            card.token_count = count_card_tokens(card)
            self._current_card = card
            self._form_loaded_for_id = self._current_id

            self._header.setText(f'Editing: {card.name}')
            self._name_edit.setText(card.name)
            self._set_field_text(self._desc_edit, card.description)
            self._set_field_text(self._pers_edit, card.personality)
            self._set_field_text(self._scen_edit, card.scenario)
            self._set_field_text(self._sys_edit, card.system_prompt)
            self._set_field_text(self._phi_edit, card.post_history_instructions)
            self._set_field_text(self._first_edit, card.first_mes)
            self._set_field_text(self._example_edit, card.mes_example)
            self._set_field_text(self._notes_edit, card.creator_notes)
            self._tag_widget.set_tags(card.tags)
            self._rebuild_alt_greetings(card.alternate_greetings)
            self._creator_edit.setText(card.creator)
            self._version_edit.setText(card.character_version)
            self._talk_spin.setValue(card.talkativeness)
            self._set_field_text(self._user_notes_edit, entry.get('user_notes') or '')
            self._change_img_btn.setEnabled(True)
            self._open_folder_btn.setEnabled(True)
            self._preview_html_btn.setEnabled(True)
            # The DB flag is the user-facing source of truth for favorites
            # (library/sidebar toggles update it directly). Mirror it onto
            # both the button and the in-memory card so a later save() can
            # never revert a favorite toggled elsewhere.
            db_favorite = bool(entry.get('is_favorite'))
            self._fav_btn.setEnabled(True)
            self._fav_btn.setChecked(db_favorite)
            card.fav = db_favorite
            self._char_book_btn.setEnabled(True)
            self._extensions_btn.setEnabled(True)
            self._character_book_dict = card.character_book if isinstance(card.character_book, dict) else None
            # Compute token counts directly from the card data we already have
            # rather than triggering _update_token_count which re-gathers and re-encodes.
            self._update_token_counts_from_card(card)
        finally:
            self._dirty_state.end_load()

    def _gather_card(self) -> CharacterCard:
        base = self._current_card if self._current_card else CharacterCard()
        alt_greetings = self._collect_alt_greetings()
        # assemble_edited_card carries fav/extensions/spec/extra_data over
        # from *base* so unmodeled V3 fields survive the save.
        return assemble_edited_card(
            base,
            name=self._name_edit.text().strip(),
            description=self._get_field_text(self._desc_edit).strip(),
            personality=self._get_field_text(self._pers_edit).strip(),
            scenario=self._get_field_text(self._scen_edit).strip(),
            first_mes=self._get_field_text(self._first_edit).strip(),
            mes_example=self._get_field_text(self._example_edit).strip(),
            creator_notes=self._get_field_text(self._notes_edit).strip(),
            system_prompt=self._get_field_text(self._sys_edit).strip(),
            post_history_instructions=self._get_field_text(self._phi_edit).strip(),
            alternate_greetings=alt_greetings,
            tags=self._tag_widget.get_tags(),
            creator=self._creator_edit.text().strip(),
            character_version=self._version_edit.text().strip(),
            talkativeness=self._talk_spin.value(),
            character_book=self._character_book_dict,
        )

    def _update_token_counts_from_card(self, card: CharacterCard) -> None:
        """Set all token count labels directly from a CharacterCard.

        Avoids re-gathering and re-encoding fields that were just loaded.
        """
        field_tokens = {
            'name': count_tokens(card.name),
            'desc': count_tokens(card.description),
            'pers': count_tokens(card.personality),
            'scen': count_tokens(card.scenario),
            'sys': count_tokens(card.system_prompt),
            'phi': count_tokens(card.post_history_instructions),
            'first': count_tokens(card.first_mes),
            'example': count_tokens(card.mes_example),
            'notes': count_tokens(card.creator_notes),
        }
        alt_tokens = sum(count_tokens(g) for g in card.alternate_greetings)
        perm = field_tokens['name'] + field_tokens['desc'] + field_tokens['pers'] + field_tokens['scen']
        full = perm + field_tokens['first'] + field_tokens['example'] + field_tokens['sys'] + field_tokens['phi']
        self._token_label.setText(f'Tokens: {perm:,} (permanent) / {full:,} (full)')
        self._name_token_label.setText(f'Name: ({field_tokens["name"]:,} Tokens)')
        self._desc_section.set_token_count(field_tokens['desc'])
        self._pers_section.set_token_count(field_tokens['pers'])
        self._scen_section.set_token_count(field_tokens['scen'])
        self._sys_section.set_token_count(field_tokens['sys'])
        self._phi_section.set_token_count(field_tokens['phi'])
        self._first_section.set_token_count(field_tokens['first'])
        self._example_section.set_token_count(field_tokens['example'])
        self._notes_section.set_token_count(field_tokens['notes'])
        self._alt_greeting_section.set_token_count(alt_tokens)

    def _collect_alt_greetings(self) -> list[str]:
        greetings = []
        for edit in self._alt_greeting_edits:
            text = edit.toPlainText().strip()
            if text:
                greetings.append(text)
        return greetings

    def _rebuild_alt_greetings(self, greetings: list[str] | None = None) -> None:
        texts = greetings if greetings is not None else [e.toPlainText() for e in self._alt_greeting_edits]
        for edit in self._alt_greeting_edits:
            edit.textChanged.disconnect(self._mark_dirty)
            edit.setParent(None)
            edit.deleteLater()
        for row in self._alt_greeting_rows:
            row.setParent(None)
            row.deleteLater()
        self._alt_greeting_edits.clear()
        self._alt_greeting_rows.clear()
        for text in texts:
            self._create_greeting_widget(text)
        self._alt_greeting_section.set_token_count(
            sum(count_tokens(e.toPlainText()) for e in self._alt_greeting_edits),
        )

    def _create_greeting_widget(self, text: str = '') -> None:
        idx = len(self._alt_greeting_edits) + 1
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 0)
        row_layout.setSpacing(4)
        label = QLabel(f'Greeting {idx}:')
        label.setStyleSheet('font-size: 11px; font-weight: bold; color: #aaa;')
        row_layout.addWidget(label)
        row_layout.addStretch()
        del_btn = QPushButton('x')
        del_btn.setFixedSize(16, 16)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setStyleSheet(
            'QPushButton { background: transparent; color: #999; border: none;'
            ' padding: 0px; font-size: 11px; font-weight: bold; }'
            ' QPushButton:hover { color: #ff6666; }'
        )
        row_layout.addWidget(del_btn)
        insert_pos = self._alt_greeting_layout.count() - 1
        self._alt_greeting_layout.insertWidget(insert_pos, row)
        edit = QTextEdit()
        edit.setMaximumHeight(120)
        edit.setPlainText(text)
        edit.textChanged.connect(self._mark_dirty)
        self._alt_greeting_layout.insertWidget(insert_pos + 1, edit)
        self._alt_greeting_rows.append(row)
        self._alt_greeting_edits.append(edit)
        del_btn.clicked.connect(lambda checked=False, r=row, e=edit: self._delete_greeting(r, e))

    def _add_greeting(self) -> None:
        self._create_greeting_widget('')
        self._mark_dirty()

    def _delete_greeting(self, row: QWidget, edit: QTextEdit) -> None:
        edit.textChanged.disconnect(self._mark_dirty)
        self._alt_greeting_edits.remove(edit)
        if row in self._alt_greeting_rows:
            self._alt_greeting_rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        edit.setParent(None)
        edit.deleteLater()
        self._renumber_greetings()
        self._mark_dirty()

    def _renumber_greetings(self) -> None:
        layout = self._alt_greeting_container.layout()
        idx = 0
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item and item.widget():
                label = item.widget().findChild(QLabel)
                if label and 'Greeting' in label.text():
                    idx += 1
                    label.setText(f'Greeting {idx}:')
        self._alt_greeting_section.set_token_count(
            sum(count_tokens(e.toPlainText()) for e in self._alt_greeting_edits),
        )

    def _update_token_count(self) -> None:
        self._token_timer.start()

    def _do_update_token_count(self) -> None:
        # Encode each field individually once, then sum for permanent/full.
        # _get_field_text (not raw toPlainText) so HTML-preview mode counts
        # the underlying source text, not the rendered document.
        field_texts = {
            'name': self._name_edit.text(),
            'desc': self._get_field_text(self._desc_edit),
            'pers': self._get_field_text(self._pers_edit),
            'scen': self._get_field_text(self._scen_edit),
            'sys': self._get_field_text(self._sys_edit),
            'phi': self._get_field_text(self._phi_edit),
            'first': self._get_field_text(self._first_edit),
            'example': self._get_field_text(self._example_edit),
            'notes': self._get_field_text(self._notes_edit),
        }
        field_tokens = {k: count_tokens(v) for k, v in field_texts.items()}
        alt_tokens = sum(count_tokens(e.toPlainText()) for e in self._alt_greeting_edits)

        perm = field_tokens['name'] + field_tokens['desc'] + field_tokens['pers'] + field_tokens['scen']
        full = perm + field_tokens['first'] + field_tokens['example'] + field_tokens['sys'] + field_tokens['phi']
        self._token_label.setText(f'Tokens: {perm:,} (permanent) / {full:,} (full)')

        self._name_token_label.setText(f'Name: ({field_tokens["name"]:,} Tokens)')
        self._desc_section.set_token_count(field_tokens['desc'])
        self._pers_section.set_token_count(field_tokens['pers'])
        self._scen_section.set_token_count(field_tokens['scen'])
        self._sys_section.set_token_count(field_tokens['sys'])
        self._phi_section.set_token_count(field_tokens['phi'])
        self._first_section.set_token_count(field_tokens['first'])
        self._example_section.set_token_count(field_tokens['example'])
        self._notes_section.set_token_count(field_tokens['notes'])
        self._alt_greeting_section.set_token_count(alt_tokens)

    def _add_tag(self) -> None:
        tag = self._tag_input.text().strip()
        if tag:
            self._tag_widget.add_tag(tag)
            self._tag_input.clear()

    def refresh_tag_completer(self) -> None:
        """Refresh the tag autocomplete model from the DB.

        Called on construction and whenever the library changes (wired from
        the main window via ``library_changed``).
        """
        try:
            tags = self.db.get_all_tags()
        except Exception:
            logger.exception("Failed to load tags for autocomplete")
            tags = []
        self._tag_completer_model.setStringList(tags)

    def _open_tag_manager(self) -> None:
        from src.ui.widgets.tag_manager import TagManagerDialog
        try:
            db_before = set(self.db.get_all_tags())
        except Exception:
            logger.exception("Failed to load tags before tag manager")
            return
        dlg = TagManagerDialog(self.db, self)
        dlg.exec()
        # Tags may have changed across the library; refresh autocomplete and
        # reconcile only the tag list into the current form — a full card
        # reload here would silently discard unsaved edits.  Reconciliation
        # drops a form tag only when the manager removed/renamed it in the
        # library; mixed-case variants and new unsaved tags are preserved.
        self.refresh_tag_completer()
        if self._current_id is not None:
            try:
                db_after = set(self.db.get_all_tags())
            except Exception:
                logger.exception("Failed to load tags after tag manager")
                return
            current = self._tag_widget.get_tags()
            reconciled = reconcile_form_tags(current, db_before, db_after)
            if reconciled != current:
                self._tag_widget.set_tags(reconciled)
                self._mark_dirty()

    def _on_open_folder(self) -> None:
        if self._current_id is None:
            return
        entry = self.db.get_by_id(self._current_id)
        if not entry:
            return
        source = entry.get('source_path', '')
        from src.path_utils import open_containing_folder
        if not open_containing_folder(source):
            QMessageBox.warning(self, 'Open Folder', 'Source file not found.')
            logger.warning("Could not open folder for card %s (source missing)", self._current_id)

    def _edit_character_book(self) -> None:
        from src.ui.widgets.character_book_editor import CharacterBookEditor
        dlg = CharacterBookEditor(self._character_book_dict, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._character_book_dict = dlg.get_book_dict()
            self._mark_dirty()

    def _edit_extensions(self) -> None:
        if self._current_card is None:
            return
        from src.ui.widgets.extensions_editor import ExtensionsEditor
        dlg = ExtensionsEditor(self._current_card.extensions, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._current_card.extensions = dlg.get_extensions()
            self._mark_dirty()

    def _change_image(self) -> None:
        if self._current_id is None or self._current_card is None:
            return
        entry = self.db.get_by_id(self._current_id)
        if not entry:
            return
        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            QMessageBox.warning(self, 'Error', 'Source file not found.')
            return

        path, _ = QFileDialog.getOpenFileName(
            self, 'Select New Image', '',
            'Image Files (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*)',
        )
        if not path:
            return

        try:
            replace_card_image(source, path, source)
            from src.card_parser import save_thumbnail
            thumb_path = entry.get('thumbnail_path', '')
            if thumb_path and not save_thumbnail(source, thumb_path):
                logger.warning("Thumbnail regeneration failed for %s", source)
            QMessageBox.information(self, 'Image Changed', 'Character image updated successfully.')
            self.card_updated.emit(self._current_id)
        except Exception as e:
            QMessageBox.critical(self, 'Error', f"Failed to change image: {e}")

    def _save_user_notes(self) -> None:
        """Persist the private user notes for the currently selected card."""
        if self._current_id is None:
            return
        self.db.set_user_notes(self._current_id, self._user_notes_edit.toPlainText())
        self.status_message.emit('Notes saved', 2000)

    def save(self) -> bool:
        """Save the current card. Returns True on success."""
        if self._current_id is None:
            return False
        if self._form_loaded_for_id != self._current_id or self._current_card is None:
            # The form content belongs to a different card than the one
            # currently selected (e.g. the selection changed while another
            # tab was frontmost). Saving would corrupt the selected card.
            QMessageBox.warning(
                self, 'Save',
                'The editor form does not match the selected card.\n'
                'Nothing was saved — reopen the card on the Edit tab and try again.',
            )
            return False
        card = self._gather_card()
        card.token_count = count_card_tokens(card)
        try:
            self.db.update_card(self._current_id, card, rename_file_on_name_change=True)
            self._dirty_state.clear()
            self.status_message.emit(f"'{card.name}' saved", 4000)
            # Reload from disk so the editor (header, file path, in-memory
            # card) reflects the saved state, including a renamed file when
            # the character name changed.
            self._load_card()
            self.card_updated.emit(self._current_id)
            return True
        except Exception as e:
            QMessageBox.critical(self, 'Error', f"Failed to save: {e}")
            return False

    def _on_toggle_favorite(self) -> None:
        """Toggle the loaded card's favorite flag (mirrors Library tab)."""
        if self._current_id is None or self._form_loaded_for_id != self._current_id:
            return
        was_checked = self._fav_btn.isChecked()
        try:
            new_state = self.db.toggle_favorite(self._current_id)
        except Exception as e:
            # Qt already flipped the checkable button visually; restore the
            # pre-click state (tracked on the in-memory card).
            self._fav_btn.setChecked(
                bool(self._current_card.fav) if self._current_card else was_checked,
            )
            QMessageBox.critical(self, 'Error', f'Failed to toggle favorite: {e}')
            logger.exception('Favorite toggle error')
            return
        # Keep button and in-memory card consistent with the DB so saving
        # this form won't revert the toggle.
        if self._current_card is not None:
            self._current_card.fav = new_state
        self._fav_btn.setChecked(new_state)
        name = self._name_edit.text().strip() or 'card'
        self.status_message.emit(
            f"'{name}' {'favorited' if new_state else 'unfavorited'}", 4000,
        )
        self.card_updated.emit(self._current_id)

    def _export_png(self) -> None:
        if self._current_id is None:
            return
        entry = self.db.get_by_id(self._current_id)
        if not entry:
            return
        name = self._name_edit.text().strip() or 'card'
        dest, _ = QFileDialog.getSaveFileName(
            self, 'Export Card (PNG)', f"{sanitize_filename(name)}.png", 'PNG Files (*.png)',
        )
        if not dest:
            return
        card = self._gather_card()
        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            QMessageBox.warning(self, 'Export', 'Source file not found. Cannot export.')
            return
        try:
            if Path(source).suffix.lower() != '.png':
                # JSON-imported cards have no base image: build one so the
                # card can still be exported as a valid PNG.
                from PIL import Image
                tmp_img = Path(dest).with_suffix('.tmp-img.png')
                Image.new('RGBA', (400, 600), (60, 60, 60, 255)).save(tmp_img, 'PNG')
                try:
                    write_chara_card_dual(str(tmp_img), dest, card.to_spec_dict())
                finally:
                    tmp_img.unlink(missing_ok=True)
            else:
                write_chara_card_dual(source, dest, card.to_spec_dict())
            self.status_message.emit(f"Exported PNG to {Path(dest).name}", 4000)
        except Exception as e:
            logger.exception("PNG export failed")
            QMessageBox.critical(self, 'Export', f"Failed to export PNG: {e}")

    def _export_json(self) -> None:
        if self._current_id is None:
            return
        name = self._name_edit.text().strip() or 'card'
        dest, _ = QFileDialog.getSaveFileName(
            self, 'Export Card (JSON)', f"{sanitize_filename(name)}.json", 'JSON Files (*.json)',
        )
        if not dest:
            return
        card = self._gather_card()
        try:
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump(card.to_spec_dict(), f, ensure_ascii=False, indent=2)
            self.status_message.emit(f"Exported JSON to {Path(dest).name}", 4000)
        except OSError as e:
            logger.exception("JSON export failed")
            QMessageBox.critical(self, 'Export', f"Failed to export JSON: {e}")

    # ---- Public API for menu / shortcut delegation ----

    def is_dirty(self) -> bool:
        """Return True if the form has unsaved changes."""
        return self._dirty_state.is_dirty

    def clear_dirty(self) -> None:
        """Mark the form as clean (used after Discard in the dirty guard)."""
        self._dirty_state.clear()

    def revert(self) -> None:
        """Reload the current card from disk, discarding edits."""
        self._load_card()

    def delete_card(self) -> None:
        """Delete the current card from the library after confirmation."""
        if self._current_id is None:
            return
        entry = self.db.get_by_id(self._current_id)
        if not entry:
            return
        reply = QMessageBox.question(
            self, 'Delete Character',
            f"Delete '{entry['name']}'?\n\n"
            "This removes the card from the library and permanently deletes "
            "its card file from disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        deleted_id = self._current_id
        self.db.remove_card(deleted_id, delete_files=True)
        self._current_id = None
        self._current_card = None
        self._form_loaded_for_id = None
        self._clear_form()
        self.card_deleted.emit(deleted_id)

    def _clear_form(self) -> None:
        """Reset the edit form to its empty (no-card) state.

        Called after deleting the current card so the deleted card's edited
        content and any unsaved-change (dirty) flag do not linger — otherwise
        the next selection would show a spurious "unsaved changes" prompt, and
        saving would write the stale content over another card.
        """
        self._form_loaded_for_id = None
        self._dirty_state.begin_load()
        try:
            if self._html_preview_mode:
                self._preview_html_btn.blockSignals(True)
                self._preview_html_btn.setChecked(False)
                self._preview_html_btn.blockSignals(False)
                for field in self._html_fields:
                    self._restore_plain_text(field)
                self._html_preview_mode = False
            self._html_sources = {}
            self._html_field_formats = {}

            self._header.setText('Select a character to edit')
            self._name_edit.clear()
            for field in self._html_fields:
                self._set_field_text(field, '')
            self._set_field_text(self._user_notes_edit, '')
            self._tag_widget.set_tags([])
            self._rebuild_alt_greetings([])
            self._creator_edit.clear()
            self._version_edit.clear()
            self._talk_spin.setValue(0.5)
            self._character_book_dict = None
            self._change_img_btn.setEnabled(False)
            self._open_folder_btn.setEnabled(False)
            self._preview_html_btn.setEnabled(False)
            self._fav_btn.setEnabled(False)
            self._fav_btn.setChecked(False)
            self._char_book_btn.setEnabled(False)
            self._extensions_btn.setEnabled(False)
            self._token_label.setText('Tokens: 0 (permanent) / 0 (full)')
            self._name_token_label.setText('Name: (0 Tokens)')
            for section in self._sections:
                section.set_token_count(0)
        finally:
            self._dirty_state.end_load()

    def export_png(self) -> None:
        """Export the current card as a PNG file (menu entry)."""
        self._export_png()

    def duplicate_card(self) -> None:
        """Duplicate the current card into the library with a '(copy)' suffix."""
        if self._current_id is None or self._current_card is None:
            return
        import shutil
        import uuid
        from src.card_models import build_duplicate_card
        from src.database import _get_library_dir
        base = self._current_card
        entry = self.db.get_by_id(self._current_id)
        if not entry:
            return
        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            QMessageBox.warning(self, 'Duplicate', 'Source file not found.')
            return

        clone = build_duplicate_card(base)
        clone.character_book = self._character_book_dict
        clone.token_count = count_card_tokens(clone)

        lib_dir = _get_library_dir()
        ext = Path(source).suffix
        # Card names may contain characters illegal in Windows filenames;
        # use the DB's sanitizer and keep the copy inside the try so a
        # failure can't escape the slot (PyQt6 aborts on slot exceptions).
        dest_path = lib_dir / f"{sanitize_filename(clone.name)[:60]}_{uuid.uuid4().hex[:8]}{ext}"
        try:
            shutil.copy2(source, str(dest_path))
            clone.source_path = str(dest_path)
            new_id = self.db.add_card(clone)
            self.status_message.emit(f"Duplicated as '{clone.name}'", 4000)
            self.card_added.emit(new_id)
            self.load_card_by_id(new_id)
        except Exception as e:
            QMessageBox.critical(self, 'Error', f"Failed to duplicate: {e}")
            logger.exception("Duplicate failed")

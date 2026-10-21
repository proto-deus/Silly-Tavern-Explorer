from __future__ import annotations

import html
import json
import logging
import time
import traceback
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.ai_client import (
    AIClient,
    APIPreset,
    preset_from_saved,
    split_for_context,
    vary_seed,
    with_sampling_override,
)
from src.attachments import prepare_attachment
from src.card_models import CharacterBook, CharacterCard
from src.card_parser import read_card_data
from src.chat_builder import (
    append_message,
    assemble_system,
    build_context_plan,
    build_initial_messages,
    clean_generated_text,
    example_block_text,
    example_messages_for_card,
    history_for_api,
    injections_for_chat,
    post_history_text,
    substitute_macros,
)
from src.chat_sessions import (
    ChatSessionStore,
    apply_attribute_updates,
    auto_title,
    default_attributes,
    drop_memories_from,
    new_memory_entry,
    normalize_attributes,
    normalize_author_note,
    normalize_memories,
    parse_attribute_updates,
    session_flags,
)
from src.database import LibraryDatabase
from src import context_templates
from src import lorebook_store
from src import st_chat_io
from src.prompt_templates import active_template_id, resolve_template
from src.settings_manager import (
    get_persona,
    load_active_lorebooks,
    load_api_settings,
    load_active_persona_id,
    load_font_size,
    load_macro_settings,
    load_provider_models,
    load_test_settings,
    save_active_lorebooks,
    save_active_sampling,
)
from src.token_counter import count_tokens
from src.ui.spell_check import SpellCheckTextEdit
from src.ui.styles import ui_font_px
from src.ui.widgets.chat_bubble import MessageBubble
from src.ui.widgets.chat_image_loader import _widget_alive
from src.ui.widgets.context_inspector_dialog import ContextInspectorDialog
from src.ui.widgets.edit_message_dialog import TextEditDialog
from src.ui.widgets.memory_dialog import MemoryDialog

logger = logging.getLogger(__name__)


def _format_session_ts(iso: str) -> str:
    if not iso:
        return ''
    try:
        return datetime.fromisoformat(iso).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return iso


# Chat export formats offered by the save dialog: (file filter, extension).
_EXPORT_FILTERS: tuple[tuple[str, str], ...] = (
    ('Text (*.txt)', '.txt'),
    ('JSON (*.json)', '.json'),
    ('SillyTavern chat (*.jsonl)', '.jsonl'),
    ('Markdown (*.md)', '.md'),
    ('HTML (*.html)', '.html'),
)
_EXPORT_FILE_FILTER = ';;'.join(f for f, _ in _EXPORT_FILTERS)
_EXPORT_EXTENSIONS = {ext for _, ext in _EXPORT_FILTERS}


def _resolve_export_format(path: str, selected_filter: str) -> tuple[str, str]:
    """Normalise the destination path and derive its export format.

    The format is the one whose file-type filter the user picked in the save
    dialog; a destination with no suffix (or one of the known export suffixes)
    is rewritten to that format's extension.  Pure so the mapping is
    unit-testable without a file dialog.
    """
    ext_by_filter = {f: ext for f, ext in _EXPORT_FILTERS}
    ext = ext_by_filter.get(selected_filter, '.txt')
    p = Path(path)
    if not p.suffix or p.suffix.lower() in _EXPORT_EXTENSIONS:
        path = str(p.with_suffix(ext))
    return path, ext.lstrip('.')


def _safe_variant_pos(value, default: int) -> int:
    """Coerce a stored ``variant_pos`` to int, falling back to *default*.

    Session JSON is user-editable; a non-numeric value here used to raise
    inside a Qt slot, which aborts the whole process.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


#: The share of the context window at which the token label turns amber.
CONTEXT_WARNING_RATIO = 0.7

#: The colours of the token label (readable on the app's dark theme).
CONTEXT_COLOR_NORMAL = '#777'
CONTEXT_COLOR_WARNING = '#e0a030'
CONTEXT_COLOR_OVER = '#ff6666'

#: The suffix appended to the token label when the prompt does not fit.
CONTEXT_OVER_SUFFIX = ' - over the context window!'


def context_pressure_level(tokens: int, limit: int) -> str:
    """Classify how full the context window is.

    Args:
        tokens: The number of tokens the prompt uses.
        limit: The size of the context window (0 = unknown).

    Returns:
        ``'ok'`` while the prompt uses less than ~70 % of the window,
        ``'warning'`` from there up to the window size, and ``'over'`` when
        the prompt does not fit into the window at all.
    """
    try:
        used = max(0, int(tokens))
    except (TypeError, ValueError):
        used = 0
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return 'ok'
    if used > size:
        return 'over'
    if used >= size * CONTEXT_WARNING_RATIO:
        return 'warning'
    return 'ok'


def count_prompt_tokens(messages) -> int:
    """The number of tokens of the messages that were sent.

    The text of every message is counted with the app's own tokenizer, so
    the number matches the one the context budget was computed with.  Image
    parts are not counted (they are charged a flat estimate elsewhere).

    Pure function so the counting is unit-testable without a running chat.
    """
    total = 0
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        content = message.get('content', '')
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'text':
                    total += count_tokens(part.get('text', '') or '')
    return total


def record_generation_usage(
    provider: str,
    model: str,
    card_id: int | None,
    card_name: str,
    session_id: str,
    prompt_messages,
    reply: str,
) -> None:
    """Record one completed generation in the usage ledger.

    The prompt that was sent is charged as the input and the reply as the
    output.  The two are stored as separate entries (one per role) so the
    "By role" breakdown of the usage dialog is meaningful.

    The call is guarded - a broken ledger must never break a chat.

    Args:
        provider: The name of the provider that served the generation.
        model: The model that served the generation.
        card_id: The id of the card the chat belongs to.
        card_name: The name of that card.
        session_id: The id of the chat session.
        prompt_messages: The messages that were sent.
        reply: The reply that came back.
    """
    try:
        from src.usage_ledger import record_generation

        record_generation(
            provider=provider,
            model=model,
            card_id=card_id,
            card_name=card_name,
            session_id=session_id,
            input_tokens=count_prompt_tokens(prompt_messages),
            output_tokens=count_tokens(reply or ''),
        )
    except Exception:
        logger.exception("Could not record the usage of the generation")


def notify_generation_finished(card_id: int | None, provider: str, model: str) -> None:
    """Announce a finished generation to the plugin/event bus.

    The event module is imported lazily - it is an optional cross-cutting
    module and must not be pulled in at start-up - and every error is
    swallowed: a failing user plugin must never break the chat.

    Args:
        card_id: The id of the card the chat belongs to.
        provider: The name of the provider that served the generation.
        model: The model that served the generation.
    """
    try:
        from src import app_events
        app_events.generation_finished(card_id, provider, model)
    except Exception:
        logger.exception("Could not announce the finished generation")


class _ChatWorker(QThread):
    """Streams a chat completion over a message history."""
    # Named ``completed`` so the built-in QThread.finished stays available
    # for lifetime management (deleteLater).
    completed = pyqtSignal(str)
    chunk = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(
        self,
        client: AIClient,
        system: str,
        messages: list[dict],
        parent=None,
        post_history: str = '',
        jailbreak: str = '',
        examples_block: str = '',
        injections: list | None = None,
        mode: str = 'assistant',
        name_prefixes: dict[str, str] | None = None,
        conversation_start_header: str | None = None,
        card_id: int | None = None,
        card_name: str = '',
        session_id: str = '',
        provider: str = '',
        model: str = '',
    ):
        super().__init__(parent)
        self.client = client
        self.system = system
        self.messages = messages
        self.post_history = post_history
        self.jailbreak = jailbreak
        self.examples_block = examples_block
        self.injections = list(injections or [])
        # How the completed text is applied: 'assistant' appends a new reply,
        # 'user' appends the model-written {{user}} message (Impersonate), and
        # 'continue' extends the last assistant reply.
        self.mode = mode
        # Context-template knobs: history name prefixes and the header used
        # when a leading greeting folds into the system message.
        self.name_prefixes = name_prefixes
        self.conversation_start_header = conversation_start_header
        # What the generation is billed to (the usage ledger records it).
        self.card_id = card_id
        self.card_name = card_name
        self.session_id = session_id
        self.provider = provider
        self.model = model
        self._cancel = False

    def cancel(self):
        self._cancel = True
        self.client.cancel()

    def run(self):
        try:
            if self._cancel:
                return
            parts: list[str] = []
            for piece in self.client.generate_chat(
                history_for_api(self.messages, self.name_prefixes), self.system,
                stream=True,
                post_history=self.post_history,
                jailbreak=self.jailbreak,
                examples_block=self.examples_block,
                injections=self.injections,
                conversation_start_header=self.conversation_start_header,
            ):
                if self._cancel:
                    return
                parts.append(piece)
                self.chunk.emit(piece)
            if not self._cancel:
                reply = ''.join(parts)
                self._record_usage(reply)
                notify_generation_finished(
                    self.card_id, self.provider, self.model,
                )
                self.completed.emit(reply)
        except Exception as e:
            if not self._cancel:
                self.error.emit(f"{e}\n{traceback.format_exc()}")

    def _record_usage(self, reply: str) -> None:
        """Book the finished generation in the usage ledger.

        The prompt is the one the client actually sent (trimmed and with
        the depth injections spliced in), so the recorded token numbers
        describe the real request.
        """
        record_generation_usage(
            provider=self.provider,
            model=self.model,
            card_id=self.card_id,
            card_name=self.card_name,
            session_id=self.session_id,
            prompt_messages=getattr(self.client, 'last_request_messages', None),
            reply=reply,
        )


class _SummarizeWorker(QThread):
    """Summarizes the current conversation into chat memory (non-streaming)."""
    completed = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, client: AIClient, system: str, user: str, parent=None):
        super().__init__(parent)
        self.client = client
        self.system = system
        self.user = user
        self._cancel = False

    def cancel(self):
        self._cancel = True
        self.client.cancel()

    def run(self):
        try:
            if self._cancel:
                return
            result = self.client.generate(self.system, self.user, stream=False)
            if not self._cancel:
                self.completed.emit(result)
        except Exception as e:
            if not self._cancel:
                self.error.emit(f"{e}\n{traceback.format_exc()}")


class _SessionsDialog(QDialog):
    """Popup listing saved chats for a card: Load / Export / Delete / Import.

    Export and Import emit signals instead of doing file I/O here: the parent
    owns the session store and the chat formats, and the file dialogs it opens
    must be parented to this dialog so they stack above it.
    """

    export_requested = pyqtSignal(str)
    import_requested = pyqtSignal()

    def __init__(self, sessions: list[dict], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Chats')
        self.setMinimumSize(480, 320)
        self._sessions = sessions

        layout = QVBoxLayout(self)
        self._list = QListWidget()
        self._empty_label = QLabel('No saved chats for this character.')
        for s in sessions:
            title = s.get('title') or '(untitled)'
            count = s.get('message_count', 0)
            ts = _format_session_ts(s.get('created_at') or s.get('updated_at', ''))
            label = f"{title}  —  {count} message(s)"
            if ts:
                label += f"  —  {ts}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, s.get('id'))
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)
        self._list.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self._list)
        layout.addWidget(self._empty_label)

        btn_row = QHBoxLayout()
        self._load_btn = QPushButton('Load')
        self._load_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._load_btn)
        self._export_btn = QPushButton('Export')
        self._export_btn.setToolTip(
            'Save the selected chat as text, JSON, or a SillyTavern .jsonl chat.'
        )
        self._export_btn.clicked.connect(self._on_export)
        btn_row.addWidget(self._export_btn)
        self._delete_btn = QPushButton('Delete')
        self._delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self._delete_btn)
        self._import_btn = QPushButton('Import...')
        self._import_btn.setToolTip(
            'Import a SillyTavern .jsonl chat as a new chat for this character.'
        )
        self._import_btn.clicked.connect(self.import_requested.emit)
        btn_row.addWidget(self._import_btn)
        btn_row.addStretch()
        # "Close", not "Cancel": Delete is confirmed and applied immediately,
        # so closing the dialog is not a rollback (a button labelled Cancel
        # that still destroys data is a trap).
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        has_selection = self._list.currentItem() is not None
        self._load_btn.setEnabled(has_selection)
        self._export_btn.setEnabled(has_selection)
        self._delete_btn.setEnabled(has_selection)
        self._empty_label.setVisible(self._list.count() == 0)

    def _on_export(self) -> None:
        session_id = self.selected_id()
        if session_id is not None:
            self.export_requested.emit(session_id)

    def _on_delete(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(
            self, 'Delete Chat', 'Delete this saved chat?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._list.takeItem(self._list.row(item))
            self._deleted_ids = getattr(self, '_deleted_ids', [])
            self._deleted_ids.append(session_id)
            self._sync_buttons()

    def selected_id(self) -> str | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def deleted_ids(self) -> list[str]:
        return getattr(self, '_deleted_ids', [])


class _LorebookSelectDialog(QDialog):
    """Popup for choosing which standalone lorebooks feed the Test chat."""

    def __init__(self, available: list[dict], active: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Lorebooks (World Info)')
        self.setMinimumSize(420, 380)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            'Selected lorebooks are scanned every turn and matching entries '
            'are injected as [World Info] alongside the card\'s own book.'
        ))

        self._checks: list[QCheckBox] = []
        active_set = set(active)
        for meta in available:
            cb = QCheckBox(f"{meta['name']}  ({meta['entries']} entr"
                           f"{'y' if meta['entries'] == 1 else 'ies'})")
            cb.setChecked(meta['filename'] in active_set)
            cb.setProperty('filename', meta['filename'])
            layout.addWidget(cb)
            self._checks.append(cb)

        if not available:
            layout.addWidget(QLabel(
                'No lorebooks found.\nCreate or import some in the Lorebooks tab.'
            ))

        btn_row = QHBoxLayout()
        select_all_btn = QPushButton('Select All')
        select_all_btn.clicked.connect(lambda: self._set_all(True))
        btn_row.addWidget(select_all_btn)
        none_btn = QPushButton('Select None')
        none_btn.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(none_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_all(self, checked: bool) -> None:
        for cb in self._checks:
            cb.setChecked(checked)

    def selected_filenames(self) -> list[str]:
        return [
            cb.property('filename') for cb in self._checks if cb.isChecked()
        ]


class ChatInputEdit(SpellCheckTextEdit):
    """Multi-line message input: plain Enter sends, Shift+Enter adds a line."""

    send_requested = pyqtSignal()

    def keyPressEvent(self, event) -> None:
        sends = event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ) and not event.modifiers() & (
            Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        if sends:
            self.send_requested.emit()
            return
        super().keyPressEvent(event)


class TestTab(QWidget):
    status_message = pyqtSignal(str, int)
    settings_requested = pyqtSignal()

    def __init__(self, db: LibraryDatabase, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self._store = ChatSessionStore()
        self._worker: _ChatWorker | None = None
        self._summarize_worker: _SummarizeWorker | None = None
        # Workers whose result has been handled but whose thread may still
        # be running: they are kept referenced until the thread really
        # finished, otherwise the QThread could be destroyed while it was
        # still running ("QThread: Destroyed while thread is still
        # running" aborts the process).
        self._pending_workers: list[QThread] = []
        self._generating = False
        self._at_bottom = True
        # True while _render_history tears down/rebuilds the bubbles, so the
        # scrollbar's intermediate range changes don't trigger follow/scroll.
        self._rebuilding = False

        self._current_id: int | None = None
        self._card: CharacterCard | None = None
        self._session_id: str | None = None
        self._messages: list[dict[str, str]] = []
        self._memories: list[dict] = []
        self._auto_summarize = True
        self._summarize_source = 'summary'
        self._summarize_end_index: int | None = None
        self._summarize_char_id: int | None = None
        self._summarize_session_id: str | None = None
        self._summarize_prefix = ''
        self._summarize_quiet = False
        self._memory_dialog: MemoryDialog | None = None
        self._attributes: list[dict] = []
        self._auto_update_attributes = True
        self._attr_worker: _SummarizeWorker | None = None
        self._attr_char_id: int | None = None
        self._attr_session_id: str | None = None
        self._attr_quiet = True
        self._bubbles: list[MessageBubble] = []
        self._stream_bubble: MessageBubble | None = None
        self._pending_attachments: list[dict] = []
        self._greeting_index = 0
        # SillyTavern-style per-chat prompt knobs (persisted per session):
        # Author's Note injection, user jailbreak block, and active persona.
        self._author_note: dict = normalize_author_note(None)
        self._jailbreak = ''
        self._persona_id = load_active_persona_id()
        self._persona = get_persona(self._persona_id).get('text', '')
        # How the next response is applied (see _ChatWorker.mode).
        self._response_mode = 'assistant'
        # What the running generation is billed to (card, session, provider
        # and model); captured when the generation starts.
        self._generation_meta: dict = {}
        # The colour of the token label (follows the context pressure).
        self._token_label_color = CONTEXT_COLOR_NORMAL
        # Number of leading messages already covered by a rolling summary.
        self._summarized_through = 0
        # Seed bump applied per regeneration so fixed seeds still vary.
        self._regen_bump = 0
        # Alternate responses (swipes): while a regenerate/try-again is in
        # flight, _regen_tried holds every previously generated text for the
        # slot and _regen_replaced stashes the removed message dict so a
        # failed/cancelled run can put it back untouched.
        self._regen_tried: list[str] | None = None
        self._regen_replaced: tuple[int, dict] | None = None
        # Standalone lorebooks injected alongside the card's character book.
        self._active_lore_names: list[str] = load_active_lorebooks()
        self._extra_books: list[CharacterBook] = []

        # Model/Temp/Min-P/Ctx write-through to the saved provider settings so
        # the settings dialog always shows what the Test tab is using.
        # Debounced because the widgets emit changed signals on every step.
        self._suppress_sampling_sync = False
        self._sampling_save_timer = QTimer(self)
        self._sampling_save_timer.setSingleShot(True)
        self._sampling_save_timer.setInterval(300)
        self._sampling_save_timer.timeout.connect(self._flush_sampling_sync)

        # Token recount is O(chat); coalesce the bursts that re-rendering
        # produces (and the paths that update the label twice in a row).
        self._token_label_timer = QTimer(self)
        self._token_label_timer.setSingleShot(True)
        self._token_label_timer.setInterval(250)
        self._token_label_timer.timeout.connect(self._recount_tokens)

        self._preset = self._load_preset()
        self._font_size = load_font_size()
        self._reload_macros()
        self._reload_test_settings()
        self._reload_extra_books()

        self._build_ui()
        # The Auto-Summarize button starts unchecked like every QPushButton;
        # mirror the (on-by-default) flag onto it before anything is shown.
        self._sync_auto_toggle()
        self._set_inputs_enabled(False)

    # ---- configuration ----

    def _load_preset(self) -> APIPreset:
        return preset_from_saved(load_api_settings())

    def _reload_macros(self) -> None:
        macro = load_macro_settings()
        self._user_name = macro.get('user_name') or 'User'
        self._custom_macros = macro.get('macros') or {}
        # The active persona is global (Settings -> Macros); always follow it.
        self._persona_id = load_active_persona_id()
        self._persona = get_persona(self._persona_id).get('text', '')

    def _reload_test_settings(self) -> None:
        settings = load_test_settings()
        self._dialogue_color = settings.get('dialogue_color', '#9ad8ff')
        self._action_color = settings.get('action_color', '#e0a060')
        self._emphasis_color = settings.get('emphasis_color', '#9be8a0')
        self._show_timestamps = bool(settings.get('show_timestamps', False))
        self._auto_scroll = bool(settings.get('auto_scroll', True))
        self._include_first = bool(settings.get('include_first_message', True))
        placement = settings.get('example_placement', 'system')
        self._example_placement = (
            placement if placement in ('system', 'post_history', 'history') else 'system'
        )

    def reload_preset(self) -> None:
        """Reload API preset + macros + test settings (after settings change)."""
        self._preset = self._load_preset()
        self._font_size = load_font_size()
        self._apply_font_sizes()
        self._reload_macros()
        self._reload_test_settings()
        # Override widgets track the freshly-loaded preset.
        self._refresh_model_combo(self._preset.model)
        self._set_sampling_widgets(
            self._preset.temperature, self._preset.min_p, self._preset.context_size,
        )
        self._refresh_min_p_tooltip()
        self._render_history()

    def _refresh_min_p_tooltip(self) -> None:
        """Say whether the active provider implements Min-P at all.

        The value is saved either way, but a provider that does not know
        the parameter ignores it - which looks like a broken slider.
        """
        from src.provider_capabilities import unsupported_settings

        widget = getattr(self, '_minp_override', None)
        if widget is None:
            return
        text = (
            'Min-P sampling override (0 = off); saved to Settings -> LLM '
            'when changed.\n'
            'A small value (~0.05) trims unlikely tokens while keeping '
            'creativity — a good companion to a higher temperature.'
        )
        provider = getattr(self._preset, 'name', '')
        if 'min_p' in unsupported_settings(provider):
            text += (
                f'\n{provider} does not implement Min-P: the value is left '
                'out of the request and has no effect.'
            )
        else:
            text += '\nNot every provider implements Min-P.'
        widget.setToolTip(text)

    def _assemble_system(self) -> str:
        """Single source of truth for the system prompt sent to the API.

        Layers (SillyTavern order, see ``chat_builder.system_prompt_blocks``):
        main prompt -> world info -> description/personality/scenario ->
        user persona -> persistent attributes -> chat memory, plus the
        example dialogue block when the placement setting keeps it in the
        system prompt.  The context inspector shares these blocks so the two
        can never drift apart.
        """
        if self._card is None:
            return ''
        system = assemble_system(
            self._card,
            self._messages,
            user_name=self._user_name,
            persona=self._persona,
            memories=self._memories,
            attributes=self._attributes,
            custom_macros=self._custom_macros,
            chat_template=self._sys_template().content,
            extra_books=self._extra_books,
            context_template=self._ctx_template(),
        )
        if self._example_placement == 'system':
            block = self._examples_block()
            if block:
                system = f'{system}\n\n{block}' if system.strip() else block
        return system

    def _examples_block(self) -> str:
        """Few-shot example dialogue as one labelled text block ('' = none)."""
        if self._card is None:
            return ''
        return example_block_text(
            self._card, self._user_name, self._custom_macros,
            context_template=self._ctx_template(),
        )

    def _examples_for_history(self) -> list[dict]:
        """Few-shot examples as fake history turns (legacy placement only)."""
        if self._card is None or self._example_placement != 'history':
            return []
        return example_messages_for_card(
            self._card, self._user_name, self._custom_macros,
        )

    def _post_history_text(self) -> str:
        """Trailing instructions for the request (may be empty).

        The card's ``post_history_instructions`` first, then the active
        template's own post-history text (SillyTavern sysprompt presets carry
        one), both macro-substituted.
        """
        parts = []
        if self._card is not None:
            card_phi = post_history_text(self._card, self._user_name, self._custom_macros)
            if card_phi:
                parts.append(card_phi)
        tpl_phi = self._sys_template().post_history.strip()
        if tpl_phi:
            parts.append(substitute_macros(
                tpl_phi, self._user_name,
                self._card.name if self._card else '', self._custom_macros,
            ))
        return '\n\n'.join(parts)

    def _jailbreak_text(self, text: str | None = None) -> str:
        """Macro-substituted user jailbreak block (may be empty).

        *text* overrides the stored jailbreak, which is what the Context
        Inspector needs: it previews the *edited* note and jailbreak before
        the user applies them.
        """
        source = self._jailbreak if text is None else text
        if not source.strip():
            return ''
        return substitute_macros(
            source, self._user_name,
            self._card.name if self._card else '', self._custom_macros,
        )

    def _effective_preset(self) -> APIPreset:
        """Preset for the next chat request: per-chat overrides + seed bump."""
        preset = with_sampling_override(
            self._preset,
            temperature=self._temp_override.value(),
            min_p=self._minp_override.value(),
            model=self._model_combo.currentText().strip() or None,
            context_size=self._context_override.value(),
        )
        if self._regen_bump:
            preset = vary_seed(preset, self._regen_bump)
        return preset

    def _effective_context_size(self) -> int:
        """Context size in effect for token budgeting (the Ctx override)."""
        return self._context_override.value()

    # ---- override widget sync (Settings -> API/LLM tabs) ----

    def _refresh_model_combo(self, select: str = '') -> None:
        """Repopulate the model dropdown from the cached provider model list."""
        cached = load_provider_models(self._preset.name, self._preset.base_url)
        self._suppress_sampling_sync = True
        try:
            self._model_combo.clear()
            items = list(cached)
            if select and select not in items:
                items.append(select)
            self._model_combo.addItems(items)
            self._model_combo.setCurrentText(select)
        finally:
            self._suppress_sampling_sync = False

    def _set_sampling_widgets(
        self, temperature: float, min_p: float, context_size: int | None = None,
    ) -> None:
        """Programmatic widget update that skips the write-through sync."""
        self._suppress_sampling_sync = True
        try:
            self._temp_override.setValue(temperature)
            self._minp_override.setValue(min_p)
            if context_size is not None:
                self._context_override.setValue(int(context_size))
        finally:
            self._suppress_sampling_sync = False
        self._sampling_save_timer.stop()

    def _on_sampling_changed(self) -> None:
        if not self._suppress_sampling_sync:
            self._sampling_save_timer.start()

    def _flush_sampling_sync(self) -> None:
        """Write the current overrides into the active provider settings."""
        self._sampling_save_timer.stop()
        try:
            save_active_sampling(
                self._temp_override.value(), self._minp_override.value(),
                model=self._model_combo.currentText().strip() or None,
                context_size=self._context_override.value(),
            )
        except Exception:
            logger.exception("Failed to sync chat overrides to settings")

    # ---- UI ----

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)

        toolbar_row = QHBoxLayout()
        toolbar_row.setContentsMargins(8, 8, 20, 2)
        toolbar_row.addStretch()
        self._memory_btn = QPushButton('Memory')
        self._memory_btn.clicked.connect(self._on_memory)
        toolbar_row.addWidget(self._memory_btn)
        self._lore_btn = QPushButton('Lorebooks')
        self._lore_btn.setToolTip(
            'Choose standalone lorebooks whose matching entries are injected '
            'as world info into this chat.'
        )
        self._lore_btn.clicked.connect(self._on_lorebooks)
        toolbar_row.addWidget(self._lore_btn)
        self._context_btn = QPushButton('Prompt')
        self._context_btn.setToolTip(
            'Inspect the exact prompt the next request will send, block by '
            'block, with the token cost of each one.\n'
            "The Author's Note and Jailbreak tabs edit this chat's trailing "
            'instructions.'
        )
        self._context_btn.clicked.connect(self._on_context_inspect)
        toolbar_row.addWidget(self._context_btn)
        self._pairings_btn = QPushButton('Pairings')
        self._pairings_btn.setToolTip(
            'Save this character together with the persona you are chatting '
            'with, and jump back into it later.'
        )
        self._pairings_btn.clicked.connect(self._on_pairings)
        toolbar_row.addWidget(self._pairings_btn)
        self._auto_summarize_btn = QPushButton('Auto-Summarize')
        self._auto_summarize_btn.setCheckable(True)
        self._auto_summarize_btn.clicked.connect(self._on_auto_summarize_toggled)
        toolbar_row.addWidget(self._auto_summarize_btn)
        self._settings_btn = QPushButton('Settings')
        self._settings_btn.clicked.connect(self._on_settings)
        toolbar_row.addWidget(self._settings_btn)
        right_layout.addLayout(toolbar_row)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(8, 2, 20, 4)
        self._header = QLabel('Select a character to start testing')
        self._header.setStyleSheet('font-weight: bold; color: #e0e0e0;')
        header_row.addWidget(self._header)
        header_row.addStretch()
        right_layout.addLayout(header_row)

        self._chat_scroll = QScrollArea()
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._bubble_container = QWidget()
        self._bubble_layout = QVBoxLayout(self._bubble_container)
        self._bubble_layout.setContentsMargins(4, 4, 4, 4)
        self._bubble_layout.setSpacing(8)
        self._bubble_layout.addStretch()
        self._chat_scroll.setWidget(self._bubble_container)
        right_layout.addWidget(self._chat_scroll, 1)

        # Stick-to-bottom: follow new messages only while the viewport is
        # already at the bottom edge; scrolling up pauses the following.
        vbar = self._chat_scroll.verticalScrollBar()
        vbar.valueChanged.connect(self._update_at_bottom)
        vbar.rangeChanged.connect(self._follow_if_at_bottom)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(12, 2, 12, 0)
        self._token_label = QLabel('')
        self._token_label.setStyleSheet(f'color: {CONTEXT_COLOR_NORMAL};')
        status_row.addWidget(self._token_label)
        self._apply_font_sizes()
        status_row.addStretch()
        right_layout.addLayout(status_row)

        self._chips_container = QWidget()
        self._chips_layout = QHBoxLayout(self._chips_container)
        self._chips_layout.setContentsMargins(8, 0, 8, 0)
        self._chips_layout.setSpacing(4)
        self._chips_layout.addStretch()
        self._chips_container.setVisible(False)
        right_layout.addWidget(self._chips_container)

        self._input = ChatInputEdit()
        self._input.setPlaceholderText(
            'Type a message. Enter to send, Shift+Enter for a new line...'
        )
        self._input.setAcceptRichText(False)
        self._input.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        line_metrics = QFontMetrics(self._input.font())
        doc_margin = int(self._input.document().documentMargin())
        box_padding = self._input.frameWidth() * 2 + doc_margin * 2 + 4
        # Room for ~4 lines; long messages scroll inside the box.
        self._input.setFixedHeight(line_metrics.lineSpacing() * 4 + box_padding)
        self._input.send_requested.connect(self._on_send)
        message_row = QHBoxLayout()
        message_row.setContentsMargins(8, 4, 8, 0)
        message_row.addWidget(self._input, 1)
        right_layout.addLayout(message_row)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(8, 4, 8, 2)
        # The chat controls sit on the left of the input row, next to the
        # message buttons on the right.
        self._new_session_btn = QPushButton('New Chat')
        self._new_session_btn.setToolTip('Start a fresh conversation.')
        self._new_session_btn.clicked.connect(self._on_new_session)
        button_row.addWidget(self._new_session_btn)
        self._sessions_btn = QPushButton('Chats')
        self._sessions_btn.setToolTip(
            'Load, export, import, or delete this character\'s saved chats.'
        )
        self._sessions_btn.clicked.connect(self._on_sessions)
        button_row.addWidget(self._sessions_btn)
        button_row.addStretch()
        self._impersonate_btn = QPushButton('Impersonate')
        self._impersonate_btn.setToolTip(
            'Have the model write {{user}}\'s next message; it is added to '
            'the chat for you to edit or send.'
        )
        self._impersonate_btn.clicked.connect(self._on_impersonate)
        button_row.addWidget(self._impersonate_btn)
        self._continue_btn = QPushButton('Continue')
        self._continue_btn.setToolTip(
            'Continue the last reply instead of starting a new message.'
        )
        self._continue_btn.clicked.connect(self._on_continue)
        button_row.addWidget(self._continue_btn)
        self._attach_btn = QPushButton('Attach')
        self._attach_btn.clicked.connect(self._on_attach)
        button_row.addWidget(self._attach_btn)
        self._send_btn = QPushButton('Send')
        self._send_btn.clicked.connect(self._on_send)
        button_row.addWidget(self._send_btn)
        self._cancel_btn = QPushButton('Cancel')
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        button_row.addWidget(self._cancel_btn)
        right_layout.addLayout(button_row)

        settings_row = QHBoxLayout()
        settings_row.setContentsMargins(8, 2, 8, 8)
        small_style = 'color: #777;'
        model_label = QLabel('Model')
        model_label.setStyleSheet(small_style)
        settings_row.addWidget(model_label)
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setMinimumWidth(200)
        self._model_combo.setToolTip(
            'Model used for this chat; saved to Settings -> API when changed.\n'
            'The dropdown lists models cached from the endpoint - refresh it '
            'with Settings -> API -> Fetch Models.'
        )
        self._refresh_model_combo(self._preset.model)
        self._model_combo.currentTextChanged.connect(self._on_sampling_changed)
        settings_row.addWidget(self._model_combo)
        ctx_label = QLabel('Ctx')
        ctx_label.setStyleSheet(small_style)
        settings_row.addWidget(ctx_label)
        self._context_override = QSpinBox()
        self._context_override.setRange(512, 200000)
        self._context_override.setSingleStep(256)
        self._context_override.setValue(self._preset.context_size)
        self._context_override.setToolTip(
            'Context window size in tokens for the selected model; saved to '
            'Settings -> LLM when changed.\n'
            'Controls how much history fits before older messages are '
            'trimmed or summarized.'
        )
        self._context_override.valueChanged.connect(self._on_sampling_changed)
        settings_row.addWidget(self._context_override)
        temp_label = QLabel('Temp')
        temp_label.setStyleSheet(small_style)
        settings_row.addWidget(temp_label)
        self._temp_override = QDoubleSpinBox()
        self._temp_override.setRange(0.0, 2.0)
        self._temp_override.setSingleStep(0.05)
        self._temp_override.setDecimals(2)
        self._temp_override.setValue(self._preset.temperature)
        self._temp_override.setToolTip(
            'Sampling temperature for this chat; saved to Settings -> LLM '
            'when changed.\n'
            'Higher = more creative/varied, lower = more focused/accurate.'
        )
        self._temp_override.valueChanged.connect(self._on_sampling_changed)
        settings_row.addWidget(self._temp_override)
        minp_label = QLabel('Min-P')
        minp_label.setStyleSheet(small_style)
        settings_row.addWidget(minp_label)
        self._minp_override = QDoubleSpinBox()
        self._minp_override.setRange(0.0, 1.0)
        self._minp_override.setSingleStep(0.05)
        self._minp_override.setDecimals(2)
        self._minp_override.setValue(self._preset.min_p)
        self._minp_override.valueChanged.connect(self._on_sampling_changed)
        settings_row.addWidget(self._minp_override)
        self._refresh_min_p_tooltip()
        settings_row.addStretch()
        right_layout.addLayout(settings_row)

        layout.addWidget(right_container)

        self._input_widgets = [
            self._input, self._send_btn, self._attach_btn,
            self._new_session_btn, self._sessions_btn, self._memory_btn,
            self._context_btn, self._auto_summarize_btn,
            self._impersonate_btn, self._continue_btn,
            self._lore_btn,
        ]

    def _set_inputs_enabled(self, enabled: bool) -> None:
        for w in self._input_widgets:
            w.setEnabled(enabled)

    def _is_generating(self) -> bool:
        return self._generating

    # ---- card list ----

    def select_card(self, char_id: int) -> None:
        """Load a card for testing (save current session first)."""
        if char_id == self._current_id and self._card is not None:
            return
        if self._is_generating():
            # A response is streaming in for the current card; swapping the
            # card now would deliver the reply into the new card's session.
            QMessageBox.information(
                self, 'Test',
                'A response is still generating.\n'
                'Cancel it before switching characters.',
            )
            return
        self._save_current_session()
        self._load_card(char_id)

    def _load_card(self, char_id: int) -> None:
        entry = self.db.get_by_id(char_id)
        if not entry:
            return
        source = entry.get('source_path', '')
        if not source or not Path(source).exists():
            QMessageBox.warning(self, 'Test', 'Source file not found.')
            return
        raw = read_card_data(source)
        if not raw:
            QMessageBox.warning(self, 'Test', 'Could not read card data.')
            return

        self._current_id = char_id
        self._card = CharacterCard.from_spec_dict(raw, source)
        self._greeting_index = 0
        self._summarized_through = 0
        self._regen_bump = 0
        self._header.setTextFormat(Qt.TextFormat.PlainText)
        self._header.setText(f'Testing: {self._card.name}')

        sessions = self._store.list_sessions(char_id)
        if sessions:
            self._load_session_data(char_id, sessions[0]['id'])
        else:
            self._start_new_session()

        self._set_inputs_enabled(True)

    def _load_session_data(self, char_id: int, session_id: str) -> None:
        data = self._store.load_session(char_id, session_id)
        if not data:
            return
        self._session_id = session_id
        # A session file is on-disk data: it can be hand-edited, written by
        # another build, or left half-written.  ``dict(m)`` on a non-dict
        # message raised in a Qt slot, which aborts the process.
        self._messages = [
            dict(m) for m in (data.get('messages') or []) if isinstance(m, dict)
        ]
        self._memories = normalize_memories(data)
        self._summarized_through = 0
        self._regen_bump = 0
        # Restore the persisted Auto-Summarize / Auto-update toggles; chats
        # saved before the flags were persisted faithfully come back ON.
        self._auto_summarize, self._auto_update_attributes = session_flags(data)
        # Sessions saved before persistent attributes existed get the default
        # watched list so the tracker works out of the box; an explicit (even
        # empty) saved list is respected as-is.
        if data.get('attributes') is None:
            self._attributes = default_attributes()
        else:
            self._attributes = normalize_attributes(data)
        self._author_note = normalize_author_note(data.get('author_note'))
        self._jailbreak = data.get('jailbreak') or ''
        self._persona_id = load_active_persona_id()
        self._persona = get_persona(self._persona_id).get('text', '')
        self._sync_auto_toggle()
        self._update_memory_button()
        self._refresh_memory_dialog()
        self._update_prompt_buttons()
        self._at_bottom = True
        self._render_history()

    def _start_new_session(self) -> None:
        self._session_id = ChatSessionStore.new_session_id()
        self._messages = []
        self._memories = []
        self._attributes = default_attributes()
        self._auto_update_attributes = True
        self._greeting_index = 0
        self._summarized_through = 0
        self._regen_bump = 0
        self._author_note = normalize_author_note(None)
        self._jailbreak = ''
        self._persona_id = load_active_persona_id()
        self._persona = get_persona(self._persona_id).get('text', '')
        self._sync_auto_toggle()
        self._update_memory_button()
        self._update_prompt_buttons()
        if self._include_first and self._card is not None and self._card.first_mes:
                self._messages = build_initial_messages(
                    self._card, self._user_name, custom_macros=self._custom_macros,
                )
        self._at_bottom = True
        self._render_history()

    def _update_prompt_buttons(self) -> None:
        """Reflect note/jailbreak state on their toolbar button."""
        if getattr(self, '_context_btn', None) is not None:
            has_note = bool((self._author_note.get('text') or '').strip())
            has_jb = bool(self._jailbreak.strip())
            self._context_btn.setText('Prompt*' if (has_note or has_jb) else 'Prompt')

    def _sys_template(self):
        """The active system prompt template (Settings > Prompts)."""
        return resolve_template(active_template_id())

    def _ctx_template(self):
        """The active context template (Settings > Prompts)."""
        return context_templates.resolve_template(
            context_templates.active_context_template_id(),
        )

    def _on_new_session(self) -> None:
        if self._current_id is None:
            return
        if self._is_generating():
            # A response is streaming into the current chat; starting a
            # new one now would deliver the reply into the wrong session
            # file when it completes.
            QMessageBox.information(
                self, 'Test',
                'A response is still generating.\n'
                'Cancel it before starting a new chat.',
            )
            return
        self._save_current_session()
        self._start_new_session()
        self._set_inputs_enabled(True)

    def _on_sessions(self) -> None:
        if self._current_id is None:
            return
        if self._is_generating():
            QMessageBox.information(
                self, 'Test',
                'A response is still generating.\n'
                'Cancel it before switching chats.',
            )
            return
        self._save_current_session()
        sessions = self._store.list_sessions(self._current_id)
        dlg = _SessionsDialog(sessions, self)
        dlg.export_requested.connect(lambda sid: self._on_export_session(sid, dlg))
        dlg.import_requested.connect(self._on_import_st_chat)
        # Snapshot dialog state before deleteLater: exec() + WA_DeleteOnClose
        # would destroy the C++ object before the getters below run.
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        session_id = dlg.selected_id()
        deleted_ids = dlg.deleted_ids()
        dlg.deleteLater()
        if accepted and session_id:
            self._load_session_data(self._current_id, session_id)
        for deleted in deleted_ids:
            self._store.delete_session(self._current_id, deleted)
            if deleted == self._session_id:
                # The active session was deleted: clear the conversation too,
                # otherwise the next send re-saves the "deleted" messages
                # under a fresh id while the memories summarizing them are
                # silently lost.
                self._session_id = ChatSessionStore.new_session_id()
                self._messages = []
                self._memories = []
                self._attributes = default_attributes()
                self._auto_update_attributes = True
                self._render_history()
                self._update_token_label()
                self._sync_auto_toggle()
                self._update_memory_button()
                self._refresh_memory_dialog()

    def _on_settings(self) -> None:
        self._flush_sampling_sync()
        self.settings_requested.emit()

    # ---- lorebook (world info) injection ----

    def _reload_extra_books(self) -> None:
        """Re-read the active lorebooks from disk into the injection cache."""
        books: list[CharacterBook] = []
        for name in self._active_lore_names:
            book = lorebook_store.load_lorebook(name)
            if book is not None:
                books.append(book)
        self._extra_books = books
        self._update_lore_button()

    def reload_lorebooks(self) -> None:
        """Public hook: refresh the cached lorebooks after library changes."""
        self._active_lore_names = [
            n for n in self._active_lore_names
            if lorebook_store.load_lorebook(n) is not None
        ]
        save_active_lorebooks(self._active_lore_names)
        self._reload_extra_books()
        self._update_token_label()

    def _update_lore_button(self) -> None:
        if getattr(self, '_lore_btn', None) is None:
            return
        n = len(self._extra_books)
        self._lore_btn.setText(f'Lorebooks ({n})' if n else 'Lorebooks')

    def _on_lorebooks(self) -> None:
        available = lorebook_store.list_lorebooks()
        dlg = _LorebookSelectDialog(available, self._active_lore_names, self)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        selected = dlg.selected_filenames()
        dlg.deleteLater()
        if not accepted:
            return
        self._active_lore_names = selected
        save_active_lorebooks(selected)
        self._reload_extra_books()
        self._update_token_label()

    # ---- chat rendering ----

    def _clear_bubbles(self) -> None:
        self._stream_bubble = None
        while self._bubble_layout.count():
            item = self._bubble_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._bubbles.clear()
        self._bubble_layout.addStretch()

    def _set_bubble_controls_enabled(self, enabled: bool) -> None:
        for bubble in self._bubbles:
            bubble.set_controls_enabled(enabled)

    def _greeting_options(self) -> list[str]:
        if self._card is None:
            return []
        return [self._card.first_mes or ''] + [str(g) for g in self._card.alternate_greetings]

    def _should_show_greeting_selector(self) -> bool:
        if self._card is None:
            return False
        if not self._card.alternate_greetings:
            return False
        if not self._messages:
            return False
        return self._messages[0].get('role') == 'assistant'

    def _greeting_text(self, index: int) -> str:
        options = self._greeting_options()
        if not options:
            return ''
        index = max(0, min(index, len(options) - 1))
        return substitute_macros(
            options[index],
            self._user_name,
            self._card.name if self._card else '',
            self._custom_macros,
        )

    def _add_greeting_selector(self) -> None:
        container = QWidget()
        h = QHBoxLayout(container)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        label = QLabel('Greeting:')
        label.setStyleSheet(f'color: #777; font-size: {ui_font_px(-2)}px;')
        h.addWidget(label)
        combo = QComboBox()
        options = self._greeting_options()
        for i in range(len(options)):
            name = 'Default greeting' if i == 0 else f'Alternate greeting {i}'
            combo.addItem(name, options[i])
        combo.setCurrentIndex(max(0, min(self._greeting_index, len(options) - 1)))
        combo.activated.connect(self._on_greeting_selected)
        h.addWidget(combo)
        h.addStretch()
        self._bubble_layout.insertWidget(self._bubble_layout.count() - 1, container)

    def _on_greeting_selected(self, index: int) -> None:
        if index == self._greeting_index:
            return
        self._greeting_index = index
        if not self._messages or self._messages[0].get('role') != 'assistant':
            return
        first = self._messages[0]
        self._set_message_content(first, self._greeting_text(index))
        # Stored alternates were generated from the previous greeting.
        first.pop('variants', None)
        first.pop('variant_pos', None)
        self._render_history()
        self._save_current_session()

    def _add_bubble(self, index: int, msg: dict, show_regenerate: bool = False,
                    show_variant_back: bool = False,
                    show_variant_forward: bool = False,
                    variant_forward_new: bool = False) -> MessageBubble:
        role = msg.get('role', 'user')
        name = self._user_name if role == 'user' else (self._card.name if self._card else 'Assistant')
        bubble = MessageBubble(
            index=index,
            role=role,
            name=name,
            text=msg.get('content', ''),
            attachments=msg.get('attachments') or [],
            show_timestamp=self._show_timestamps,
            timestamp=msg.get('ts'),
            dialogue_color=self._dialogue_color,
            action_color=self._action_color,
            emphasis_color=self._emphasis_color,
            font_size=self._font_size,
            show_regenerate=show_regenerate,
            show_variant_back=show_variant_back,
            show_variant_forward=show_variant_forward,
            variant_forward_new=variant_forward_new,
            parent=self._bubble_container,
        )
        bubble.edit_requested.connect(self._on_edit_message)
        bubble.delete_requested.connect(self._on_delete_message)
        bubble.copy_requested.connect(self._on_copy_message)
        bubble.regenerate_requested.connect(self._on_regenerate_message)
        bubble.variant_back_requested.connect(self._on_variant_back)
        bubble.variant_forward_requested.connect(self._on_variant_forward)
        self._bubble_layout.insertWidget(self._bubble_layout.count() - 1, bubble)
        self._bubbles.append(bubble)
        return bubble

    @staticmethod
    def _variant_flags(msg: dict, is_last_assistant: bool) -> dict:
        """Footer flags for *msg* (regenerate/variant controls).

        They ride only the newest assistant message:  steps forward through
        tries, or (labelled New) generates past the newest.
        """
        variants = msg.get('variants') or []
        if not isinstance(variants, (list, tuple)):
            variants = []
        pos = _safe_variant_pos(msg.get('variant_pos'), len(variants) - 1)
        return {
            'show_regenerate': is_last_assistant,
            'show_variant_back': is_last_assistant and pos > 0,
            'show_variant_forward': is_last_assistant,
            'variant_forward_new': is_last_assistant and pos >= len(variants) - 1,
        }

    def _drop_stream_bubble(self) -> None:
        """Remove the streaming placeholder now that its message has landed."""
        bubble = self._stream_bubble
        self._stream_bubble = None
        if bubble is None:
            return
        if bubble in self._bubbles:
            self._bubbles.remove(bubble)
        self._bubble_layout.removeWidget(bubble)
        bubble.setParent(None)
        bubble.deleteLater()

    def _append_message_tail(self) -> None:
        """Show the newest message without rebuilding the whole chat.

        Sending and new replies only change the tail of the conversation, so
        appending one bubble replaces the O(n) teardown/rebuild that made long
        chats lag (and flash white) every time a message landed.
        """
        index = len(self._messages) - 1
        if index < 0 or len(self._bubbles) != index:
            # Out of sync (e.g. a leftover streaming placeholder): rebuild.
            self._render_history()
            return
        msg = self._messages[index]
        is_last_assistant = msg.get('role') == 'assistant'
        if is_last_assistant:
            # Retire the regenerate/variant buttons on the assistant message
            # that held them until now (they ride the newest reply only).
            for i in range(index - 1, -1, -1):
                if self._messages[i].get('role') == 'assistant':
                    self._bubbles[i].clear_actions()
                    break
        self._add_bubble(index, msg, **self._variant_flags(msg, is_last_assistant))
        self._update_token_label()
        self._maybe_scroll_to_bottom()

    def _render_history(self) -> None:
        bar = self._chat_scroll.verticalScrollBar()
        scroll_pos = bar.value()
        follow_bottom = self._at_bottom and self._auto_scroll
        # Repaints stay off for the teardown/rebuild: otherwise the emptied
        # viewport paints a blank frame first, so every re-render made the
        # chat flash white and long chats stuttered while widgets churned.
        self._rebuilding = True
        self._chat_scroll.setUpdatesEnabled(False)
        try:
            self._clear_bubbles()
            last_assistant_idx = -1
            for i, msg in enumerate(self._messages):
                if msg.get('role') == 'assistant':
                    last_assistant_idx = i
            for i, msg in enumerate(self._messages):
                is_last_assistant = (
                    msg.get('role') == 'assistant' and i == last_assistant_idx
                )
                if i == 0 and self._should_show_greeting_selector():
                    self._add_greeting_selector()
                self._add_bubble(i, msg, **self._variant_flags(msg, is_last_assistant))
            self._bubble_layout.activate()
        finally:
            self._rebuilding = False
            self._chat_scroll.setUpdatesEnabled(True)
        self._update_token_label()
        if follow_bottom:
            self._scroll_to_bottom()
        elif scroll_pos > 0:
            # Keep the reader's place: clearing the bubbles clamps the
            # scrollbar, so restore once the rebuilt layout has settled.
            def _restore() -> None:
                # The tab may be torn down before the timer fires: calling
                # into a destroyed scrollbar raises RuntimeError, and an
                # exception inside a slot aborts the whole process.
                if not _widget_alive(bar):
                    return
                try:
                    bar.setValue(min(scroll_pos, bar.maximum()))
                    self._update_at_bottom(bar.value())
                except RuntimeError:
                    pass

            QTimer.singleShot(0, _restore)

    def _append_error(self, text: str) -> None:
        short = text.splitlines()[0] if text else 'Unknown error'
        label = QLabel(
            f'<i><span style="color:#ff6666">Error: {html.escape(short)}</span></i>'
        )
        label.setWordWrap(True)
        self._bubble_layout.insertWidget(self._bubble_layout.count() - 1, label)
        self._maybe_scroll_to_bottom()

    def _conversation_text(self, messages: list[dict] | None = None) -> str:
        msgs = self._messages if messages is None else messages
        lines = []
        for m in msgs:
            role = self._user_name if m.get('role') == 'user' else (self._card.name if self._card else 'Assistant')
            lines.append(f"{role}: {m.get('content', '')}")
        return '\n'.join(lines)

    def _update_token_label(self) -> None:
        """Schedule a token recount (debounced; see _recount_tokens)."""
        timer = getattr(self, '_token_label_timer', None)
        if timer is None:
            self._recount_tokens()
            return
        timer.start()

    def _recount_tokens(self) -> None:
        if self._current_id is None or self._card is None:
            self._token_label.setText('')
            self._token_label_color = CONTEXT_COLOR_NORMAL
            self._apply_font_sizes()
            return
        # The plan is the exact payload the send path builds (system blocks,
        # example dialogue, history + depth injections, post-history and
        # jailbreak), so the label can't drift from what is actually sent.
        plan = build_context_plan(
            self._card,
            self._messages,
            memories=self._memories,
            attributes=self._attributes,
            user_name=self._user_name,
            custom_macros=self._custom_macros,
            persona=self._persona,
            extra_books=self._extra_books,
            example_placement=self._example_placement,
            author_note=self._author_note,
            jailbreak=self._jailbreak,
            context_template=self._ctx_template(),
        )
        total = plan.total_tokens
        ctx = self._effective_context_size()
        self._apply_font_sizes()
        if ctx:
            text = f'{total:,} / {ctx:,} tokens'
        else:
            text = f'{total:,} tokens'
        self._apply_context_pressure(text, total, ctx)

    def _apply_context_pressure(self, text: str, tokens: int, limit: int) -> None:
        """Style the token label according to how full the context window is.

        Below ~70 % of the window the label keeps its plain look, from there
        on it turns amber, and a prompt that does not fit turns red and says
        so.
        """
        level = context_pressure_level(tokens, limit)
        if level == 'over':
            self._token_label_color = CONTEXT_COLOR_OVER
            text = f'{text}{CONTEXT_OVER_SUFFIX}'
        elif level == 'warning':
            self._token_label_color = CONTEXT_COLOR_WARNING
        else:
            self._token_label_color = CONTEXT_COLOR_NORMAL
        self._token_label.setText(text)
        self._apply_font_sizes()

    def _update_at_bottom(self, value: int) -> None:
        if self._rebuilding:
            # Intermediate values while bubbles are torn down would fake a
            # "pinned to bottom" state and yank the scroll position.
            return
        bar = self._chat_scroll.verticalScrollBar()
        self._at_bottom = value >= bar.maximum() - 8

    def _follow_if_at_bottom(self, _minimum: int, _maximum: int) -> None:
        """Keep following new content while pinned to the bottom."""
        if self._rebuilding:
            return
        if self._at_bottom:
            self._scroll_to_bottom()

    def _maybe_scroll_to_bottom(self) -> None:
        """Scroll after new content only when auto-scroll is on and we're at bottom."""
        if self._auto_scroll and self._at_bottom:
            self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        self._at_bottom = True
        bar = self._chat_scroll.verticalScrollBar()

        def _scroll() -> None:
            # The tab can be torn down before the timer fires; a destroyed
            # scrollbar must not raise inside the slot (it would abort the
            # process).
            if not _widget_alive(bar):
                return
            try:
                bar.setValue(bar.maximum())
            except RuntimeError:
                pass

        QTimer.singleShot(0, _scroll)

    # ---- sending / streaming ----

    def _on_send(self) -> None:
        text = self._input.toPlainText().strip()
        if not text or self._current_id is None or self._is_generating():
            return
        attachments = list(self._pending_attachments)
        self._messages = append_message(
            self._messages, 'user', text,
            self._user_name, self._card.name if self._card else '',
            custom_macros=self._custom_macros,
        )
        if attachments:
            self._messages[-1]['attachments'] = attachments
        self._pending_attachments = []
        self._refresh_attachment_chips()
        self._input.clear()
        self._regen_bump = 0
        self._append_message_tail()
        self._save_current_session()
        self._start_generation()

    def _on_impersonate(self) -> None:
        """Have the model write {{user}}'s next message (added as a user turn)."""
        if self._current_id is None or self._is_generating():
            return
        self._regen_bump = 0
        self._start_generation(mode='user')

    def _on_continue(self) -> None:
        """Extend the last assistant reply instead of starting a new one."""
        if self._current_id is None or self._is_generating():
            return
        if not any(m.get('role') == 'assistant' for m in self._messages):
            self.status_message.emit('Nothing to continue.', 3000)
            return
        self._regen_bump = 0
        self._start_generation(mode='continue')

    def _start_generation(self, mode: str = 'assistant') -> None:
        preset = self._effective_preset()
        client = AIClient(preset)
        self._generating = True
        self._response_mode = mode
        # What the generation is billed to.  It is captured here (and not
        # read back when the reply arrives) so a selection change in
        # between cannot mislabel the generation.
        self._generation_meta = {
            'card_id': self._current_id,
            'card_name': self._card.name if self._card else '',
            'session_id': self._session_id or '',
            'provider': preset.name,
            'model': preset.model,
        }
        self._send_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._input.setEnabled(False)
        self._attach_btn.setEnabled(False)

        # Impersonate streams a user-side message; everything else replies
        # as the character.
        bubble_role = 'user' if mode == 'user' else 'assistant'
        self._stream_bubble = self._add_bubble(len(self._messages), {
            'role': bubble_role, 'content': '',
        })
        self._set_bubble_controls_enabled(False)
        # Explicit send/regenerate always shows the new exchange.
        self._scroll_to_bottom()

        system = self._assemble_system()
        api_history = self._examples_for_history() + self._messages
        injections = injections_for_chat(
            self._card,
            self._messages,
            author_note=self._author_note,
            extra_books=self._extra_books,
            user_name=self._user_name,
            custom_macros=self._custom_macros,
        )
        self._summarize_evicted(system, api_history, injections)
        ctx_template = self._ctx_template()
        jailbreak = self._jailbreak_text()
        mode_prompt = self._mode_prompt(mode)
        if mode_prompt:
            jailbreak = f'{jailbreak}\n\n{mode_prompt}' if jailbreak.strip() else mode_prompt
        if jailbreak.strip():
            jailbreak = context_templates.render_jailbreak(ctx_template, jailbreak)
        post_history = self._post_history_text()
        if post_history.strip():
            post_history = context_templates.render_post_history(
                ctx_template, post_history,
            )
        examples_block = (
            self._examples_block() if self._example_placement == 'post_history' else ''
        )
        self._worker = _ChatWorker(
            client, system, api_history, self,
            post_history=post_history,
            jailbreak=jailbreak,
            examples_block=examples_block,
            injections=injections,
            mode=mode,
            name_prefixes=context_templates.name_prefixes(
                ctx_template, self._user_name,
                self._card.name if self._card else '',
            ),
            conversation_start_header=ctx_template.conversation_start_header,
            card_id=self._generation_meta['card_id'],
            card_name=self._generation_meta['card_name'],
            session_id=self._generation_meta['session_id'],
            provider=self._generation_meta['provider'],
            model=self._generation_meta['model'],
        )
        self._worker.chunk.connect(self._append_streaming_chunk)
        self._worker.completed.connect(self._on_response)
        self._worker.error.connect(self._on_error)
        # Cleanup on the built-in signal so cancelled runs are still deleted.
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _mode_prompt(self, mode: str) -> str:
        """Trailing instruction for Impersonate / Continue ('' = none)."""
        if mode not in ('user', 'continue'):
            return ''
        from src.ai_prompts import load_prompt
        key = 'impersonate_user' if mode == 'user' else 'continue_user'
        return substitute_macros(
            load_prompt(key), self._user_name,
            self._card.name if self._card else '', self._custom_macros,
        )

    def _summarize_evicted(self, system: str, api_history: list[dict] | None = None,
                           injections: list | None = None) -> None:
        """Rolling-summary fallback for messages the context window evicts.

        Mirrors the trimming inside ``AIClient.generate_chat``: when older
        messages no longer fit, they are summarized into a chat-memory entry
        so the conversation keeps its continuity instead of hitting an
        amnesia cliff.  *api_history* is the exact history the request sends
        (examples + conversation); *injections* are reserved against the
        budget like in the real trim.
        """
        ctx = self._effective_context_size()
        if not ctx or not self._messages:
            return
        if api_history is None:
            api_history = list(self._messages)
        injections = list(injections or [])
        full: list[dict] = [{'role': 'system', 'content': system}]
        full.extend(history_for_api(api_history, context_templates.name_prefixes(
            self._ctx_template(), self._user_name,
            self._card.name if self._card else '',
        )))
        reserve = sum(count_tokens(i.content) for i in injections)
        _, dropped = split_for_context(
            full, ctx, self._preset.max_tokens, reserve=reserve,
        )
        leading_examples = max(0, len(api_history) - len(self._messages))
        evicted = max(0, len(dropped) - leading_examples)
        if evicted <= self._summarized_through:
            return
        char_name = self._card.name if self._card else 'Assistant'
        lines = []
        for m in self._messages[self._summarized_through:evicted]:
            who = self._user_name if m.get('role') == 'user' else char_name
            content = m.get('content', '')
            if content:
                lines.append(f'{who}: {content}')
        text = '\n'.join(lines).strip()
        if not text:
            self._summarized_through = evicted
            return
        started = self._start_summarize(
            text, source='auto', end_index=evicted - 1,
            prefix='[Earlier conversation] ', quiet=True,
        )
        if started:
            self._summarized_through = evicted

    def _append_streaming_chunk(self, text: str) -> None:
        if self._stream_bubble is not None:
            self._stream_bubble.append_stream(text)
        self._maybe_scroll_to_bottom()

    @pyqtSlot(str)
    def _on_response(self, response: str) -> None:
        response = clean_generated_text(
            response, self._user_name,
            self._card.name if self._card else '',
        )
        mode = getattr(self, '_response_mode', 'assistant')
        if mode == 'continue':
            self._finish_generation()
            self._append_continuation(response)
            self._save_current_session()
            return
        role = 'user' if mode == 'user' else 'assistant'
        self._messages = append_message(
            self._messages, role, response,
            self._user_name, self._card.name if self._card else '',
            custom_macros=self._custom_macros,
        )
        if role == 'assistant' and self._regen_tried is not None and self._messages:
            # A regenerate/try-again produced this reply: keep every earlier
            # attempt browsable and land on the newest one.
            tried = list(self._regen_tried)
            tried.append(response)
            self._messages[-1]['variants'] = tried
            self._messages[-1]['variant_pos'] = len(tried) - 1
            self._regen_tried = None
            self._regen_replaced = None
        self._drop_stream_bubble()
        self._append_message_tail()
        self._finish_generation()
        self._save_current_session()
        if role == 'assistant' and self._auto_summarize:
            self._start_summarize(self._last_exchange_text(), source='auto')
        if role == 'assistant' and self._auto_update_attributes and self._attributes:
            self._start_attribute_update(self._last_exchange_text())

    def _append_continuation(self, text: str) -> None:
        """Extend the last assistant message with a Continue result."""
        if not text.strip():
            self._render_history()
            return
        for msg in reversed(self._messages):
            if msg.get('role') == 'assistant':
                content = msg.get('content', '')
                joined = f'{content}{text}' if content.endswith(('\n', ' ', '\t')) or text.startswith(('\n', ' ', '\t')) else f'{content}\n{text}'
                self._set_message_content(msg, joined)
                break
        self._render_history()

    @pyqtSlot(str)
    def _on_error(self, error: str) -> None:
        self._finish_generation()
        self._render_history()
        self._append_error(error)
        logger.warning("Test chat error: %s", error)

    def _restore_interrupted_response(self) -> bool:
        """Re-insert the message a failed/cancelled retry removed.

        Returns True when a message was restored (caller should re-render).
        """
        replaced = self._regen_replaced
        if replaced is None:
            self._regen_tried = None
            return False
        self._regen_tried = None
        self._regen_replaced = None
        index, msg = replaced
        if len(self._messages) <= index:
            self._messages.insert(index, msg)
            return True
        return False

    def _finish_generation(self) -> None:
        self._generating = False
        self._response_mode = 'assistant'
        worker = self._worker
        self._worker = None
        if worker is not None:
            # The worker's run() may already have emitted its result when the
            # user presses Cancel; without disconnecting, the queued
            # completed/error would land after this call and append a second
            # reply next to the restored one.
            for signal, slot in (
                (worker.completed, self._on_response),
                (worker.error, self._on_error),
                (worker.chunk, self._append_streaming_chunk),
            ):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass   # never connected, or the object is already gone
            # The result is emitted from *inside* run(), so the thread can
            # still be a few instructions from returning: keep the worker
            # referenced until the thread has really finished.
            self._retire_worker(worker)
        # Remove the streaming placeholder widget too: only _on_response
        # dropped it before, so cancelling a generation left a phantom
        # bubble on screen (and one more bubble than messages) until some
        # later action happened to rebuild the history.
        self._drop_stream_bubble()
        restored = self._restore_interrupted_response()
        self._send_btn.setEnabled(True)
        self._input.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._attach_btn.setEnabled(True)
        self._set_bubble_controls_enabled(True)
        if restored:
            self._render_history()
        self._update_token_label()
        self._input.setFocus()

    def _retire_worker(self, worker: QThread | None) -> None:
        """Keep a reference to *worker* until its thread has really finished.

        ``completed`` / ``error`` are emitted from *inside* ``run()``, so the
        queued slot can run while the worker thread is still a few
        instructions from returning.  Dropping the last reference then let
        the QThread be destroyed while it was still running, which aborts
        the process ("QThread: Destroyed while thread is still running").
        The worker is released again once the thread reports that it
        finished.
        """
        if worker is None:
            return
        self._pending_workers = [
            pending for pending in self._pending_workers
            if self._worker_is_alive(pending)
        ]
        if not self._worker_is_alive(worker):
            # Already stopped: destroying it now is safe.
            return
        self._pending_workers.append(worker)
        worker.finished.connect(self._on_worker_finished)

    def _on_worker_finished(self) -> None:
        """Release the workers whose thread has stopped (GUI thread)."""
        self._pending_workers = [
            pending for pending in self._pending_workers
            if self._worker_is_alive(pending)
        ]

    def _on_cancel(self) -> None:
        if not (self._generating and self._worker_is_alive(self._worker)):
            self._finish_generation()
            return
        # Cooperative cancel; poll from the GUI thread instead of wait()
        # so the UI never blocks. _generating stays True until the worker
        # has actually stopped, which also prevents a second concurrent
        # generation while the old one winds down.
        self._worker.cancel()
        self._cancel_btn.setEnabled(False)
        if getattr(self, '_cancel_poll', None) is None:
            self._cancel_poll = QTimer(self)
            self._cancel_poll.setInterval(100)
            self._cancel_poll.timeout.connect(self._poll_cancel_finished)
        self._cancel_poll.start()

    def _poll_cancel_finished(self) -> None:
        if self._generating and self._worker_is_alive(self._worker):
            return
        poll = getattr(self, '_cancel_poll', None)
        if poll is not None:
            poll.stop()
            poll.deleteLater()
            self._cancel_poll = None
        self._finish_generation()

    # ---- message actions (edit / delete / copy / regenerate) ----

    def _on_edit_message(self, index: int) -> None:
        if index < 0 or index >= len(self._messages) or self._is_generating():
            return
        msg = self._messages[index]
        dlg = TextEditDialog(msg.get('content', ''), title='Edit Message', parent=self)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        new_text = dlg.text()
        dlg.deleteLater()
        if accepted:
            self._set_message_content(msg, new_text)
            self._render_history()
            self._save_current_session()

    def _on_delete_message(self, index: int) -> None:
        if index < 0 or index >= len(self._messages) or self._is_generating():
            return
        count = len(self._messages) - index
        if QMessageBox.question(
            self, 'Delete Messages',
            f'You are about to delete {count} message(s). Continue?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._messages = self._messages[:index]
        self._remove_memories_from(index)
        self._render_history()
        self._save_current_session()

    def _on_copy_message(self, index: int) -> None:
        if index < 0 or index >= len(self._messages):
            return
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._messages[index].get('content', ''))
        self.status_message.emit('Message copied to clipboard.', 3000)

    def _set_message_content(self, msg: dict, text: str) -> None:
        """Update a message's content, keeping its alternate list in sync."""
        msg['content'] = text
        variants = msg.get('variants')
        if isinstance(variants, list):
            pos = _safe_variant_pos(msg.get('variant_pos'), 0)
            if 0 <= pos < len(variants):
                variants[pos] = text

    def _on_regenerate_message(self, index: int) -> None:
        """Discard *index* (assistant) and all its alternates, then generate.

        Unlike ▶ (New), Regenerate replaces the whole try-level: every stored
        response for this slot is thrown away, leaving none to browse.
        """
        self._start_regen(index, keep_tries=False)

    def _start_regen(self, index: int, *, keep_tries: bool) -> None:
        if self._is_generating() or index < 0 or index >= len(self._messages):
            return
        msg = self._messages[index]
        if msg.get('role') != 'assistant':
            return
        if keep_tries:
            # Every previous response for this slot stays browsable,
            # including the one currently displayed.
            existing = msg.get('variants')
            self._regen_tried = list(existing) if existing else [msg.get('content', '')]
        else:
            # Full replacement: none of the stored responses survive.
            self._regen_tried = []
        self._regen_bump += 1
        # Stash so an interrupted run can restore the exchange untouched
        # (the disk copy stays whole until the new reply is saved).
        self._regen_replaced = (index, dict(msg))
        self._messages = self._messages[:index]
        self._remove_memories_from(index)
        self._render_history()
        self._start_generation()

    def _on_variant_back(self, index: int) -> None:
        self._navigate_variant(index, -1)

    def _on_variant_forward(self, index: int) -> None:
        msg = self._messages[index] if 0 <= index < len(self._messages) else None
        if msg is not None:
            variants = msg.get('variants') or []
            if not isinstance(variants, (list, tuple)):
                variants = []
            pos = _safe_variant_pos(msg.get('variant_pos'), len(variants) - 1)
            if pos < len(variants) - 1:
                # An older response is showing; step toward the newest.
                self._navigate_variant(index, 1)
                return
        # Already at the newest response: ▶ (New) generates a fresh
        # alternate while keeping the existing tries browsable.
        self._start_regen(index, keep_tries=True)

    def _navigate_variant(self, index: int, step: int) -> None:
        """Show the previous/next stored response for message *index*."""
        if self._is_generating() or index < 0 or index >= len(self._messages):
            return
        msg = self._messages[index]
        variants = msg.get('variants')
        if not isinstance(variants, list) or not variants:
            return
        pos = _safe_variant_pos(msg.get('variant_pos'), len(variants) - 1)
        new_pos = max(0, min(pos + step, len(variants) - 1))
        if new_pos == pos:
            return
        msg['variant_pos'] = new_pos
        msg['content'] = variants[new_pos]
        self._render_history()
        self._save_current_session()

    # ---- attachments ----

    def _on_attach(self) -> None:
        if self._is_generating():
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Attach Files', '',
            'Supported files (*.txt *.md *.json *.png *.jpg *.jpeg *.gif *.webp *.bmp);;'
            'Text files (*.txt *.md *.json);;'
            'Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;'
            'All files (*)',
        )
        for p in paths:
            att = prepare_attachment(Path(p))
            if att is None:
                self.status_message.emit(f'Could not attach: {Path(p).name}', 5000)
                continue
            self._pending_attachments.append(att)
        self._refresh_attachment_chips()

    def _refresh_attachment_chips(self) -> None:
        while self._chips_layout.count():
            item = self._chips_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for i, att in enumerate(self._pending_attachments):
            self._chips_layout.addWidget(self._make_chip(att, i))
        self._chips_layout.addStretch()
        self._chips_container.setVisible(bool(self._pending_attachments))

    def _make_chip(self, att: dict, index: int) -> QWidget:
        from PyQt6.QtWidgets import QFrame
        frame = QFrame()
        h = QHBoxLayout(frame)
        h.setContentsMargins(6, 2, 6, 2)
        h.setSpacing(4)
        kind = att.get('kind')
        name = html.escape(att.get('name', 'file'))
        color = '#9ad8ff' if kind == 'image' else '#9be8a0'
        icon = '🖼' if kind == 'image' else '📄'
        label = QLabel(f'<span style="color:{color};">{icon} {name}</span>')
        label.setTextFormat(Qt.TextFormat.RichText)
        h.addWidget(label)
        remove = QPushButton('×')
        remove.setFixedSize(18, 18)
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.clicked.connect(lambda _=False, idx=index: self._remove_attachment(idx))
        h.addWidget(remove)
        return frame

    def _remove_attachment(self, index: int) -> None:
        if 0 <= index < len(self._pending_attachments):
            del self._pending_attachments[index]
        self._refresh_attachment_chips()

    # ---- memory ----

    def _update_memory_button(self) -> None:
        if getattr(self, '_memory_btn', None) is None:
            return
        n = len(self._memories)
        self._memory_btn.setText(f'Memory ({n})' if n else 'Memory')

    def _remove_memories_from(self, index: int) -> None:
        """Drop memories that summarize messages at/after *index*."""
        if index < 0:
            return
        kept = drop_memories_from(self._memories, index)
        # Rolling-summary coverage can't extend past the truncation point.
        self._summarized_through = min(self._summarized_through, max(0, index))
        if len(kept) != len(self._memories):
            self._memories = kept
            self._update_memory_button()
            self._refresh_memory_dialog()
            self._update_token_label()

    def _sync_auto_toggle(self) -> None:
        if getattr(self, '_auto_summarize_btn', None) is not None:
            self._auto_summarize_btn.setChecked(self._auto_summarize)
        if self._memory_dialog is not None:
            self._memory_dialog.set_auto_summarize(self._auto_summarize)

    def _refresh_memory_dialog(self) -> None:
        if self._memory_dialog is not None:
            self._memory_dialog.set_memories(self._memories)
            self._memory_dialog.set_auto_summarize(self._auto_summarize)
            self._memory_dialog.set_attributes(self._attributes)
            self._memory_dialog.set_auto_update_attributes(self._auto_update_attributes)

    def _on_auto_summarize_toggled(self, checked: bool) -> None:
        self._auto_summarize = bool(checked)
        self._save_current_session()
        # Mirror into both controls so the toolbar button and the Memories
        # checkbox always show the same state, whichever one was clicked.
        self._sync_auto_toggle()

    def _on_memory(self) -> None:
        if self._current_id is None:
            return
        if self._memory_dialog is None:
            self._memory_dialog = MemoryDialog(
                self._memories, self._auto_summarize,
                self._attributes, self._auto_update_attributes, self,
            )
            self._memory_dialog.memories_changed.connect(self._on_memories_changed)
            self._memory_dialog.summarize_requested.connect(self._on_summarize_now)
            self._memory_dialog.auto_summarize_changed.connect(self._on_auto_summarize_toggled)
            self._memory_dialog.attributes_changed.connect(self._on_attributes_changed)
            self._memory_dialog.update_attributes_requested.connect(self._on_update_attributes_now)
            self._memory_dialog.auto_update_attributes_changed.connect(
                self._on_auto_update_attributes_toggled,
            )
        else:
            self._refresh_memory_dialog()
        self._memory_dialog.show()
        self._memory_dialog.raise_()
        self._memory_dialog.activateWindow()

    def _build_preview(self, author_note: dict, jailbreak: str):
        """Build the prompt preview of the next request.

        Mirrors the send path exactly: the same system assembly, the same
        trailing blocks (example dialogue, post-history instructions and
        the user's jailbreak) and the same trim, so the preview cannot
        drift from what the provider receives.
        """
        from src.prompt_preview import build_preview

        if self._card is None:
            return None
        template = self._sys_template()
        ctx_template = self._ctx_template()

        # The trailing blocks are rendered the way the send path renders
        # them, so the preview shows the final text.  The jailbreak comes
        # from the argument, not from the stored value: the Context
        # Inspector previews the *edited* text before it is applied.
        rendered_jailbreak = self._jailbreak_text(jailbreak)
        if rendered_jailbreak.strip():
            rendered_jailbreak = context_templates.render_jailbreak(
                ctx_template, rendered_jailbreak,
            )
        rendered_post_history = self._post_history_text()
        if rendered_post_history.strip():
            rendered_post_history = context_templates.render_post_history(
                ctx_template, rendered_post_history,
            )
        examples_block = (
            self._examples_block() if self._example_placement == 'post_history' else ''
        )

        note_text = str((author_note or {}).get('text', '') or '')
        return build_preview(
            self._card,
            self._messages,
            system_prompt=template.content,
            context_limit=self._effective_context_size() or 8192,
            max_tokens=self._preset.max_tokens,
            personas=[self._persona] if self._persona else [],
            memories=self._memories,
            attributes=self._attributes,
            author_note=author_note if isinstance(author_note, dict) else note_text,
            jailbreak=rendered_jailbreak,
            post_history=rendered_post_history,
            examples_block=examples_block,
            example_turns=self._examples_for_history(),
            extra_books=self._extra_books,
            user_name=self._user_name,
            name_prefixes=context_templates.name_prefixes(
                ctx_template, self._user_name,
                self._card.name if self._card else '',
            ),
            custom_macros=self._custom_macros,
            context_template=ctx_template,
        )

    def _on_context_inspect(self) -> None:
        """Show the exact prompt the next request will send.

        The dialog's Author's Note / Jailbreak tabs edit this chat's trailing
        instructions; OK applies them, Cancel discards.
        """
        if self._card is None:
            return

        def preview_factory(author_note: dict, jailbreak: str):
            return self._build_preview(author_note, jailbreak)

        preview = self._build_preview(self._author_note, self._jailbreak)
        if preview is None:
            return
        dialog = ContextInspectorDialog(
            preview,
            self._effective_context_size(),
            self,
            author_note=self._author_note,
            jailbreak=self._jailbreak,
            preview_factory=preview_factory,
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        note = dialog.author_note()
        jailbreak = dialog.jailbreak()
        dialog.deleteLater()
        if not accepted:
            return
        self._author_note = note
        self._jailbreak = jailbreak
        self._update_prompt_buttons()
        self._update_token_label()
        self._save_current_session()

    def _on_pairings(self) -> None:
        """Save / manage the card + persona pairings."""
        from src.persona_pairings import Pairing, list_pairings, save_pairing
        from src.ui.widgets.feature_dialogs import PairingsDialog

        if self._card is None:
            self.status_message.emit('Select a character first.', 3000)
            return

        existing = list_pairings()
        same_card = [p for p in existing if p.card_id == self._current_id]
        if same_card:
            # The pairing already exists: open the manager on it.
            dialog = PairingsDialog(self.db, self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.exec()
            return

        from datetime import datetime, timezone
        persona_name = ''
        try:
            from src.settings_manager import get_persona
            persona_name = str(get_persona(self._persona_id).get('name', '') or '')
        except Exception:
            persona_name = ''
        pairing = Pairing(
            id=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f'),
            card_id=self._current_id,
            card_name=self._card.name,
            persona_name=persona_name or 'Default',
            persona_text=self._persona or '',
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        save_pairing(pairing)
        self.status_message.emit(
            f'Saved the pairing "{self._card.name}" / {pairing.persona_name}.', 4000,
        )

    def _on_memories_changed(self, memories: list) -> None:
        self._memories = [dict(m) for m in memories]
        self._save_current_session()
        self._update_memory_button()
        self._update_token_label()

    def _on_summarize_now(self) -> None:
        conversation = self._conversation_text().strip()
        if not conversation:
            self.status_message.emit('Nothing to summarize.', 3000)
            return
        self._start_summarize(conversation, source='summary')

    def _last_exchange_text(self) -> str:
        """Text of the latest exchange for auto-summarization.

        Always pairs the newest assistant response with the *user* message
        that directly precedes it, so summaries cover both sides of the chat.
        """
        msgs = self._messages
        asst_idx = -1
        user_text = ''
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get('role') == 'assistant':
                asst_idx = i
                break
        assistant_text = ''
        if asst_idx >= 0:
            assistant_text = msgs[asst_idx].get('content', '')
            # The response's own turn: walk back to the user message that
            # preceded it (not just any earlier user message).
            for i in range(asst_idx - 1, -1, -1):
                if msgs[i].get('role') == 'user':
                    user_text = msgs[i].get('content', '')
                    break
        parts = []
        if user_text:
            parts.append(f"{self._user_name}: {user_text}")
        if assistant_text:
            parts.append(f"{self._card.name if self._card else 'Assistant'}: {assistant_text}")
        return '\n'.join(parts)

    def _start_summarize(
        self,
        text: str,
        source: str,
        end_index: int | None = None,
        prefix: str = '',
        quiet: bool = False,
    ) -> bool:
        """Summarize *text* into a memory entry.  Returns True when started.

        *end_index* records the last message index covered (defaults to the
        current last message); *prefix* is prepended to the stored content;
        *quiet* suppresses the error popup (used by background eviction
        summarization, which retries on the next turn anyway).
        """
        if self._summarize_worker is not None or not text.strip():
            return False
        from src.ai_prompts import build_memory_summary_prompts
        system, user = build_memory_summary_prompts(text)
        client = AIClient(self._effective_preset())
        self._summarize_source = source
        self._summarize_prefix = prefix
        if end_index is not None:
            self._summarize_end_index = end_index
        else:
            self._summarize_end_index = len(self._messages) - 1 if self._messages else None
        # Identity tag: if the user switches cards while the summarize
        # request is in flight, the result must not be filed into the new
        # card's session.
        self._summarize_char_id = self._current_id
        self._summarize_session_id = self._session_id
        self._set_summarizing(True)
        self._summarize_worker = _SummarizeWorker(client, system, user, self)
        self._summarize_worker.completed.connect(self._on_summary)
        self._summarize_worker.error.connect(self._on_summary_error)
        # Cleanup on the built-in signal so cancelled runs are still deleted.
        self._summarize_worker.finished.connect(self._summarize_worker.deleteLater)
        self._summarize_quiet = quiet
        self._summarize_worker.start()
        return True

    def _set_summarizing(self, active: bool) -> None:
        if self._memory_dialog is not None:
            self._memory_dialog.set_summarizing(active)

    @pyqtSlot(str)
    def _on_summary(self, summary: str) -> None:
        self._retire_worker(self._summarize_worker)
        self._summarize_worker = None
        self._set_summarizing(False)
        if (self._summarize_char_id != self._current_id
                or self._summarize_session_id != self._session_id):
            logger.info("Discarding stale summarize result for card %s", self._summarize_char_id)
            return
        content = summary.strip()
        if content:
            prefix = getattr(self, '_summarize_prefix', '')
            self._memories.append(new_memory_entry(
                f'{prefix}{content}' if prefix else content,
                source=self._summarize_source,
                end_index=self._summarize_end_index,
            ))
            self._save_current_session()
            self._update_memory_button()
            self._update_token_label()
            self._refresh_memory_dialog()
            self.status_message.emit('Memory updated.', 3000)

    @pyqtSlot(str)
    def _on_summary_error(self, error: str) -> None:
        self._retire_worker(self._summarize_worker)
        self._summarize_worker = None
        self._set_summarizing(False)
        quiet = getattr(self, '_summarize_quiet', False)
        if quiet:
            logger.warning("Background eviction summarize failed: %s", error.splitlines()[0] if error else error)
            return
        short = error.splitlines()[0] if error else 'Unknown error'
        QMessageBox.critical(self, 'Summarize Error', short)

    # ---- persistent attributes ----

    def _start_attribute_update(self, text: str, quiet: bool = True) -> bool:
        """Re-derive persistent attributes from *text*.  Returns True when started.

        *quiet* (per-turn auto-update) only logs failures; an explicit
        "Update Attributes Now" run reports them in a popup.
        """
        if (
            self._attr_worker is not None
            or not text.strip()
            or not any(a.get('enabled', True) for a in self._attributes)
        ):
            return False
        from src.ai_prompts import build_attribute_update_prompts
        system, user = build_attribute_update_prompts(self._attributes, text)
        client = AIClient(self._effective_preset())
        # Identity tag: results must not land in another card's session if
        # the user switches cards while the request is in flight.
        self._attr_char_id = self._current_id
        self._attr_session_id = self._session_id
        self._attr_quiet = quiet
        self._set_updating_attributes(True)
        self._attr_worker = _SummarizeWorker(client, system, user, self)
        self._attr_worker.completed.connect(self._on_attribute_update)
        self._attr_worker.error.connect(self._on_attribute_update_error)
        # Cleanup on the built-in signal so cancelled runs are still deleted.
        self._attr_worker.finished.connect(self._attr_worker.deleteLater)
        self._attr_worker.start()
        return True

    def _set_updating_attributes(self, active: bool) -> None:
        if self._memory_dialog is not None:
            self._memory_dialog.set_updating_attributes(active)

    @pyqtSlot(str)
    def _on_attribute_update(self, result: str) -> None:
        self._retire_worker(self._attr_worker)
        self._attr_worker = None
        self._set_updating_attributes(False)
        if (self._attr_char_id != self._current_id
                or self._attr_session_id != self._session_id):
            logger.info("Discarding stale attribute update for card %s", self._attr_char_id)
            return
        updates = parse_attribute_updates(
            result,
            [a.get('name', '') for a in self._attributes if a.get('enabled', True)],
        )
        if not updates:
            if not getattr(self, '_attr_quiet', True):
                self.status_message.emit('No attribute changes.', 3000)
            return
        self._attributes = apply_attribute_updates(self._attributes, updates)
        self._save_current_session()
        self._update_token_label()
        self._refresh_memory_dialog()
        self.status_message.emit('Attributes updated.', 3000)

    @pyqtSlot(str)
    def _on_attribute_update_error(self, error: str) -> None:
        self._retire_worker(self._attr_worker)
        self._attr_worker = None
        self._set_updating_attributes(False)
        if getattr(self, '_attr_quiet', True):
            logger.warning(
                "Background attribute update failed: %s",
                error.splitlines()[0] if error else error,
            )
            return
        short = error.splitlines()[0] if error else 'Unknown error'
        QMessageBox.critical(self, 'Attribute Update Error', short)

    def _on_update_attributes_now(self) -> None:
        conversation = self._conversation_text().strip()
        if not conversation:
            self.status_message.emit('Nothing to update attributes from.', 3000)
            return
        self._start_attribute_update(conversation, quiet=False)

    def _on_attributes_changed(self, attributes: list) -> None:
        self._attributes = [dict(a) for a in attributes]
        self._save_current_session()
        self._update_token_label()

    def _on_auto_update_attributes_toggled(self, checked: bool) -> None:
        self._auto_update_attributes = bool(checked)
        self._save_current_session()

    # ---- export ----

    def _on_export_session(self, session_id: str, parent: QWidget | None = None) -> None:
        """Export one saved chat (selected in the Chats dialog) to a file.

        The destination format comes from the save dialog's file-type filter.
        """
        if self._current_id is None:
            return
        data = self._store.load_session(self._current_id, session_id) or {}
        messages = [m for m in data.get('messages') or [] if isinstance(m, dict)]
        if not messages:
            self.status_message.emit('That chat is empty - nothing to export.', 3000)
            return
        memories = normalize_memories(data)
        author_note = normalize_author_note(data.get('author_note'))
        raw_jb = data.get('jailbreak')
        jailbreak = raw_jb.strip() if isinstance(raw_jb, str) else ''
        base = (self._card.name if self._card else 'chat').replace(' ', '_')
        path, selected_filter = QFileDialog.getSaveFileName(
            parent or self, 'Export Chat', base + '.txt', _EXPORT_FILE_FILTER,
        )
        if not path:
            return
        path, fmt = _resolve_export_format(path, selected_filter)
        title = data.get('title') or auto_title(messages, fallback=base)
        try:
            if fmt == 'json':
                payload = {
                    'title': title,
                    'memories': memories,
                    'auto_summarize': bool(data.get('auto_summarize', False)),
                    'attributes': normalize_attributes(data),
                    'auto_update_attributes': bool(data.get('auto_update_attributes', False)),
                    'author_note': author_note,
                    'jailbreak': jailbreak,
                    'messages': messages,
                }
                Path(path).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8',
                )
            elif fmt == 'jsonl':
                Path(path).write_text(
                    st_chat_io.export_st_chat(
                        messages,
                        user_name=self._user_name,
                        char_name=self._card.name if self._card else 'Assistant',
                        chat_name=title,
                    ),
                    encoding='utf-8',
                )
            elif fmt == 'md':
                from src.chat_export import session_to_markdown
                Path(path).write_text(
                    session_to_markdown(
                        messages,
                        card_name=self._card.name if self._card else '',
                        title=title,
                    ),
                    encoding='utf-8',
                )
            elif fmt == 'html':
                from src.chat_export import session_to_html
                Path(path).write_text(
                    session_to_html(
                        messages,
                        card_name=self._card.name if self._card else '',
                        title=title,
                    ),
                    encoding='utf-8',
                )
            else:
                Path(path).write_text(self._conversation_text(messages), encoding='utf-8')
            self.status_message.emit(f'Exported chat to {path}', 5000)
        except OSError as e:
            QMessageBox.critical(parent or self, 'Export', f'Failed to export: {e}')

    def _on_import_st_chat(self) -> None:
        """Import a SillyTavern ``.jsonl`` chat as a fresh session."""
        if self._current_id is None:
            self.status_message.emit('Select a character first.', 3000)
            return
        if self._is_generating():
            QMessageBox.information(
                self, 'Test',
                'A response is still generating.\n'
                'Cancel it before importing.',
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, 'Import SillyTavern Chat', '',
            'SillyTavern chats (*.jsonl);;All files (*)',
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as e:
            QMessageBox.critical(self, 'Import', f'Failed to read chat file: {e}')
            return
        messages = st_chat_io.import_st_chat(text)
        if not messages:
            self.status_message.emit('No messages found in that chat file.', 5000)
            return
        self._save_current_session()
        self._session_id = ChatSessionStore.new_session_id()
        self._messages = messages
        self._memories = []
        self._attributes = default_attributes()
        self._auto_update_attributes = True
        self._greeting_index = 0
        self._summarized_through = 0
        self._regen_bump = 0
        self._author_note = normalize_author_note(None)
        self._jailbreak = ''
        self._persona_id = load_active_persona_id()
        self._persona = get_persona(self._persona_id).get('text', '')
        self._sync_auto_toggle()
        self._update_memory_button()
        self._update_prompt_buttons()
        self._at_bottom = True
        self._render_history()
        self._save_current_session()
        self.status_message.emit(f'Imported {len(messages)} message(s).', 5000)

    # ---- persistence ----

    def _save_current_session(self) -> None:
        if self._current_id is None or self._session_id is None:
            return
        title = auto_title(self._messages, fallback=(self._card.name if self._card else 'New chat'))
        self._store.save_session(
            self._current_id, self._session_id, title, self._messages,
            memories=self._memories,
            attributes=self._attributes,
            auto_summarize=self._auto_summarize,
            auto_update_attributes=self._auto_update_attributes,
            author_note=self._author_note,
            jailbreak=self._jailbreak,
            persona_id=self._persona_id,
        )

    def refresh_font_size(self) -> None:
        """Re-apply the app font size to the chat bubbles and token counter."""
        self._font_size = load_font_size()
        self._apply_font_sizes()
        self._render_history()

    def _apply_font_sizes(self) -> None:
        small = ui_font_px(-2)
        if getattr(self, '_token_label', None) is not None:
            # The colour follows the context pressure, so it is kept in an
            # attribute instead of being hard-coded here.
            color = getattr(self, '_token_label_color', CONTEXT_COLOR_NORMAL)
            self._token_label.setStyleSheet(f'color: {color}; font-size: {small}px;')

    @staticmethod
    def _worker_is_alive(worker) -> bool:
        """True when *worker* exists and its C++ object hasn't been deleted."""
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def cleanup_workers(self, timeout_ms: int = 3000) -> bool:
        if self._generating and self._worker_is_alive(self._worker):
            # The retried message was removed from the live list; restore it
            # before saving so a shutdown mid-generation loses nothing.
            self._restore_interrupted_response()
        self._save_current_session()
        # One shared deadline for every worker: waiting on each of them
        # with the full timeout in turn could block the shutdown for
        # 3 x timeout_ms.
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        if self._generating and self._worker_is_alive(self._worker):
            try:
                self._worker.cancel()
                if not self._wait_worker(self._worker, deadline):
                    return False
            except RuntimeError:
                pass
        for worker in (self._summarize_worker, self._attr_worker):
            if not self._worker_is_alive(worker):
                continue
            try:
                worker.cancel()
                if not self._wait_worker(worker, deadline):
                    return False
            except RuntimeError:
                pass
        # Workers whose result was already handled but whose thread may
        # still be running: destroying them now would abort the process.
        for worker in list(self._pending_workers):
            if not self._worker_is_alive(worker):
                continue
            try:
                if not self._wait_worker(worker, deadline):
                    return False
            except RuntimeError:
                pass
        return True

    def _wait_worker(self, worker: QThread, deadline: float) -> bool:
        """Wait for *worker* until *deadline*; True when it stopped."""
        remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
        return bool(worker.wait(remaining_ms))

    def current_preset(self) -> APIPreset:
        return self._preset

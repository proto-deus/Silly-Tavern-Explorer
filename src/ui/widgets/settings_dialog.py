from __future__ import annotations

import json
import logging
import traceback

import requests
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src import ai_prompts
from src.ai_client import PRESETS, preset_from_saved
from src.settings_manager import (
    load_active_provider,
    load_macro_settings,
    load_provider_settings,
    load_test_settings,
    load_user_persona,
    save_all_provider_settings,
    save_macro_settings,
    save_test_settings,
    save_user_persona,
    set_active_provider,
)

logger = logging.getLogger(__name__)


class _FetchCancelled(Exception):
    """Internal: the user cancelled an in-flight model fetch."""


# Parentless workers are tracked here so the Python wrapper (and therefore
# the C++ QThread) cannot be garbage-collected while run() is executing —
# even if the settings dialog is destroyed first.
_LIVE_FETCH_WORKERS: list[_FetchModelsWorker] = []


class _FetchModelsWorker(QThread):
    # Named ``completed`` so the built-in QThread.finished stays available
    # for lifetime management (deleteLater).
    completed = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, base_url: str, api_key: str):
        super().__init__()
        self.base_url = base_url
        self.api_key = api_key
        self._cancelled = False

    def cancel(self) -> None:
        # Cooperative cancellation: checked before/after connect and between
        # body chunks. (requests.Response has no abort(); streaming + flag
        # checks is the supported way to interrupt promptly.)
        self._cancelled = True

    def _check_cancel(self) -> None:
        if self._cancelled:
            raise _FetchCancelled()

    def run(self):
        try:
            self._check_cancel()
            url = self.base_url.rstrip('/') + '/models'
            headers = {'Content-Type': 'application/json'}
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'
            resp = requests.get(url, headers=headers, timeout=15, stream=True)
            try:
                self._check_cancel()
                resp.raise_for_status()
                payload = bytearray()
                for chunk in resp.iter_content(chunk_size=65536):
                    self._check_cancel()
                    payload.extend(chunk)
            finally:
                resp.close()
            data = json.loads(payload.decode('utf-8', errors='replace'))
            models = []
            if isinstance(data, dict) and 'data' in data:
                for m in data['data']:
                    mid = m.get('id', '')
                    if mid:
                        models.append(mid)
            elif isinstance(data, list):
                for m in data:
                    if isinstance(m, str):
                        models.append(m)
                    elif isinstance(m, dict):
                        mid = m.get('id', '')
                        if mid:
                            models.append(mid)
            models.sort()
            self.completed.emit(models)
        except _FetchCancelled:
            return
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")


class _APIPanel(QWidget):
    """API connection settings (provider, URL, key, model)."""

    provider_changed = pyqtSignal(str)

    def __init__(self, names: list[str], active: str, connection: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self._fetch_worker: _FetchModelsWorker | None = None
        layout = QVBoxLayout(self)

        preset_group = QGroupBox('Active Provider')
        preset_row = QHBoxLayout(preset_group)
        self._preset_combo = QComboBox()
        self._preset_combo.addItems(names)
        self._preset_combo.setCurrentText(active)
        self._preset_combo.currentTextChanged.connect(self.provider_changed)
        preset_row.addWidget(QLabel('Provider:'))
        preset_row.addWidget(self._preset_combo)
        preset_row.addStretch()
        layout.addWidget(preset_group)

        conn_group = QGroupBox('Connection')
        form = QFormLayout(conn_group)
        self._url_edit = QLineEdit(connection.get('base_url', ''))
        form.addRow('Base URL:', self._url_edit)

        self._key_edit = QLineEdit(connection.get('api_key', ''))
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('API Key:', self._key_edit)

        model_row = QHBoxLayout()
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setMinimumWidth(250)
        if connection.get('model'):
            self._model_combo.setCurrentText(connection['model'])
        model_row.addWidget(self._model_combo)
        self._fetch_btn = QPushButton('Fetch Models')
        self._fetch_btn.clicked.connect(self._fetch_models)
        model_row.addWidget(self._fetch_btn)
        form.addRow('Model:', model_row)

        self._status_label = QLabel('')
        self._status_label.setStyleSheet('color: #aaa; font-size: 11px;')
        form.addRow('', self._status_label)
        layout.addWidget(conn_group)
        layout.addStretch()

    def load_connection(self, connection: dict) -> None:
        self._url_edit.setText(connection.get('base_url', ''))
        self._key_edit.setText(connection.get('api_key', ''))
        self._model_combo.clear()
        model = connection.get('model', '')
        if model:
            self._model_combo.setCurrentText(model)

    @staticmethod
    def _fetch_worker_is_alive(worker) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            # The C++ QThread was already destroyed by deleteLater.
            return False

    def _fetch_models(self) -> None:
        base_url = self._url_edit.text().strip()
        if not base_url:
            self._status_label.setText('Enter a base URL first.')
            return
        # Invalidate any in-flight fetch for a previous URL: its stale
        # result must not repopulate the model list for this one.
        if self._fetch_worker_is_alive(self._fetch_worker):
            self._fetch_worker.cancel()
        api_key = self._key_edit.text().strip()
        self._fetch_btn.setEnabled(False)
        self._status_label.setText('Fetching models...')
        self._fetch_request_url = base_url
        # Parentless + registry-tracked: the thread can never be destroyed
        # while running, even if this dialog closes first.
        self._fetch_worker = _FetchModelsWorker(base_url, api_key)
        _LIVE_FETCH_WORKERS.append(self._fetch_worker)
        self._fetch_worker.finished.connect(self._release_fetch_worker)
        self._fetch_worker.completed.connect(self._on_models_fetched)
        self._fetch_worker.error.connect(self._on_models_error)
        # Cleanup on the built-in signal so aborted runs are still deleted.
        self._fetch_worker.finished.connect(self._fetch_worker.deleteLater)
        self._fetch_worker.start()

    def _release_fetch_worker(self) -> None:
        try:
            _LIVE_FETCH_WORKERS.remove(self.sender())
        except ValueError:
            pass

    def _on_models_fetched(self, models: list[str]) -> None:
        self._fetch_btn.setEnabled(True)
        # Drop stale results: the user may have changed the URL/provider
        # while this fetch was in flight.
        if getattr(self, '_fetch_request_url', None) != self._url_edit.text().strip():
            return
        if not models:
            self._status_label.setText('No models found.')
            return
        current = self._model_combo.currentText()
        self._model_combo.clear()
        self._model_combo.addItems(models)
        if current and current in models:
            self._model_combo.setCurrentText(current)
        elif models:
            self._model_combo.setCurrentIndex(0)
        self._status_label.setText(f'Found {len(models)} model(s).')

    def _on_models_error(self, error: str) -> None:
        self._fetch_btn.setEnabled(True)
        short = error.splitlines()[0] if error else 'Unknown error'
        if len(short) > 100:
            short = short[:97] + '...'
        self._status_label.setText(f'Error: {short}')
        logger.warning("Model fetch error: %s", error)

    def cleanup(self) -> None:
        if self._fetch_worker_is_alive(self._fetch_worker):
            # Cooperative cancel shortens (usually eliminates) the wait; the
            # parentless worker cleans itself up via the built-in finished
            # signal and outlives the dialog safely if it needs longer.
            self._fetch_worker.cancel()
            self._fetch_worker.wait(2000)

    def connection_values(self) -> dict:
        return {
            'preset_name': self._preset_combo.currentText(),
            'base_url': self._url_edit.text().strip(),
            'api_key': self._key_edit.text().strip(),
            'model': self._model_combo.currentText().strip(),
        }


class _LLMPanel(QWidget):
    """Sampling / context parameters."""

    def __init__(self, config: dict, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        group = QGroupBox('Sampling Parameters')
        form = QFormLayout(group)

        self._temp = self._double(config.get('temperature', 0.8), 0.0, 2.0, 0.1)
        form.addRow('Temperature:', self._temp)

        self._top_p = self._double(config.get('top_p', 1.0), 0.0, 1.0, 0.05)
        form.addRow('Top P:', self._top_p)

        self._top_k = QSpinBox()
        self._top_k.setRange(0, 200)
        self._top_k.setValue(config.get('top_k', 0))
        form.addRow('Top K (0 = off):', self._top_k)

        self._min_p = self._double(config.get('min_p', 0.0), 0.0, 1.0, 0.05)
        form.addRow('Min P (0 = off):', self._min_p)

        self._context_size = QSpinBox()
        self._context_size.setRange(512, 200000)
        self._context_size.setSingleStep(256)
        self._context_size.setValue(config.get('context_size', 8192))
        form.addRow('Context Size (tokens):', self._context_size)

        self._max_tokens = QSpinBox()
        self._max_tokens.setRange(1, 32768)
        self._max_tokens.setSingleStep(256)
        self._max_tokens.setValue(config.get('max_tokens', 2048))
        form.addRow('Output Length (max tokens):', self._max_tokens)

        self._freq_pen = self._double(config.get('frequency_penalty', 0.0), -2.0, 2.0, 0.1)
        form.addRow('Frequency Penalty:', self._freq_pen)

        self._pres_pen = self._double(config.get('presence_penalty', 0.0), -2.0, 2.0, 0.1)
        form.addRow('Presence Penalty:', self._pres_pen)

        self._seed = QSpinBox()
        self._seed.setRange(-1, 2147483647)
        self._seed.setValue(config.get('seed', -1))
        form.addRow('Seed (-1 = random):', self._seed)

        self._retries = QSpinBox()
        self._retries.setRange(0, 5)
        self._retries.setValue(config.get('retry_attempts', 2))
        self._retries.setToolTip(
            'Automatic retries for failed API requests (connection errors, '
            'timeouts, rate limits, server errors) with exponential backoff.'
        )
        form.addRow('Retries on failure:', self._retries)

        layout.addWidget(group)
        layout.addStretch()

    @staticmethod
    def _double(value: float, lo: float, hi: float, step: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        spin.setDecimals(2)
        spin.setValue(value)
        return spin

    def values(self) -> dict:
        return {
            'temperature': self._temp.value(),
            'top_p': self._top_p.value(),
            'top_k': self._top_k.value(),
            'min_p': self._min_p.value(),
            'context_size': self._context_size.value(),
            'max_tokens': self._max_tokens.value(),
            'frequency_penalty': self._freq_pen.value(),
            'presence_penalty': self._pres_pen.value(),
            'seed': self._seed.value(),
            'retry_attempts': self._retries.value(),
        }

    def load_values(self, config: dict) -> None:
        self._temp.setValue(config.get('temperature', 1.0))
        self._top_p.setValue(config.get('top_p', 1.0))
        self._top_k.setValue(config.get('top_k', 0))
        self._min_p.setValue(config.get('min_p', 0.0))
        self._context_size.setValue(config.get('context_size', 8192))
        self._max_tokens.setValue(config.get('max_tokens', 2048))
        self._freq_pen.setValue(config.get('frequency_penalty', 0.0))
        self._pres_pen.setValue(config.get('presence_penalty', 0.0))
        self._seed.setValue(config.get('seed', -1))
        self._retries.setValue(config.get('retry_attempts', 2))


class _MacrosPanel(QWidget):
    """{{user}} value + persona + custom macro overrides."""

    def __init__(
        self,
        user_name: str,
        custom_macros: dict[str, str],
        persona: str = '',
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        user_group = QGroupBox('User Macro')
        user_form = QFormLayout(user_group)
        self._user_edit = QLineEdit(user_name)
        user_form.addRow('{{user}}:', self._user_edit)

        from PyQt6.QtWidgets import QPlainTextEdit
        self._persona_edit = QPlainTextEdit(persona)
        self._persona_edit.setPlaceholderText(
            "Describe who {{user}} is (background, appearance, role) so the "
            "character responds to a real person, not a nameless stranger..."
        )
        self._persona_edit.setFixedHeight(80)
        user_form.addRow('User persona:', self._persona_edit)
        layout.addWidget(user_group)

        custom_group = QGroupBox('Custom Macros')
        custom_layout = QVBoxLayout(custom_group)
        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(['Macro', 'Value'])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        custom_layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton('Add')
        add_btn.clicked.connect(self._add_row)
        btn_row.addWidget(add_btn)
        remove_btn = QPushButton('Remove Selected')
        remove_btn.clicked.connect(self._remove_selected)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        custom_layout.addLayout(btn_row)
        layout.addWidget(custom_group)

        for name, value in (custom_macros or {}).items():
            self._add_row(name, value)
        layout.addStretch()

    def _add_row(self, name: str = '', value: str = '') -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(name))
        self._table.setItem(row, 1, QTableWidgetItem(value))

    def _remove_selected(self) -> None:
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()}, reverse=True)
        for row in rows:
            self._table.removeRow(row)

    def values(self) -> dict:
        custom: dict[str, str] = {}
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, 0)
            value_item = self._table.item(row, 1)
            name = (name_item.text() if name_item else '').strip()
            value = (value_item.text() if value_item else '')
            if name:
                custom[name] = value
        return {
            'user_name': self._user_edit.text().strip(),
            'macros': custom,
            'persona': self._persona_edit.toPlainText().strip(),
        }


class _PromptsPanel(QWidget):
    """Embedded prompt template editor with a dropdown prompt selector."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        from src.ui.widgets.prompt_settings_dialog import _PromptTab

        layout = QVBoxLayout(self)

        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel('Edit prompt:'))
        self._prompt_combo = QComboBox()
        self._prompt_combo.setMinimumWidth(180)
        selector_row.addWidget(self._prompt_combo)
        selector_row.addStretch()
        layout.addLayout(selector_row)

        self._stack = QStackedWidget()
        prompts = [
            ('Tags', ['tags_system', 'tags_user']),
            ('Missing Tags', ['missing_tags_system', 'missing_tags_user']),
            ('Summary', ['summary_system', 'summary_user']),
            ('Alt Greetings', ['alt_greetings_system', 'alt_greetings_user']),
            ('Fill Fields', ['fill_system', 'fill_user']),
            ('Wizard', ['wizard_system', 'wizard_user']),
            ('Chat', ['chat_system']),
            ('Memory', ['memory_summary_system', 'memory_summary_user']),
            ('Lorebook', ['lorebook_system', 'lorebook_user']),
            ('Lorebook Entry', ['lorebook_entry_system', 'lorebook_entry_user']),
        ]
        for name, keys in prompts:
            self._prompt_combo.addItem(name)
            self._stack.addWidget(_PromptTab(name, keys))
        self._prompt_combo.currentIndexChanged.connect(self._stack.setCurrentIndex)
        layout.addWidget(self._stack)

    def save(self) -> None:
        for index in range(self._stack.count()):
            tab = self._stack.widget(index)
            for key, value in tab.get_templates().items():
                ai_prompts.save_prompt(key, value)


class _TestPanel(QWidget):
    """Test-tab display / history options."""

    def __init__(self, settings: dict, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        group = QGroupBox('Chat Display')
        form = QFormLayout(group)

        self._dialogue_color = self._color_field(settings.get('dialogue_color', '#9ad8ff'))
        form.addRow('Dialogue ("text") color:', self._dialogue_color[0])

        self._action_color = self._color_field(settings.get('action_color', '#e0a060'))
        form.addRow('Action (*text*) color:', self._action_color[0])

        self._emphasis_color = self._color_field(settings.get('emphasis_color', '#9be8a0'))
        form.addRow('Emphasis (_text_) color:', self._emphasis_color[0])

        layout.addWidget(group)

        from PyQt6.QtWidgets import QCheckBox

        options_group = QGroupBox('Options')
        options_layout = QVBoxLayout(options_group)
        self._show_timestamps = QCheckBox('Show message timestamps')
        self._show_timestamps.setChecked(bool(settings.get('show_timestamps', False)))
        options_layout.addWidget(self._show_timestamps)

        self._auto_scroll = QCheckBox('Auto-scroll to latest message')
        self._auto_scroll.setChecked(bool(settings.get('auto_scroll', True)))
        options_layout.addWidget(self._auto_scroll)

        self._include_first = QCheckBox('Include first message as greeting')
        self._include_first.setChecked(bool(settings.get('include_first_message', True)))
        options_layout.addWidget(self._include_first)
        layout.addWidget(options_group)
        layout.addStretch()

    def _color_field(self, initial: str):
        edit = QLineEdit(initial)
        pick_btn = QPushButton('Choose...')
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit)
        h.addWidget(pick_btn)
        pick_btn.clicked.connect(lambda: self._pick_color(edit))
        return (row, edit, pick_btn)

    def _pick_color(self, edit: QLineEdit) -> None:
        color = QColorDialog.getColor(QColor(edit.text()), self)
        if color.isValid():
            edit.setText(color.name())

    def values(self) -> dict:
        return {
            'dialogue_color': self._dialogue_color[1].text().strip(),
            'action_color': self._action_color[1].text().strip(),
            'emphasis_color': self._emphasis_color[1].text().strip(),
            'show_timestamps': self._show_timestamps.isChecked(),
            'auto_scroll': self._auto_scroll.isChecked(),
            'include_first_message': self._include_first.isChecked(),
        }


class SettingsDialog(QDialog):
    """Master settings dialog with API / LLM / Macros / Prompts / Test tabs."""

    settings_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Settings')
        self.setMinimumWidth(600)
        self.setMinimumHeight(560)

        from dataclasses import asdict

        names = list(PRESETS.keys())
        active = load_active_provider()
        if active not in names:
            active = 'LM Studio'

        self._configs: dict[str, dict] = {}
        for name in names:
            raw = load_provider_settings(name)
            preset = preset_from_saved(raw)
            self._configs[name] = asdict(preset)

        self._current_name = active

        macro = load_macro_settings()
        test = load_test_settings()
        persona = load_user_persona()

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._api_panel = _APIPanel(names, active, self._configs[active])
        self._llm_panel = _LLMPanel(self._configs[active])
        self._macros_panel = _MacrosPanel(
            macro.get('user_name', 'User'), macro.get('macros', {}), persona,
        )
        self._prompts_panel = _PromptsPanel()
        self._test_panel = _TestPanel(test)
        self._tabs.addTab(self._api_panel, 'API')
        self._tabs.addTab(self._llm_panel, 'LLM')
        self._tabs.addTab(self._macros_panel, 'Macros')
        self._tabs.addTab(self._prompts_panel, 'Prompts')
        self._tabs.addTab(self._test_panel, 'Test')
        layout.addWidget(self._tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._api_panel.provider_changed.connect(self._on_provider_changed)

    def _on_provider_changed(self, new_name: str) -> None:
        if not new_name:
            return
        old = self._current_name
        if old and old != new_name:
            self._configs[old] = self._collect_current()
        self._current_name = new_name
        cfg = self._configs.get(new_name, {})
        self._api_panel.load_connection(cfg)
        self._llm_panel.load_values(cfg)

    def _collect_current(self) -> dict:
        cfg = self._api_panel.connection_values()
        cfg.pop('preset_name', None)
        cfg.update(self._llm_panel.values())
        return cfg

    def _save(self) -> None:
        self._configs[self._current_name] = self._collect_current()
        save_all_provider_settings(self._configs)
        set_active_provider(self._current_name)

        macro = self._macros_panel.values()
        test = self._test_panel.values()
        save_macro_settings(macro['user_name'], macro['macros'])
        save_user_persona(macro.get('persona', ''))
        self._prompts_panel.save()
        save_test_settings(test)
        logger.info("Settings saved")
        self.settings_changed.emit()
        self.accept()

    def closeEvent(self, event) -> None:
        self._api_panel.cleanup()
        super().closeEvent(event)

    def reject(self) -> None:
        self._api_panel.cleanup()
        super().reject()

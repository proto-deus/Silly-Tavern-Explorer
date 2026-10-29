from __future__ import annotations

import json
import logging
import traceback

import requests
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QColor, QStandardItemModel
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
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
from src.provider_capabilities import (
    rejects_unknown_parameters,
    unsupported_settings,
)
from src.settings_manager import (
    apply_logging_enabled,
    load_active_persona_id,
    load_active_provider,
    load_macro_settings,
    load_personas,
    load_provider_models,
    load_provider_settings,
    load_spellcheck_enabled,
    load_test_settings,
    save_active_persona_id,
    save_all_provider_settings,
    save_font_size,
    save_macro_settings,
    save_logging_enabled,
    save_personas,
    save_provider_models,
    save_spellcheck_enabled,
    save_test_settings,
    save_user_persona,
    set_active_provider,
)

logger = logging.getLogger(__name__)

#: Display names of the settings whose support depends on the provider.
_SETTING_NAMES: dict[str, str] = {
    'top_p': 'Top P',
    'top_k': 'Top K',
    'min_p': 'Min P',
    'typical_p': 'Typical P',
    'min_tokens_to_keep': 'Min tokens to keep',
    'frequency_penalty': 'Frequency Penalty',
    'presence_penalty': 'Presence Penalty',
    'repeat_penalty': 'Repetition penalty',
    'mirostat': 'Mirostat mode',
    'mirostat_tau': 'Mirostat tau',
    'mirostat_eta': 'Mirostat eta',
    'dry_multiplier': 'DRY multiplier',
    'dry_base': 'DRY base',
    'dry_allowed_length': 'DRY allowed length',
    'dry_penalty_range': 'DRY penalty range',
    'xtc_threshold': 'XTC threshold',
    'xtc_probability': 'XTC probability',
    'dynatemp_range': 'Dynatemp range',
    'dynatemp_exponent': 'Dynatemp exponent',
    'smoothing_factor': 'Smoothing factor',
    'seed': 'Seed',
    'stop': 'Stop sequences',
}

#: The optional settings grouped by the feature they belong to, so the
#: summary can name "DRY" instead of four separate rows.
_SETTING_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('Top K', ('top_k',)),
    ('Min P', ('min_p',)),
    ('Typical P', ('typical_p',)),
    ('Min tokens to keep', ('min_tokens_to_keep',)),
    ('Repetition penalty', ('repeat_penalty',)),
    ('DRY', (
        'dry_multiplier', 'dry_base', 'dry_allowed_length', 'dry_penalty_range',
    )),
    ('Mirostat', ('mirostat', 'mirostat_tau', 'mirostat_eta')),
    ('XTC', ('xtc_threshold', 'xtc_probability')),
    ('Dynamic temperature', ('dynatemp_range', 'dynatemp_exponent')),
    ('Smoothing factor', ('smoothing_factor',)),
    ('Frequency Penalty', ('frequency_penalty',)),
    ('Presence Penalty', ('presence_penalty',)),
    ('Seed', ('seed',)),
    ('Stop sequences', ('stop',)),
)


def _unsupported_summary(unsupported: set[str]) -> list[str]:
    """Name the unsupported settings, grouped by the feature they belong to.

    A provider that misses a whole feature is named by it ("DRY"); one that
    misses only part of it gets the individual settings.
    """
    names: list[str] = []
    covered: set[str] = set()
    for name, members in _SETTING_GROUPS:
        hit = [m for m in members if m in unsupported]
        if not hit:
            continue
        covered.update(hit)
        if len(hit) == len(members):
            names.append(name)
        else:
            names.extend(_SETTING_NAMES.get(m, m) for m in hit)
    for setting in sorted(unsupported - covered):
        names.append(_SETTING_NAMES.get(setting, setting))
    return names


class _FetchCancelled(Exception):
    """Internal: the user cancelled an in-flight model fetch."""


# Parentless workers are tracked here so the Python wrapper (and therefore
# the C++ QThread) cannot be garbage-collected while run() is executing —
# even if the settings dialog is destroyed first.
_LIVE_FETCH_WORKERS: list[_FetchModelsWorker] = []

#: The model list is read into memory in full, so its size is capped: the
#: endpoint is user-configured, and a broken one could stream gigabytes.
_MAX_MODELS_BODY_BYTES = 16 * 1024 * 1024


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
                # Same rule as ai_client: trim, and refuse values requests
                # would reject with an InvalidHeader (whose message embeds
                # the whole header value, i.e. the key).
                key = self.api_key.strip()
                if '\r' in key or '\n' in key:
                    self.error.emit(
                        'The API key contains invalid characters. '
                        'Check the key in Settings.'
                    )
                    return
                try:
                    key.encode('latin-1')
                except UnicodeEncodeError:
                    self.error.emit(
                        'The API key contains characters that cannot be sent. '
                        'Re-enter it in Settings.'
                    )
                    return
                headers['Authorization'] = f'Bearer {key}'
            resp = requests.get(url, headers=headers, timeout=15, stream=True)
            try:
                self._check_cancel()
                resp.raise_for_status()
                payload = bytearray()
                for chunk in resp.iter_content(chunk_size=65536):
                    self._check_cancel()
                    payload.extend(chunk)
                    # The body is unbounded as far as the server is
                    # concerned: a hostile or broken endpoint could stream
                    # gigabytes into the worker's memory.
                    if len(payload) > _MAX_MODELS_BODY_BYTES:
                        raise ValueError(
                            'The model list is too large to read; the endpoint '
                            'returned more than '
                            f'{_MAX_MODELS_BODY_BYTES // (1024 * 1024)} MiB.',
                        )
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
            # The raw exception (and its traceback) can carry the request URL
            # with userinfo and the Authorization header value; send only a
            # redacted one-line message to the UI and log the details under
            # the same redaction.
            from src.ai_client import redact_secrets
            logger.error(
                "Model fetch failed: %s", redact_secrets(f"{e}\n{traceback.format_exc()}"),
            )
            self.error.emit(redact_secrets(str(e)))


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

        # Shown when a stored key exists but could not be decrypted: the field
        # reads as empty, which would otherwise look like "no key was ever
        # saved" and tempt the user to save over the real ciphertext.
        self._key_warning = QLabel('')
        self._key_warning.setWordWrap(True)
        self._key_warning.setStyleSheet('color: #e0a030; ')
        self._key_warning.setVisible(False)
        form.addRow('', self._key_warning)

        # Shown when the base URL would send the API key over cleartext http
        # to a remote host (loopback is exempt: that is the local-server case).
        self._url_warning = QLabel('')
        self._url_warning.setWordWrap(True)
        self._url_warning.setStyleSheet('color: #e0a030; ')
        self._url_warning.setVisible(False)
        form.addRow('', self._url_warning)

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
        self._status_label.setStyleSheet('color: #aaa; ')
        form.addRow('', self._status_label)
        layout.addWidget(conn_group)
        layout.addStretch()

        self._url_edit.textChanged.connect(self._refresh_url_warning)
        self._key_edit.textChanged.connect(self._refresh_url_warning)
        self._refresh_url_warning()

    def load_connection(self, connection: dict) -> None:
        self._url_edit.setText(connection.get('base_url', ''))
        self._key_edit.setText(connection.get('api_key', ''))
        provider = self._preset_combo.currentText()
        cached = load_provider_models(provider, connection.get('base_url', ''))
        self._populate_model_combo(cached, connection.get('model', ''))
        self._refresh_key_warning()
        self._refresh_url_warning()

    def _populate_model_combo(self, models: list[str], current: str) -> None:
        """Fill the model combo from the cached list, keeping *current* visible."""
        self._model_combo.clear()
        items = list(models)
        if current and current not in items:
            items.append(current)
        self._model_combo.addItems(items)
        if current:
            self._model_combo.setCurrentText(current)
        elif items:
            self._model_combo.setCurrentIndex(0)

    def _refresh_key_warning(self) -> None:
        """Surface a stored-but-undecryptable API key.

        ``_read_api_key`` returns '' when decryption fails (leaving the
        ciphertext intact), so without this the user would see an empty field,
        assume nothing was saved, and hit Save - which is exactly the path that
        used to destroy the key.
        """
        from src.settings_manager import is_provider_undecryptable
        preset = self._preset_combo.currentText()
        if is_provider_undecryptable(preset):
            self._key_warning.setText(
                "This provider's saved key could not be decrypted. It has been "
                "left untouched - re-enter the key and Save to replace it."
            )
            self._key_warning.setVisible(True)
        else:
            self._key_warning.setVisible(False)

    def _refresh_url_warning(self) -> None:
        """Warn when the API key would travel over cleartext http."""
        from src.ai_client import is_cleartext_remote
        base_url = self._url_edit.text().strip()
        if not base_url or not is_cleartext_remote(base_url):
            self._url_warning.setVisible(False)
            return
        if self._key_edit.text().strip():
            self._url_warning.setText(
                'This base URL is plain http:// to a remote host: the API key '
                'would be sent unencrypted. Prefer https:// for non-local '
                'servers.'
            )
        else:
            # Even without a key the card description, the chat history and
            # the persona travel over the same connection - that is often the
            # more sensitive payload, so it must not be sent silently.
            self._url_warning.setText(
                'This base URL is plain http:// to a remote host: your prompts '
                'and chat history would be sent unencrypted. Prefer https:// '
                'for non-local servers.'
            )
        self._url_warning.setVisible(True)

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
        # Drop wrappers of workers that already finished so the registry
        # cannot grow without bound across repeated fetches.
        _LIVE_FETCH_WORKERS[:] = [w for w in _LIVE_FETCH_WORKERS if not w.isFinished()]
        api_key = self._key_edit.text().strip()
        self._fetch_btn.setEnabled(False)
        self._status_label.setText('Fetching models...')
        self._fetch_request_url = base_url
        self._fetch_request_provider = self._preset_combo.currentText()
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
        worker = self.sender()
        try:
            _LIVE_FETCH_WORKERS.remove(worker)
        except ValueError:
            pass
        if worker is getattr(self, '_fetch_worker', None):
            self._fetch_worker = None

    def _is_current_fetch(self) -> bool:
        """True when the emitting worker is the one for the current request.

        A newer fetch overwrites the request context, so a late result from a
        superseded worker must not be cached or shown as its own.
        """
        worker = self.sender()
        return worker is None or worker is getattr(self, '_fetch_worker', None)

    def _on_models_fetched(self, models: list[str]) -> None:
        if not self._is_current_fetch():
            return
        self._fetch_btn.setEnabled(True)
        # Drop stale results: the user may have changed the URL/provider
        # while this fetch was in flight.
        if getattr(self, '_fetch_request_url', None) != self._url_edit.text().strip():
            return
        if not models:
            self._status_label.setText('No models found.')
            return
        # Cache the list so the combo (and the Test tab) can be populated
        # without re-fetching; Fetch Models only refreshes it.
        provider = getattr(self, '_fetch_request_provider', '') or self._preset_combo.currentText()
        try:
            save_provider_models(provider, models, self._fetch_request_url)
        except Exception:
            logger.exception("Failed to cache fetched model list")
        self._populate_model_combo(models, self._model_combo.currentText())
        self._status_label.setText(f'Found {len(models)} model(s).')

    def _on_models_error(self, error: str) -> None:
        if self._is_current_fetch():
            self._fetch_btn.setEnabled(True)
        from src.ai_client import redact_secrets
        error = redact_secrets(error)
        short = error.splitlines()[0] if error else 'Unknown error'
        if len(short) > 100:
            short = short[:97] + '...'
        if self._is_current_fetch():
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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        layout = QVBoxLayout(container)

        # The settings the selected provider does not implement, so the user
        # is not left wondering why a slider does no effect.
        self._rows: dict[str, tuple[QLabel, QWidget, str, str]] = {}
        self._provider = ''
        self._grey_unsupported = QCheckBox(
            'Grey out settings this provider does not support'
        )
        self._grey_unsupported.setChecked(True)
        self._grey_unsupported.setToolTip(
            'The backends do not agree on which parameters they implement. '
            'Unsupported settings are left out of the request either way; '
            'tick this off to keep editing them (for instance to prepare a '
            'profile for another provider).'
        )
        self._grey_unsupported.toggled.connect(self._apply_row_states)
        layout.addWidget(self._grey_unsupported)
        self._provider_note = QLabel('')
        self._provider_note.setWordWrap(True)
        self._provider_note.setStyleSheet('color: #e0a030;')
        self._provider_note.setVisible(False)
        layout.addWidget(self._provider_note)

        # Sampler widget definitions.  The optional ones are sent only when
        # set to a non-neutral value, and only when the active provider
        # implements them at all - the llama.cpp-family samplers below only
        # work on the local servers. The layout is assembled further down,
        # with every sampler in one section grouped by type.
        self._temp = self._double(config.get('temperature', 0.8), 0.0, 2.0, 0.1)

        self._top_p = self._double(config.get('top_p', 1.0), 0.0, 1.0, 0.05)
        self._top_k = QSpinBox()
        self._top_k.setRange(0, 200)
        self._top_k.setValue(config.get('top_k', 0))
        self._min_p = self._double(config.get('min_p', 0.0), 0.0, 1.0, 0.05)
        self._context_size = QSpinBox()
        self._context_size.setRange(512, 200000)
        self._context_size.setSingleStep(256)
        self._context_size.setValue(config.get('context_size', 8192))
        self._max_tokens = QSpinBox()
        self._max_tokens.setRange(1, 32768)
        self._max_tokens.setSingleStep(256)
        self._max_tokens.setValue(config.get('max_tokens', 2048))
        self._freq_pen = self._double(config.get('frequency_penalty', 0.0), -2.0, 2.0, 0.1)
        self._pres_pen = self._double(config.get('presence_penalty', 0.0), -2.0, 2.0, 0.1)
        self._seed = QSpinBox()
        self._seed.setRange(-1, 2147483647)
        self._seed.setValue(config.get('seed', -1))
        self._retries = QSpinBox()
        self._retries.setRange(0, 5)
        self._retries.setValue(config.get('retry_attempts', 2))
        self._retries.setToolTip(
            'Automatic retries for failed API requests (connection errors, '
            'timeouts, rate limits, server errors) with exponential backoff.'
        )
        self._stop = QLineEdit()
        stop_value = config.get('stop', [])
        if isinstance(stop_value, (list, tuple)):
            stop_value = ', '.join(str(s) for s in stop_value)
        self._stop.setText(str(stop_value or ''))
        self._stop.setPlaceholderText('e.g. <|user|>, <|end_of_turn|>')
        self._stop.setToolTip(
            'Custom stopping strings (comma-separated): generation stops as '
            'soon as one of them is produced, and the text up to it is kept.'
        )
        self._strict_template = QCheckBox(
            'Enforce alternating user/assistant roles (Llama 3.x templates)'
        )
        self._strict_template.setChecked(bool(config.get('strict_chat_template', False)))
        self._strict_template.setToolTip(
            'Rewrites chat requests into one system message followed by '
            'strictly alternating user/assistant turns.\n'
            'Required by Llama 3.x / Gemma chat templates, which reject '
            'trailing system blocks (post-history instructions, jailbreak) '
            'and system messages injected mid-history.\n'
            'Without it, such requests are still repaired automatically after '
            'the server rejects them; enable this to skip the failed attempt.'
        )

        # llama.cpp-spelled repetition penalty.
        self._repeat_pen = self._double(
            config.get('repeat_penalty', 1.0), 1.0, 3.0, 0.05,
        )
        self._repeat_pen.setToolTip(
            'Penalty on tokens that already appeared in the prompt/reply '
            '(1.0 = off). Around 1.1 curbs loops; high values cause gibberish.\n'
            'Sent as "repeat_penalty" to the llama.cpp family and as '
            '"repetition_penalty" to the services that use that spelling.'
        )
        # The advanced samplers.  They only work on the local llama.cpp-family
        # servers; every other provider leaves them out of the request.
        self._mirostat = QSpinBox()
        self._mirostat.setRange(0, 2)
        self._mirostat.setValue(config.get('mirostat', 0))
        self._mirostat.setToolTip(
            'Mirostat sampling: 0 = off, 1 = Mirostat v1, 2 = Mirostat v2.\n'
            'Controls output perplexity toward a target (tau).'
        )
        self._mirostat_tau = self._double(
            config.get('mirostat_tau', 5.0), 0.0, 10.0, 0.5,
        )
        self._mirostat_tau.setToolTip(
            'Target entropy for Mirostat (tau). Lower = tighter, more '
            'predictable output. Only sent when Mirostat is enabled.'
        )
        self._mirostat_eta = self._double(
            config.get('mirostat_eta', 0.1), 0.0, 1.0, 0.01,
        )
        self._mirostat_eta.setToolTip(
            'Mirostat learning rate (eta). Higher = faster correction toward '
            'the target entropy. Only sent when Mirostat is enabled.'
        )
        self._dry_multiplier = self._double(
            config.get('dry_multiplier', 0.0), 0.0, 10.0, 0.05,
        )
        self._dry_multiplier.setToolTip(
            'DRY ("Don\'t Repeat Yourself") multiplier; 0 = off.\n'
            'Penalizes tokens that would continue a repeated sequence.'
        )
        self._dry_base = self._double(config.get('dry_base', 1.75), 1.0, 10.0, 0.05)
        self._dry_base.setToolTip(
            'DRY base for the exponential penalty growth per repeated token. '
            'Only sent when the DRY multiplier is enabled.'
        )
        self._dry_allowed_length = QSpinBox()
        self._dry_allowed_length.setRange(0, 100)
        self._dry_allowed_length.setValue(config.get('dry_allowed_length', 2))
        self._dry_allowed_length.setToolTip(
            'How many tokens may repeat before the DRY penalty kicks in. '
            'Only sent when the DRY multiplier is enabled.'
        )
        self._dry_penalty_range = QSpinBox()
        self._dry_penalty_range.setRange(0, 10000)
        self._dry_penalty_range.setValue(config.get('dry_penalty_range', 0))
        self._dry_penalty_range.setToolTip(
            'How far back (in tokens) DRY looks for repeats; 0 = the whole '
            'context. The llama.cpp servers count the tokens they scan back '
            'instead, so 0 becomes their own default of 320.\n'
            'Only sent when the DRY multiplier is enabled.'
        )
        self._xtc_threshold = self._double(
            config.get('xtc_threshold', 0.1), 0.0, 1.0, 0.01,
        )
        self._xtc_threshold.setToolTip(
            'XTC cuts all tokens above this probability before sampling. '
            'Only sent when XTC probability is enabled.'
        )
        self._xtc_probability = self._double(
            config.get('xtc_probability', 0.0), 0.0, 1.0, 0.05,
        )
        self._xtc_probability.setToolTip(
            'Chance (0-1) of applying XTC ("Exclude Top Choices") on each '
            'step; 0 = off. Pushes output away from the safest tokens.'
        )
        self._typical_p = self._double(config.get('typical_p', 1.0), 0.0, 1.0, 0.05)
        self._typical_p.setToolTip(
            'Typical P sampling (1.0 = off): keeps tokens whose information '
            'content is close to the expected value.'
        )
        self._dynatemp_range = self._double(
            config.get('dynatemp_range', 0.0), 0.0, 5.0, 0.05,
        )
        self._dynatemp_range.setToolTip(
            'Dynamic temperature range; 0 = off.\n'
            'Scales temperature to the entropy of each step: confident steps '
            'cool down, uncertain ones heat up.'
        )
        self._dynatemp_exponent = self._double(
            config.get('dynatemp_exponent', 1.0), 0.0, 10.0, 0.1,
        )
        self._dynatemp_exponent.setToolTip(
            'Shape of the dynamic temperature curve. Only sent when the '
            'dynatemp range is enabled.'
        )
        self._smoothing_factor = self._double(
            config.get('smoothing_factor', 0.0), 0.0, 10.0, 0.05,
        )
        self._smoothing_factor.setToolTip(
            'Quadratic smoothing of the logits (0 = off): flattens the '
            'distribution before sampling for more varied output.'
        )
        self._min_tokens_to_keep = QSpinBox()
        self._min_tokens_to_keep.setRange(1, 100)
        self._min_tokens_to_keep.setValue(config.get('min_tokens_to_keep', 1))
        self._min_tokens_to_keep.setToolTip(
            'Minimum tokens kept when a sampler truncates the distribution, '
            'so generation can never get completely stuck.'
        )
        # Every sampler in one section, grouped by type.
        sampling = QGroupBox('Sampling Parameters')
        sampling_layout = QVBoxLayout(sampling)
        sampling_layout.addWidget(self._group('Temperature shaping', [
            ('temperature', 'Temperature:', self._temp),
            ('dynatemp_range', 'Dynatemp range (0 = off):', self._dynatemp_range),
            ('dynatemp_exponent', 'Dynatemp exponent:', self._dynatemp_exponent),
            ('smoothing_factor', 'Smoothing factor (0 = off):', self._smoothing_factor),
        ]))
        sampling_layout.addWidget(self._group('Token selection', [
            ('top_p', 'Top P:', self._top_p),
            ('top_k', 'Top K (0 = off):', self._top_k),
            ('min_p', 'Min P (0 = off):', self._min_p),
            ('typical_p', 'Typical P (1.0 = off):', self._typical_p),
            ('min_tokens_to_keep', 'Min tokens to keep:', self._min_tokens_to_keep),
        ]))
        sampling_layout.addWidget(self._group('Repetition', [
            ('frequency_penalty', 'Frequency Penalty:', self._freq_pen),
            ('presence_penalty', 'Presence Penalty:', self._pres_pen),
            ('repeat_penalty', 'Repetition penalty (1.0 = off):', self._repeat_pen),
            ('dry_multiplier', 'DRY multiplier (0 = off):', self._dry_multiplier),
            ('dry_base', 'DRY base:', self._dry_base),
            ('dry_allowed_length', 'DRY allowed length:', self._dry_allowed_length),
            ('dry_penalty_range', 'DRY penalty range (0 = auto):', self._dry_penalty_range),
        ]))
        sampling_layout.addWidget(self._group('Adaptive sampling (Mirostat / XTC)', [
            ('mirostat', 'Mirostat mode (0 = off):', self._mirostat),
            ('mirostat_tau', 'Mirostat tau:', self._mirostat_tau),
            ('mirostat_eta', 'Mirostat eta:', self._mirostat_eta),
            ('xtc_threshold', 'XTC threshold:', self._xtc_threshold),
            ('xtc_probability', 'XTC probability (0 = off):', self._xtc_probability),
        ]))
        sampling_layout.addWidget(self._group('Determinism', [
            ('seed', 'Seed (-1 = random):', self._seed),
        ]))
        layout.addWidget(sampling)

        request = QGroupBox('Output & Request')
        request_form = QFormLayout(request)
        request_form.addRow('Context Size (tokens):', self._context_size)
        request_form.addRow('Output Length (max tokens):', self._max_tokens)
        request_form.addRow(
            self._register_row('stop', 'Stop sequences:', self._stop), self._stop,
        )
        request_form.addRow('Retries on failure:', self._retries)
        request_form.addRow('Chat template:', self._strict_template)
        layout.addWidget(request)

        layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _register_row(self, key: str, text: str, widget: QWidget) -> QLabel:
        """Create the label of a setting row and remember it for flagging."""
        label = QLabel(text)
        label.setStyleSheet('color: #ccc;')
        self._rows[key] = (label, widget, text, widget.toolTip())
        return label

    def _group(self, title: str, rows: list[tuple[str, str, QWidget]]) -> QGroupBox:
        """Build one labelled group of settings.

        Each row is (setting key, label text, widget).  The key indexes the
        provider capability table, so the row can be flagged when the
        selected provider does not honour the setting.
        """
        form = QFormLayout()
        form.setContentsMargins(8, 4, 8, 4)
        for key, text, widget in rows:
            form.addRow(self._register_row(key, text, widget), widget)
        group = QGroupBox(title)
        group.setLayout(form)
        return group

    def set_provider(self, provider: str) -> None:
        """Flag the settings the selected provider does not honour.

        The request body is the same for every provider, but the backends do
        not agree on which parameters they implement: OpenAI and Mistral
        reject unknown keys, several hosted services ignore them, and the
        llama.cpp family has extras the hosted APIs lack.  Marking the rows
        saves the user from wondering why a slider does nothing.
        """
        self._provider = provider
        self._apply_row_states()

    def _apply_row_states(self) -> None:
        """Re-apply the warnings (and the grey-out) for the current provider."""
        provider = self._provider
        unsupported = unsupported_settings(provider)
        strict = rejects_unknown_parameters(provider)
        grey = self._grey_unsupported.isChecked()
        for key, (label, widget, text, tooltip) in self._rows.items():
            if key in unsupported:
                effect = (
                    'the request will be rejected'
                    if strict else 'the server will ignore it'
                )
                label.setText(f'{text} (not supported by {provider})')
                label.setStyleSheet('color: #e0a030;')
                widget.setToolTip(
                    f'{provider} does not implement this setting: {effect}.'
                )
                widget.setEnabled(not grey)
            else:
                label.setText(text)
                label.setStyleSheet('color: #ccc;')
                widget.setToolTip(tooltip)
                widget.setEnabled(True)
        self._refresh_provider_note(provider, unsupported)

    def _refresh_provider_note(self, provider: str, unsupported: set[str]) -> None:
        if not unsupported:
            self._provider_note.setText('')
            self._provider_note.setVisible(False)
            return
        names = ', '.join(_unsupported_summary(unsupported))
        tail = (
            'They are greyed out and left out of the request, so they have '
            'no effect.'
            if self._grey_unsupported.isChecked()
            else 'They are left out of the request, so they have no effect.'
        )
        self._provider_note.setText(
            f'{provider} does not implement: {names}. {tail}'
        )
        self._provider_note.setVisible(True)

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
            'stop': self._stop_text_list(),
            'strict_chat_template': self._strict_template.isChecked(),
            'repeat_penalty': self._repeat_pen.value(),
            'mirostat': self._mirostat.value(),
            'mirostat_tau': self._mirostat_tau.value(),
            'mirostat_eta': self._mirostat_eta.value(),
            'dry_multiplier': self._dry_multiplier.value(),
            'dry_base': self._dry_base.value(),
            'dry_allowed_length': self._dry_allowed_length.value(),
            'dry_penalty_range': self._dry_penalty_range.value(),
            'xtc_threshold': self._xtc_threshold.value(),
            'xtc_probability': self._xtc_probability.value(),
            'typical_p': self._typical_p.value(),
            'dynatemp_range': self._dynatemp_range.value(),
            'dynatemp_exponent': self._dynatemp_exponent.value(),
            'smoothing_factor': self._smoothing_factor.value(),
            'min_tokens_to_keep': self._min_tokens_to_keep.value(),
        }

    def _stop_text_list(self) -> list[str]:
        return [s.strip() for s in self._stop.text().split(',') if s.strip()]

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
        self._strict_template.setChecked(bool(config.get('strict_chat_template', False)))
        self._repeat_pen.setValue(config.get('repeat_penalty', 1.0))
        self._mirostat.setValue(config.get('mirostat', 0))
        self._mirostat_tau.setValue(config.get('mirostat_tau', 5.0))
        self._mirostat_eta.setValue(config.get('mirostat_eta', 0.1))
        self._dry_multiplier.setValue(config.get('dry_multiplier', 0.0))
        self._dry_base.setValue(config.get('dry_base', 1.75))
        self._dry_allowed_length.setValue(config.get('dry_allowed_length', 2))
        self._dry_penalty_range.setValue(config.get('dry_penalty_range', 0))
        self._xtc_threshold.setValue(config.get('xtc_threshold', 0.1))
        self._xtc_probability.setValue(config.get('xtc_probability', 0.0))
        self._typical_p.setValue(config.get('typical_p', 1.0))
        self._dynatemp_range.setValue(config.get('dynatemp_range', 0.0))
        self._dynatemp_exponent.setValue(config.get('dynatemp_exponent', 1.0))
        self._smoothing_factor.setValue(config.get('smoothing_factor', 0.0))
        self._min_tokens_to_keep.setValue(config.get('min_tokens_to_keep', 1))
        stop_value = config.get('stop', [])
        if isinstance(stop_value, (list, tuple)):
            stop_value = ', '.join(str(s) for s in stop_value)
        self._stop.setText(str(stop_value or ''))


class _MacrosPanel(QWidget):
    """{{user}} value + personas + custom macro overrides."""

    def __init__(
        self,
        user_name: str,
        custom_macros: dict[str, str],
        personas: list[dict] | None = None,
        active_id: str = '',
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        from src.ui.widgets.persona_dialog import PersonaManagerWidget

        layout = QVBoxLayout(self)

        user_group = QGroupBox('User Macro')
        user_form = QFormLayout(user_group)
        self._user_edit = QLineEdit(user_name)
        user_form.addRow('{{user}}:', self._user_edit)
        layout.addWidget(user_group)

        persona_group = QGroupBox('User Personas')
        persona_layout = QVBoxLayout(persona_group)
        self._persona_manager = PersonaManagerWidget(personas, active_id, self)
        persona_layout.addWidget(self._persona_manager)
        layout.addWidget(persona_group, 1)

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
            'personas': self._persona_manager.personas(),
            'active_persona_id': self._persona_manager.selected_id(),
        }


class _PromptsPanel(QWidget):
    """Embedded prompt template editor with a dropdown prompt selector."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        from src.ui.widgets.prompt_settings_dialog import _PromptTab

        layout = QVBoxLayout(self)

        framing_group = QGroupBox('Chat Framing')
        framing_layout = QVBoxLayout(framing_group)

        template_row = QHBoxLayout()
        template_row.addWidget(QLabel('System prompt:'))
        self._template_combo = QComboBox()
        self._template_combo.setMinimumWidth(180)
        self._template_combo.setToolTip(
            "Main prompt used for Test chats: the selected system prompt is "
            "the first system block, and the character's own system prompt is "
            "appended after it.\n"
            'The "Custom" entry is the editable Chat prompt below.'
        )
        self._refresh_template_combo()
        template_row.addWidget(self._template_combo)
        manage_btn = QPushButton('Manage System Prompts...')
        manage_btn.setToolTip(
            'Create, edit, or delete named system prompts\n'
            '(Natural Chat, Simple Roleplay, ...) and their post-history '
            'instructions.'
        )
        manage_btn.clicked.connect(self._on_manage_templates)
        template_row.addWidget(manage_btn)
        template_row.addStretch()
        framing_layout.addLayout(template_row)

        context_row = QHBoxLayout()
        context_row.addWidget(QLabel('Context template:'))
        self._context_combo = QComboBox()
        self._context_combo.setMinimumWidth(180)
        self._context_combo.setToolTip(
            'How Test-chat contexts are framed and named: system block '
            'labels, speaker names on messages, example-dialogue formatting.\n'
            'The "Default" entry keeps the classic output.'
        )
        self._refresh_context_combo()
        context_row.addWidget(self._context_combo)
        manage_ctx_btn = QPushButton('Manage Context Templates...')
        manage_ctx_btn.setToolTip(
            'Create, edit, or delete context templates (Default, Alpaca-'
            'Style, Roleplay, ...) controlling block framing, names, and '
            'example formatting.'
        )
        manage_ctx_btn.clicked.connect(self._on_manage_context_templates)
        context_row.addWidget(manage_ctx_btn)
        context_row.addStretch()
        framing_layout.addLayout(context_row)

        layout.addWidget(framing_group)

        editor_group = QGroupBox('Prompt Templates')
        editor_layout = QVBoxLayout(editor_group)

        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel('Edit prompt:'))
        self._prompt_combo = QComboBox()
        self._prompt_combo.setMinimumWidth(180)
        selector_row.addWidget(self._prompt_combo)
        self._reset_page_btn = QPushButton('Reset')
        self._reset_page_btn.setToolTip(
            'Restore the built-in templates for the selected prompt page.\n'
            'The change is only written when the settings are saved.'
        )
        self._reset_page_btn.clicked.connect(self.reset_current_page)
        selector_row.addWidget(self._reset_page_btn)
        selector_row.addStretch()
        editor_layout.addLayout(selector_row)

        self._stack = QStackedWidget()
        prompt_groups = [
            [
                ('Chat', ['chat_system']),
                ('Impersonate', ['impersonate_user', 'continue_user']),
            ],
            [
                ('Character', ['character_system', 'character_user']),
                ('Wizard', ['wizard_system', 'wizard_user']),
                ('Fill Fields', ['fill_system', 'fill_user']),
                ('Tags', ['tags_system', 'tags_user']),
                ('Missing Tags', ['missing_tags_system', 'missing_tags_user']),
                ('Alt Greetings', ['alt_greetings_system', 'alt_greetings_user']),
            ],
            [
                ('Summary', ['summary_system', 'summary_user']),
                ('Memory', ['memory_summary_system', 'memory_summary_user']),
                ('Attributes', ['attribute_update_system', 'attribute_update_user']),
                ('Expression', ['expression_classify_system', 'expression_classify_user']),
            ],
            [
                ('Lorebook', ['lorebook_system', 'lorebook_user']),
                ('Lorebook Entry', ['lorebook_entry_system', 'lorebook_entry_user']),
            ],
        ]
        for index, pages in enumerate(prompt_groups):
            if index:
                self._insert_prompt_separator()
            for name, keys in pages:
                self._prompt_combo.addItem(name, self._stack.count())
                self._stack.addWidget(_PromptTab(name, keys))
        self._prompt_combo.currentIndexChanged.connect(self._on_prompt_selected)
        editor_layout.addWidget(self._stack)
        layout.addWidget(editor_group)

    def _insert_prompt_separator(self) -> None:
        """Insert a non-selectable divider between the prompt groups."""
        model = self._prompt_combo.model()
        self._prompt_combo.insertSeparator(self._prompt_combo.count())
        if isinstance(model, QStandardItemModel):
            item = model.item(self._prompt_combo.count() - 1)
            if item is not None:
                item.setEnabled(False)

    def _on_prompt_selected(self, index: int) -> None:
        """Switch to the page stored on the combo item (separators skip)."""
        stack_index = self._prompt_combo.itemData(index)
        if stack_index is not None:
            self._stack.setCurrentIndex(int(stack_index))

    def reset_current_page(self) -> None:
        """Restore the built-in templates for the page shown in the stack."""
        self._stack.currentWidget().reset_to_default()

    def _refresh_template_combo(self) -> None:
        from src.prompt_templates import active_template_id, all_templates

        self._template_combo.blockSignals(True)
        self._template_combo.clear()
        active = active_template_id()
        for tpl in all_templates():
            self._template_combo.addItem(tpl.name, tpl.id)
        idx = self._template_combo.findData(active)
        self._template_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._template_combo.blockSignals(False)

    def _on_manage_templates(self) -> None:
        from src.ui.widgets.system_template_dialog import SystemTemplateDialog

        dlg = SystemTemplateDialog(self)
        dlg.exec()
        dlg.deleteLater()
        self._refresh_template_combo()

    def _refresh_context_combo(self) -> None:
        from src.context_templates import (
            active_context_template_id,
            all_templates,
        )

        self._context_combo.blockSignals(True)
        self._context_combo.clear()
        active = active_context_template_id()
        for tpl in all_templates():
            self._context_combo.addItem(tpl.name, tpl.id)
        idx = self._context_combo.findData(active)
        self._context_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._context_combo.blockSignals(False)

    def _on_manage_context_templates(self) -> None:
        from src.ui.widgets.context_template_dialog import ContextTemplateDialog

        dlg = ContextTemplateDialog(self)
        dlg.exec()
        dlg.deleteLater()
        self._refresh_context_combo()

    def save(self) -> None:
        for index in range(self._stack.count()):
            tab = self._stack.widget(index)
            for key, value in tab.get_templates().items():
                ai_prompts.save_prompt(key, value)
        from src.prompt_templates import save_active_template_id
        save_active_template_id(self._template_combo.currentData() or 'custom')
        from src.context_templates import save_active_context_template_id
        save_active_context_template_id(self._context_combo.currentData() or 'default')


class _AppPanel(QWidget):
    """App-wide behaviour: spell checking, logging, and the UI font size."""

    def __init__(
        self,
        spellcheck_enabled: bool = True,
        logging_enabled: bool = True,
        font_size: int = 13,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        group = QGroupBox('Spell Checking')
        group_layout = QVBoxLayout(group)
        self._spellcheck = QCheckBox('Underline misspelled words in text fields')
        self._spellcheck.setChecked(bool(spellcheck_enabled))
        self._spellcheck.setToolTip(
            'Right-click an underlined word for suggested corrections, to\n'
            'ignore it, or to add it to your personal dictionary.'
        )
        group_layout.addWidget(self._spellcheck)
        layout.addWidget(group)

        logging_group = QGroupBox('Logging')
        logging_layout = QVBoxLayout(logging_group)
        self._logging = QCheckBox('Write the application log (app.log)')
        self._logging.setChecked(bool(logging_enabled))
        self._logging.setToolTip(
            'Appends diagnostics to ~/.st-explorer/app.log while the app\n'
            'runs. Turning it off stops new log entries immediately; the\n'
            'existing file is left in place.'
        )
        logging_layout.addWidget(self._logging)
        layout.addWidget(logging_group)

        ui_group = QGroupBox('Interface')
        ui_form = QFormLayout(ui_group)
        self._font_size = QSpinBox()
        # The same range the Font Size dialog and the settings clamp to, so
        # a hand-edited value cannot put this out of sync with the setting.
        self._font_size.setRange(8, 32)
        self._font_size.setSuffix(' px')
        self._font_size.setValue(int(font_size))
        self._font_size.setToolTip(
            'The application font size - the same setting as '
            'View > Font Size...'
        )
        ui_form.addRow('Font size:', self._font_size)
        layout.addWidget(ui_group)
        layout.addStretch()

    def spellcheck_enabled(self) -> bool:
        return self._spellcheck.isChecked()

    def logging_enabled(self) -> bool:
        return self._logging.isChecked()

    def font_size(self) -> int:
        return self._font_size.value()


class _TestPanel(QWidget):
    """Test-tab display / history options."""

    def __init__(self, settings: dict, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        # View Mode leads the panel: it decides how the whole tab presents,
        # and the VN settings below only matter in one of its modes.
        view_row = QHBoxLayout()
        view_row.addWidget(QLabel('View Mode:'))
        self._view_mode = QComboBox()
        self._view_mode.addItem('Chat (scrolling conversation)', 'chat')
        self._view_mode.addItem('Visual Novel (backdrop + chat box)', 'visual_novel')
        self._view_mode.setToolTip(
            'How the Test tab presents the conversation.\n'
            'Visual Novel shows a gallery image in the main area with a '
            'smaller chat window for the dialogue.'
        )
        idx = self._view_mode.findData(settings.get('view_mode', 'chat'))
        self._view_mode.setCurrentIndex(idx if idx >= 0 else 0)
        view_row.addWidget(self._view_mode, 1)
        layout.addLayout(view_row)

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

        placement_row = QHBoxLayout()
        placement_row.addWidget(QLabel('Example dialogue:'))
        self._example_placement = QComboBox()
        self._example_placement.addItem('In system prompt (block)', 'system')
        self._example_placement.addItem('After history (block)', 'post_history')
        self._example_placement.addItem('As chat turns (legacy)', 'history')
        self._example_placement.setToolTip(
            'Where the card\'s few-shot example dialogue rides in the request.\n'
            'SillyTavern sends it as a labelled <START> block rather than as '
            'real conversation turns.'
        )
        idx = self._example_placement.findData(settings.get('example_placement', 'system'))
        self._example_placement.setCurrentIndex(idx if idx >= 0 else 0)
        placement_row.addWidget(self._example_placement, 1)
        options_layout.addLayout(placement_row)

        self._style_variation = QCheckBox('Vary reply style each turn')
        self._style_variation.setChecked(bool(settings.get('style_variation', True)))
        self._style_variation.setToolTip(
            'Rotate a small style directive into each reply (opening, '
            'rhythm, focus) so consecutive messages do not follow the same '
            'structure.\n'
            'The Test tab shows a Vary toggle for quick switching.'
        )
        options_layout.addWidget(self._style_variation)
        layout.addWidget(options_group)

        vn_group = QGroupBox('Visual Novel Mode')
        vn_form = QFormLayout(vn_group)

        self._vn_image_source = QComboBox()
        self._vn_image_source.addItem('First gallery image', 'first')
        self._vn_image_source.addItem('Latest added', 'latest')
        self._vn_image_source.addItem('Random on open', 'random')
        self._vn_image_source.setToolTip(
            'Which gallery image a chat opens with.\n'
            'A pinned backdrop (per chat or per character) takes precedence.'
        )
        idx = self._vn_image_source.findData(settings.get('vn_image_source', 'first'))
        self._vn_image_source.setCurrentIndex(idx if idx >= 0 else 0)
        vn_form.addRow('Backdrop image:', self._vn_image_source)

        self._vn_backdrop_pool = QComboBox()
        self._vn_backdrop_pool.addItem(
            'Shared backdrops + character gallery', 'both',
        )
        self._vn_backdrop_pool.addItem('Shared backdrops only', 'global')
        self._vn_backdrop_pool.addItem('Character gallery only', 'gallery')
        self._vn_backdrop_pool.setToolTip(
            'Which images fill the Visual Novel stage.\n'
            'Shared backdrops live in the Gallery tab -> Backdrops and are '
            'used by every character; the character gallery is per card.'
        )
        idx = self._vn_backdrop_pool.findData(settings.get('vn_backdrop_pool', 'both'))
        self._vn_backdrop_pool.setCurrentIndex(idx if idx >= 0 else 0)
        vn_form.addRow('Backdrop pool:', self._vn_backdrop_pool)

        self._vn_image_fit = QComboBox()
        self._vn_image_fit.addItem('Contain (letterbox)', 'contain')
        self._vn_image_fit.addItem('Cover (crop to fill)', 'cover')
        self._vn_image_fit.addItem('Stretch', 'stretch')
        idx = self._vn_image_fit.findData(settings.get('vn_image_fit', 'contain'))
        self._vn_image_fit.setCurrentIndex(idx if idx >= 0 else 0)
        vn_form.addRow('Image fit:', self._vn_image_fit)

        self._vn_chat_height = self._percent_field(15, 50, settings.get('vn_chat_height_pct', 30))
        vn_form.addRow('Chat panel height (%):', self._vn_chat_height[0])

        self._vn_overlay_opacity = self._percent_field(
            20, 100, settings.get('vn_overlay_opacity', 60),
        )
        self._vn_overlay_opacity[1].setToolTip(
            'Opacity of the dark scrim behind the chat panel, so the text '
            'stays readable next to any image.'
        )
        vn_form.addRow('Chat panel opacity (%):', self._vn_overlay_opacity[0])

        self._vn_advance_on_reply = QCheckBox('Advance to the next image on each reply')
        self._vn_advance_on_reply.setChecked(bool(settings.get('vn_advance_on_reply', False)))
        vn_form.addRow('Image advance:', self._vn_advance_on_reply)

        self._vn_backdrop_keywords = QCheckBox('Switch image from reply keywords')
        self._vn_backdrop_keywords.setChecked(bool(settings.get('vn_backdrop_keywords', False)))
        self._vn_backdrop_keywords.setToolTip(
            'When a reply matches an image\'s keywords (Gallery tab -> right-'
            'click -> Edit Keywords...), the stage switches to that image.\n'
            'Keyword matches win over the blind image advance.'
        )
        vn_form.addRow('Keyword switching:', self._vn_backdrop_keywords)

        self._vn_show_caption = QCheckBox('Show caption and image counter')
        self._vn_show_caption.setChecked(bool(settings.get('vn_show_caption', True)))
        vn_form.addRow('Caption:', self._vn_show_caption)

        self._vn_use_portrait = QCheckBox('Use the card portrait when the gallery is empty')
        self._vn_use_portrait.setChecked(bool(settings.get('vn_use_portrait_fallback', True)))
        vn_form.addRow('Fallback image:', self._vn_use_portrait)
        layout.addWidget(vn_group)

        expr_group = QGroupBox('Expression Images')
        expr_form = QFormLayout(expr_group)
        self._vn_expression_auto = QCheckBox('Auto-switch the sprite on each reply')
        self._vn_expression_auto.setChecked(bool(settings.get('vn_expression_auto', True)))
        self._vn_expression_auto.setToolTip(
            'Picks the SillyTavern expression image that matches the tone of '
            'the reply and shows it as a character sprite over the backdrop.'
        )
        expr_form.addRow('Expressions:', self._vn_expression_auto)

        self._vn_expression_llm = QCheckBox('Use LLM classification')
        self._vn_expression_llm.setChecked(bool(settings.get('vn_expression_llm', False)))
        self._vn_expression_llm.setToolTip(
            'Ask the model to classify the tone (one small request per reply).\n'
            'Off: local keyword detection - instant and offline, but only as '
            'smart as its word lists.\n'
            'Falls back to keyword detection when the request fails.'
        )
        expr_form.addRow('Detection:', self._vn_expression_llm)

        self._vn_sprite_position = QComboBox()
        self._vn_sprite_position.addItem('Left', 'left')
        self._vn_sprite_position.addItem('Center', 'center')
        self._vn_sprite_position.addItem('Right', 'right')
        idx = self._vn_sprite_position.findData(settings.get('vn_sprite_position', 'center'))
        self._vn_sprite_position.setCurrentIndex(idx if idx >= 0 else 1)
        expr_form.addRow('Sprite position:', self._vn_sprite_position)

        self._vn_sprite_height = self._percent_field(
            40, 100, settings.get('vn_sprite_height_pct', 75),
        )
        expr_form.addRow('Sprite height (%):', self._vn_sprite_height[0])

        hint = QLabel('Uses SillyTavern expression images (Settings -> ST paths).')
        hint.setStyleSheet('color: #888;')
        hint.setWordWrap(True)
        expr_form.addRow(hint)
        layout.addWidget(expr_group)

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

    def _percent_field(self, lo: int, hi: int, initial):
        """A small spin box in a row, mirroring ``_color_field``'s tuple."""
        from PyQt6.QtWidgets import QSpinBox

        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setSuffix(' %')
        try:
            spin.setValue(max(lo, min(hi, int(initial))))
        except (TypeError, ValueError):
            spin.setValue(lo)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(spin)
        h.addStretch()
        return (row, spin)

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
            'example_placement': self._example_placement.currentData(),
            'style_variation': self._style_variation.isChecked(),
            'view_mode': self._view_mode.currentData(),
            'vn_image_source': self._vn_image_source.currentData(),
            'vn_backdrop_pool': self._vn_backdrop_pool.currentData(),
            'vn_image_fit': self._vn_image_fit.currentData(),
            'vn_chat_height_pct': self._vn_chat_height[1].value(),
            'vn_overlay_opacity': self._vn_overlay_opacity[1].value(),
            'vn_advance_on_reply': self._vn_advance_on_reply.isChecked(),
            'vn_backdrop_keywords': self._vn_backdrop_keywords.isChecked(),
            'vn_show_caption': self._vn_show_caption.isChecked(),
            'vn_use_portrait_fallback': self._vn_use_portrait.isChecked(),
            'vn_expression_auto': self._vn_expression_auto.isChecked(),
            'vn_expression_llm': self._vn_expression_llm.isChecked(),
            'vn_sprite_position': self._vn_sprite_position.currentData(),
            'vn_sprite_height_pct': self._vn_sprite_height[1].value(),
        }


class _EncryptionPanel(QWidget):
    """Data-encryption management (enable / change password / disable).

    Actions are immediate (with their own confirmation dialogs) rather than
    Save/Cancel-bound: enabling or disabling encryption cannot be deferred
    until the dialog is accepted.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        status_group = QGroupBox('Status')
        status_form = QFormLayout(status_group)
        self._status_label = QLabel('')
        self._recovery_label = QLabel('')
        status_form.addRow('Data encryption:', self._status_label)
        status_form.addRow('Recovery key:', self._recovery_label)
        layout.addWidget(status_group)

        self._note = QLabel('')
        self._note.setWordWrap(True)
        self._note.setStyleSheet('color: #888888;')
        layout.addWidget(self._note)

        actions = QGroupBox('Actions')
        actions_layout = QVBoxLayout(actions)

        self._enable_btn = QPushButton('Enable Encryption...')
        self._enable_btn.clicked.connect(self._on_enable)
        actions_layout.addWidget(self._enable_btn)

        self._change_btn = QPushButton('Change Password...')
        self._change_btn.clicked.connect(self._on_change_password)
        actions_layout.addWidget(self._change_btn)

        self._disable_btn = QPushButton('Disable Encryption...')
        self._disable_btn.clicked.connect(self._on_disable)
        actions_layout.addWidget(self._disable_btn)

        layout.addWidget(actions)
        layout.addStretch()

        self._refresh()

    def _refresh(self) -> None:
        from src import vault
        v = vault.get_vault()
        enabled = v.enabled
        self._status_label.setText('Enabled' if enabled else 'Disabled')
        self._recovery_label.setText(
            'Configured' if (enabled and v.has_recovery) else 'Not configured'
        )
        self._enable_btn.setEnabled(not enabled)
        self._change_btn.setEnabled(enabled)
        self._disable_btn.setEnabled(enabled)
        if enabled:
            self._note.setText(
                'The database is encrypted when the app closes and unlocked '
                'with your password at startup. Exported files and '
                'SillyTavern sync copies are always plain text.'
            )
        else:
            self._note.setText(
                'Your library is currently stored in plain text on disk.'
            )

    def _run_migration(self, seal: bool) -> tuple[bool, int, int]:
        """Run one seal/unseal migration pass.

        Returns ``(completed_ok, changed, failed)``. A cancelled pass is
        never a success: the caller must not update vault state from it.
        """
        from src.ui.widgets.encryption_dialogs import MigrationDialog
        migration = MigrationDialog(seal, parent=self)
        migration.start()
        migration.exec()
        changed, failed = migration.result
        completed = migration.completed_ok
        migration.cleanup()
        return completed, changed, failed

    def _on_enable(self) -> None:
        from src import vault
        from src.ui.widgets.encryption_dialogs import (
            EnableEncryptionDialog,
            RecoveryKeyDialog,
        )
        dlg = EnableEncryptionDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        password, with_recovery = dlg.get_values()
        try:
            recovery_key = vault.get_vault().enable(password, with_recovery)
        except vault.VaultError as exc:
            QMessageBox.critical(self, 'Enable Encryption', str(exc))
            return
        if recovery_key:
            RecoveryKeyDialog(recovery_key, self).exec()
        completed, changed, failed = self._run_migration(seal=True)
        while not completed:
            # The envelope exists already (enable() ran), so the only way to
            # finish is to keep sealing - offer that instead of leaving the
            # library half-plain-text with no path back.
            answer = QMessageBox.question(
                self, 'Enable Encryption',
                'Encryption is on, but the migration did not finish, so some '
                'files are still plain text on disk.\n\n'
                'Continue encrypting them now?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                QMessageBox.warning(
                    self, 'Enable Encryption',
                    'Some library files remain in plain text on disk. To '
                    'finish the migration later, use "Disable Encryption" '
                    'and then enable it again.',
                )
                break
            completed, changed, failed = self._run_migration(seal=True)
        if completed and failed:
            QMessageBox.warning(
                self, 'Enable Encryption',
                f'Encryption is on, but {failed} file(s) could not be '
                'encrypted. See the log for details.',
            )
        self._refresh()

    def _on_change_password(self) -> None:
        from src import vault
        from src.ui.widgets.encryption_dialogs import ChangePasswordDialog
        dlg = ChangePasswordDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        current, new = dlg.get_values()
        try:
            vault.get_vault().change_password(current, new)
        except vault.VaultError as exc:
            QMessageBox.critical(self, 'Change Password', str(exc))
            return
        QMessageBox.information(self, 'Change Password', 'Password changed.')

    def _on_disable(self) -> None:
        from src import vault
        from src.ui.widgets.encryption_dialogs import DisableEncryptionDialog
        dlg = DisableEncryptionDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        password = dlg.get_password()
        completed, changed, failed = self._run_migration(seal=False)
        while not completed:
            # A cancelled/failed unseal pass must NOT drop the envelope: the
            # untouched files are still encrypted and the key would be gone.
            answer = QMessageBox.question(
                self, 'Disable Encryption',
                'Decryption did not finish, so encryption is still on and no '
                'data has been lost. Some files are already decrypted.\n\n'
                'Continue decrypting the remaining files now?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                QMessageBox.warning(
                    self, 'Disable Encryption',
                    'Encryption remains enabled. Re-run "Disable Encryption" '
                    'to finish the migration.',
                )
                return
            completed, changed, failed = self._run_migration(seal=False)
        if failed:
            QMessageBox.warning(
                self, 'Disable Encryption',
                f'{failed} file(s) could not be decrypted. Encryption is '
                'still on so no data is left unreadable.',
            )
            return
        try:
            if not vault.get_vault().verify_password(password):
                QMessageBox.critical(
                    self, 'Disable Encryption',
                    'The password is no longer correct. Encryption is still on.',
                )
                return
            vault.get_vault().finish_disable()
        except vault.VaultError as exc:
            QMessageBox.critical(self, 'Disable Encryption', str(exc))
            return
        self._refresh()


class SettingsDialog(QDialog):
    """Master settings dialog with API / LLM / Macros / Prompts / App / Test tabs."""

    settings_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Settings')
        self.setMinimumWidth(600)
        self.setMinimumHeight(640)

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

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._api_panel = _APIPanel(names, active, self._configs[active])
        self._llm_panel = _LLMPanel(self._configs[active])
        self._macros_panel = _MacrosPanel(
            macro.get('user_name', 'User'), macro.get('macros', {}),
            load_personas(), load_active_persona_id(),
        )
        self._prompts_panel = _PromptsPanel()
        from src.settings_manager import load_font_size, load_logging_enabled
        self._app_panel = _AppPanel(
            load_spellcheck_enabled(), load_logging_enabled(), load_font_size(),
        )
        self._test_panel = _TestPanel(test)
        self._encryption_panel = _EncryptionPanel()
        self._tabs.addTab(self._api_panel, 'API')
        self._tabs.addTab(self._llm_panel, 'LLM')
        self._tabs.addTab(self._macros_panel, 'Macros')
        self._tabs.addTab(self._prompts_panel, 'Prompts')
        self._tabs.addTab(self._app_panel, 'App')
        self._tabs.addTab(self._test_panel, 'Test')
        self._tabs.addTab(self._encryption_panel, 'Encryption')
        layout.addWidget(self._tabs)

        footer_row = QHBoxLayout()
        self._reset_all_btn = QPushButton('Reset All Settings...')
        self._reset_all_btn.setToolTip(
            'Restore every setting to its built-in default: API providers '
            'and keys, LLM parameters, macros and personas, prompt '
            'templates, Test-chat settings, and app behaviour (spell check, '
            'logging, font size).\n'
            'Asks for confirmation; the library itself is not touched.'
        )
        self._reset_all_btn.clicked.connect(self._reset_all)
        footer_row.addWidget(self._reset_all_btn)
        footer_row.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        footer_row.addWidget(buttons)
        layout.addLayout(footer_row)

        self._api_panel.provider_changed.connect(self._on_provider_changed)
        self._llm_panel.set_provider(active)

    def _reset_all(self) -> None:
        """Confirm, then immediately restore every setting to its default."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle('Reset All Settings')
        box.setText('Restore every setting to its built-in default?')
        box.setInformativeText(
            'This immediately restores: API providers, API keys and all '
            'LLM sampling parameters; the user name, custom macros and '
            'personas; all AI prompt templates; user-created system and '
            'context templates and the active selections; Test-chat '
            'settings; and spell check, logging and the font size.\n\n'
            'Your library (cards, chats, gallery) and encryption are not '
            'touched. This cannot be undone.'
        )
        reset_btn = box.addButton(
            'Reset Everything', QMessageBox.ButtonRole.AcceptRole,
        )
        cancel_btn = box.addButton('Cancel', QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        if box.clickedButton() is not reset_btn:
            return
        from src.settings_manager import (
            apply_logging_enabled,
            load_logging_enabled,
            reset_all_settings,
        )
        reset_all_settings()
        apply_logging_enabled(load_logging_enabled())
        self._api_panel.cleanup()
        self.settings_changed.emit()
        self.accept()

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
        # The backends disagree on which parameters they implement, so the
        # LLM panel has to be re-flagged for the new provider.
        self._llm_panel.set_provider(new_name)

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
        personas = macro.get('personas') or []
        active_id = macro.get('active_persona_id') or ''
        save_personas(personas)
        save_active_persona_id(active_id)
        # Keep the legacy single-persona key mirroring the active persona so
        # older builds (and its migration path) still see the current text.
        active_text = next(
            (p.get('text', '') for p in personas if p.get('id') == active_id),
            personas[0].get('text', '') if personas else '',
        )
        save_user_persona(active_text)
        self._prompts_panel.save()
        save_test_settings(test)
        save_spellcheck_enabled(self._app_panel.spellcheck_enabled())
        # The log toggle is applied live (the FileHandler is added/removed
        # from the root logger), so no restart is needed.
        logging_enabled = self._app_panel.logging_enabled()
        save_logging_enabled(logging_enabled)
        apply_logging_enabled(logging_enabled)
        # The theme/font re-application happens in the main window's
        # settings_changed handler, which also refreshes the tab styles.
        save_font_size(self._app_panel.font_size())
        logger.info("Settings saved")
        self.settings_changed.emit()
        # accept() does not go through closeEvent, so the fetch worker must
        # be shut down here too or outlive the dialog mid-run.
        self._api_panel.cleanup()
        self.accept()

    def closeEvent(self, event) -> None:
        self._api_panel.cleanup()
        super().closeEvent(event)

    def reject(self) -> None:
        self._api_panel.cleanup()
        super().reject()

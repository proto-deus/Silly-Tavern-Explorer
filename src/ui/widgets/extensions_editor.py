from __future__ import annotations

import json
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Keys that have dedicated controls in the edit form and should not appear
# in the generic extensions table.
_RESERVED_KEYS: frozenset[str] = frozenset({'talkativeness', 'fav'})

# Supported value types in the type dropdown.
_TYPES: tuple[str, ...] = ('string', 'number', 'bool', 'object', 'array', 'null')


def filter_extensions(
    extensions: dict | None,
    reserved_keys: frozenset[str] | set[str] = _RESERVED_KEYS,
) -> dict:
    """Return a copy of *extensions* with reserved keys removed.

    Pure function so the filtering logic can be unit-tested without Qt.
    """
    if not isinstance(extensions, dict):
        return {}
    return {k: v for k, v in extensions.items() if k not in reserved_keys}


def infer_type(value: Any) -> str:
    """Infer the JSON type name of *value*.

    Pure function so type inference can be unit-tested without Qt.
    """
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    return 'string'


def parse_value(text: str, type_hint: str) -> tuple[Any, str | None]:
    """Parse *text* as a value of the given *type_hint*.

    Returns ``(parsed_value, error_message)``.  On success *error_message*
    is ``None``.  On failure *parsed_value* is ``None``.

    Pure function so parsing can be unit-tested without Qt.
    """
    text = text.strip()
    if type_hint == 'string':
        return text, None
    if type_hint == 'null':
        if text.lower() in ('', 'null', 'none'):
            return None, None
        return None, f"Cannot parse '{text}' as null"
    if type_hint == 'bool':
        low = text.lower()
        if low in ('true', '1', 'yes'):
            return True, None
        if low in ('false', '0', 'no'):
            return False, None
        return None, f"Cannot parse '{text}' as bool"
    if type_hint in ('number', 'object', 'array'):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            return None, f"Invalid JSON: {e}"
        expected = {'number': (int, float), 'object': dict, 'array': list}
        if not isinstance(parsed, expected[type_hint]) or isinstance(parsed, bool):
            if type_hint == 'number' and isinstance(parsed, bool):
                pass
            else:
                return None, f"Value is {infer_type(parsed)}, not {type_hint}"
        if type_hint == 'number' and isinstance(parsed, bool):
            return None, "Value is bool, not number"
        return parsed, None
    return None, f"Unknown type: {type_hint}"


def value_to_text(value: Any) -> str:
    """Serialize a value to its text representation for the table cell.

    Strings are returned as-is; other types use JSON serialization.
    Pure function so serialization can be unit-tested without Qt.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return 'null'
    return json.dumps(value, ensure_ascii=False)


class ExtensionsEditor(QDialog):
    """Generic key-value editor for extensions dicts.

    *reserved_keys* lists keys managed elsewhere that must not appear in
    the table; it defaults to the card-level keys.  Lorebooks pass an
    empty set since every extension field is fair game there.
    """

    def __init__(
        self,
        extensions: dict | None,
        parent: QWidget | None = None,
        reserved_keys: frozenset[str] | set[str] = _RESERVED_KEYS,
    ):
        super().__init__(parent)
        self.setWindowTitle('Extensions Editor')
        self.setMinimumWidth(500)
        self.setMinimumHeight(350)

        self._data = filter_extensions(extensions, reserved_keys)

        layout = QVBoxLayout(self)

        label = 'Edit extension fields'
        if reserved_keys:
            label += f" (except {', '.join(sorted(reserved_keys))})"
        layout.addWidget(QLabel(label + ':'))

        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(['Key', 'Value', 'Type'])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton('Add')
        add_btn.clicked.connect(self._add_row)
        btn_row.addWidget(add_btn)
        remove_btn = QPushButton('Remove')
        remove_btn.clicked.connect(self._remove_row)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate_table()

    def _populate_table(self) -> None:
        self._table.setRowCount(0)
        for key, value in self._data.items():
            self._add_row_with(key, value)

    def _add_row_with(self, key: str, value: Any) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        key_item = QTableWidgetItem(key)
        key_item.setFlags(key_item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(row, 0, key_item)

        val_item = QTableWidgetItem(value_to_text(value))
        self._table.setItem(row, 1, val_item)

        type_combo = QComboBox()
        for t in _TYPES:
            type_combo.addItem(t)
        type_combo.setCurrentText(infer_type(value))
        self._table.setCellWidget(row, 2, type_combo)

    def _add_row(self) -> None:
        self._add_row_with('', '')

    def _remove_row(self) -> None:
        row = self._table.currentRow()
        if row >= 0:
            self._table.removeRow(row)

    def _on_accept(self) -> None:
        result: dict[str, Any] = {}
        for row in range(self._table.rowCount()):
            key_item = self._table.item(row, 0)
            val_item = self._table.item(row, 1)
            type_combo = self._table.cellWidget(row, 2)
            if not key_item or not val_item:
                continue
            key = key_item.text().strip()
            if not key:
                continue
            type_hint = type_combo.currentText() if type_combo else 'string'
            parsed, err = parse_value(val_item.text(), type_hint)
            if err is not None:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.warning(self, 'Validation Error', f"Row {row + 1} ({key}): {err}")
                return
            result[key] = parsed
        self._data = result
        self.accept()

    def get_extensions(self) -> dict[str, Any]:
        """Return the edited extensions dict (reserved keys excluded)."""
        return dict(self._data)

    @staticmethod
    def edit(extensions: dict, parent: QWidget | None = None,
             reserved_keys: frozenset[str] | set[str] = _RESERVED_KEYS) -> dict | None:
        """Open a modal editor for *extensions*.

        Returns the edited dict on acceptance, or ``None`` when cancelled.
        """
        dlg = ExtensionsEditor(extensions, parent, reserved_keys=reserved_keys)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.get_extensions()
        return None

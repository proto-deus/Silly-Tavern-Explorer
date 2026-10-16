from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.chat_sessions import new_attribute_entry, new_memory_entry
from src.ui.widgets.edit_message_dialog import TextEditDialog


def _format_ts(iso: str) -> str:
    if not iso:
        return ''
    try:
        return datetime.fromisoformat(iso).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return iso


class MemoryDialog(QDialog):
    memories_changed = pyqtSignal(list)
    summarize_requested = pyqtSignal()
    auto_summarize_changed = pyqtSignal(bool)
    attributes_changed = pyqtSignal(list)
    update_attributes_requested = pyqtSignal()
    auto_update_attributes_changed = pyqtSignal(bool)

    def __init__(
        self,
        memories: list[dict],
        auto_summarize: bool,
        attributes: list[dict] | None = None,
        auto_update_attributes: bool = True,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle('Chat Memory')
        self.setMinimumSize(500, 420)
        self.resize(600, 540)
        self._memories = [dict(m) for m in memories]
        self._attributes = [dict(a) for a in (attributes or [])]
        self._refreshing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._list = QListWidget()
        layout.addWidget(self._list, 1)

        self._auto_check = QCheckBox('Auto-summarize each exchange')
        self._auto_check.setChecked(auto_summarize)
        self._auto_check.toggled.connect(self._on_auto_toggled)
        layout.addWidget(self._auto_check)

        btn_row = QHBoxLayout()
        add_btn = QPushButton('Add')
        add_btn.clicked.connect(self._on_add)
        btn_row.addWidget(add_btn)
        edit_btn = QPushButton('Edit')
        edit_btn.clicked.connect(self._on_edit)
        btn_row.addWidget(edit_btn)
        delete_btn = QPushButton('Delete')
        delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(delete_btn)
        btn_row.addStretch()
        self._summarize_btn = QPushButton('Summarize Now')
        self._summarize_btn.clicked.connect(self.summarize_requested.emit)
        btn_row.addWidget(self._summarize_btn)
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        attr_group = QGroupBox('Persistent Attributes')
        attr_layout = QVBoxLayout(attr_group)
        attr_layout.setContentsMargins(8, 6, 8, 8)
        attr_layout.setSpacing(4)
        hint = QLabel(
            'State the model watches and keeps current each turn '
            '(setting, mood, clothing, ...). Rename or add rows for anything '
            'custom you want tracked; "What To Watch For" tells the model '
            'what the attribute covers and when its value should change. '
            'Uncheck a row to stop tracking that attribute.'
        )
        # Without wrapping this label's size hint is the whole text on one
        # line (~1500px), which drags the dialog out to that width.
        hint.setWordWrap(True)
        attr_layout.addWidget(hint)
        self._attr_table = QTableWidget(0, 4)
        self._attr_table.setHorizontalHeaderLabels([
            'On', 'Attribute', 'Value', 'What To Watch For',
        ])
        # Single-line rows keep every attribute visible at once in a small
        # window; long text is elided and shown in full on hover (see the
        # ToolTipRole in _refresh_attrs).
        self._attr_table.setWordWrap(False)
        self._attr_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        header = self._attr_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._attr_table.setColumnWidth(1, 190)
        self._attr_table.setColumnWidth(2, 110)
        self._attr_table.itemChanged.connect(self._on_attr_item_changed)
        attr_layout.addWidget(self._attr_table)

        attr_btn_row = QHBoxLayout()
        attr_add_btn = QPushButton('Add')
        attr_add_btn.clicked.connect(self._on_attr_add)
        attr_btn_row.addWidget(attr_add_btn)
        attr_remove_btn = QPushButton('Remove Selected')
        attr_remove_btn.clicked.connect(self._on_attr_remove)
        attr_btn_row.addWidget(attr_remove_btn)
        attr_btn_row.addStretch()
        self._auto_attr_check = QCheckBox('Auto-update each exchange')
        self._auto_attr_check.setChecked(auto_update_attributes)
        self._auto_attr_check.setToolTip(
            'After every reply the model is asked whether any watched\n'
            'attribute changed, and the new values are stored in memory.'
        )
        self._auto_attr_check.toggled.connect(self._on_auto_attr_toggled)
        attr_btn_row.addWidget(self._auto_attr_check)
        self._update_attr_btn = QPushButton('Update Attributes Now')
        self._update_attr_btn.clicked.connect(self.update_attributes_requested.emit)
        attr_btn_row.addWidget(self._update_attr_btn)
        attr_layout.addLayout(attr_btn_row)
        # The attribute table has more rows to show than the memory list.
        layout.addWidget(attr_group, 2)

        self._refresh()
        self._refresh_attrs()

    # ---- memories ----

    def _refresh(self) -> None:
        self._list.clear()
        for i, m in enumerate(self._memories):
            source = m.get('source', 'manual')
            ts = _format_ts(m.get('created_at', ''))
            preview = m.get('content', '')[:80].replace('\n', ' ')
            label = f"[{source}] {ts}  —  {preview}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._list.addItem(item)

    def _selected_index(self) -> int | None:
        item = self._list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_add(self) -> None:
        dlg = TextEditDialog(title='Add Memory', placeholder='Enter a memory note...', parent=self)
        accepted = dlg.exec() == self.DialogCode.Accepted
        text = dlg.text().strip()
        dlg.deleteLater()
        if accepted and text:
            self._memories.append(new_memory_entry(text, source='manual'))
            self._refresh()
            self.memories_changed.emit(self._memories)

    def _on_edit(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        m = self._memories[idx]
        dlg = TextEditDialog(m.get('content', ''), title='Edit Memory', parent=self)
        accepted = dlg.exec() == self.DialogCode.Accepted
        new_text = dlg.text().strip()
        dlg.deleteLater()
        if accepted and new_text:
            m['content'] = new_text
            self._refresh()
            self.memories_changed.emit(self._memories)

    def _on_delete(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        if QMessageBox.question(
            self, 'Delete Memory', 'Delete this memory entry?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            del self._memories[idx]
            self._refresh()
            self.memories_changed.emit(self._memories)

    def _on_auto_toggled(self, checked: bool) -> None:
        self.auto_summarize_changed.emit(checked)

    def set_memories(self, memories: list[dict]) -> None:
        self._memories = [dict(m) for m in memories]
        self._refresh()

    def set_auto_summarize(self, value: bool) -> None:
        self._auto_check.blockSignals(True)
        self._auto_check.setChecked(bool(value))
        self._auto_check.blockSignals(False)

    def set_summarizing(self, active: bool) -> None:
        self._summarize_btn.setEnabled(not active)
        self._summarize_btn.setText('Summarizing...' if active else 'Summarize Now')

    # ---- persistent attributes ----

    def _refresh_attrs(self) -> None:
        self._refreshing = True
        try:
            self._attr_table.setRowCount(0)
            for a in self._attributes:
                row = self._attr_table.rowCount()
                self._attr_table.insertRow(row)
                check = QTableWidgetItem()
                check.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled,
                )
                check.setCheckState(
                    Qt.CheckState.Checked if a.get('enabled', True)
                    else Qt.CheckState.Unchecked,
                )
                self._attr_table.setItem(row, 0, check)
                self._attr_table.setItem(row, 1, QTableWidgetItem(a.get('name', '')))
                self._attr_table.setItem(row, 2, QTableWidgetItem(a.get('value', '')))
                self._attr_table.setItem(row, 3, QTableWidgetItem(a.get('description', '')))
                # Elided cells stay readable via hover.
                for col, text in (
                    (1, a.get('name', '')),
                    (2, a.get('value', '')),
                    (3, a.get('description', '')),
                ):
                    self._attr_table.item(row, col).setData(
                        Qt.ItemDataRole.ToolTipRole, text,
                    )
        finally:
            self._refreshing = False

    def _on_attr_item_changed(self, item: QTableWidgetItem) -> None:
        if self._refreshing:
            return
        row, col = item.row(), item.column()
        if not 0 <= row < len(self._attributes):
            return
        if col == 0:
            enabled = item.checkState() == Qt.CheckState.Checked
            if self._attributes[row].get('enabled', True) == enabled:
                return
            self._attributes[row]['enabled'] = enabled
            self.attributes_changed.emit(self._attributes)
            return
        key = {1: 'name', 2: 'value', 3: 'description'}.get(col)
        if key is None:
            return
        text = item.text() if col == 2 else item.text().strip()
        if col == 1 and not text:
            # An attribute without a name can't be watched; keep the edit out.
            self._refreshing = True
            item.setText(self._attributes[row].get('name', ''))
            self._refreshing = False
            return
        if self._attributes[row].get(key) == text:
            return
        self._attributes[row][key] = text
        # Keep the hover text of an elided cell in sync with the edit.
        self._refreshing = True
        item.setData(Qt.ItemDataRole.ToolTipRole, text)
        self._refreshing = False
        self.attributes_changed.emit(self._attributes)

    def _on_attr_add(self) -> None:
        self._attributes.append(new_attribute_entry(
            f'Attribute {len(self._attributes) + 1}',
        ))
        self._refresh_attrs()
        self.attributes_changed.emit(self._attributes)

    def _on_attr_remove(self) -> None:
        rows = sorted({idx.row() for idx in self._attr_table.selectedIndexes()}, reverse=True)
        changed = False
        for row in rows:
            if 0 <= row < len(self._attributes):
                del self._attributes[row]
                changed = True
        if changed:
            self._refresh_attrs()
            self.attributes_changed.emit(self._attributes)

    def _on_auto_attr_toggled(self, checked: bool) -> None:
        self.auto_update_attributes_changed.emit(checked)

    def set_attributes(self, attributes: list[dict]) -> None:
        self._attributes = [dict(a) for a in (attributes or [])]
        self._refresh_attrs()

    def set_auto_update_attributes(self, value: bool) -> None:
        self._auto_attr_check.blockSignals(True)
        self._auto_attr_check.setChecked(bool(value))
        self._auto_attr_check.blockSignals(False)

    def set_updating_attributes(self, active: bool) -> None:
        self._update_attr_btn.setEnabled(not active)
        self._update_attr_btn.setText(
            'Updating Attributes...' if active else 'Update Attributes Now',
        )

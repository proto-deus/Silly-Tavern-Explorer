from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.chat_sessions import new_memory_entry
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

    def __init__(
        self,
        memories: list[dict],
        auto_summarize: bool,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle('Chat Memory')
        self.setMinimumSize(520, 420)
        self._memories = [dict(m) for m in memories]

        layout = QVBoxLayout(self)

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

        self._refresh()

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
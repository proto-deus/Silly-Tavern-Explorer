"""Dialog presenting the results of :mod:`src.doctor`."""
from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from src import doctor

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {'error': 0, 'warning': 1, 'info': 2}
_SEVERITY_LABEL = {'error': 'Error', 'warning': 'Warning', 'info': 'Note'}


class DoctorDialog(QDialog):
    """Report library problems and offer to apply the safe automatic fixes."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Library Check')
        self.resize(760, 480)
        self._db = db
        self._report: doctor.DoctorReport | None = None

        layout = QVBoxLayout(self)

        self._summary = QLabel('Checking...')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(['Severity', 'Problem', 'Detail'])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self._tree, 1)

        self._result = QLabel('')
        self._result.setWordWrap(True)
        layout.addWidget(self._result)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._fix_btn = QPushButton('Apply Automatic Fixes')
        self._fix_btn.setEnabled(False)
        self._fix_btn.clicked.connect(self._on_fix)
        btn_row.addWidget(self._fix_btn)
        self._rescan_btn = QPushButton('Re-check')
        self._rescan_btn.clicked.connect(self.run_check)
        btn_row.addWidget(self._rescan_btn)
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self.run_check()

    def run_check(self) -> None:
        """(Re-)run every check and repopulate the list."""
        self._summary.setText('Checking...')
        self._tree.clear()
        self._fix_btn.setEnabled(False)
        self._result.clear()
        try:
            report = doctor.run_all(self._db)
        except Exception as exc:
            logger.exception("Library check failed")
            self._summary.setText(f'The library check could not run: {exc}')
            return
        self._report = report
        for finding in sorted(
            report.findings,
            key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.category),
        ):
            item = QTreeWidgetItem([
                _SEVERITY_LABEL.get(finding.severity, finding.severity),
                finding.message,
                finding.detail,
            ])
            item.setToolTip(2, finding.detail)
            self._tree.addTopLevelItem(item)
        for col in range(3):
            self._tree.resizeColumnToContents(col)
        self._summary.setText(doctor.summarize(report))
        self._fix_btn.setEnabled(any(f.fixable for f in report.findings))

    def _on_fix(self) -> None:
        if self._report is None:
            return
        applied = doctor.repair(self._db, self._report)
        if applied:
            self._result.setText('\n'.join(applied))
        else:
            self._result.setText('Nothing needed fixing.')
        self.run_check()
        self._result.setText(
            (self._result.text() + '\n\n' if applied else '') +
            'Re-checked after applying fixes.'
        )
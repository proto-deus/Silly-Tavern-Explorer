"""Dialog presenting the results of :mod:`src.doctor`.

The dialog has two modes:

* **Check** - the list of findings.
* **Preview** - a dry run of the repairs: exactly which rows would be dropped
  and which folders would be deleted, with a checkbox per item so the user can
  deselect the destructive ones.

Nothing is deleted until the user confirms the preview.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from src import doctor

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {'error': 0, 'warning': 1, 'info': 2}
_SEVERITY_LABEL = {'error': 'Error', 'warning': 'Warning', 'info': 'Note'}


class _CheckWorker(QThread):
    """Runs the (read-only) library check off the GUI thread.

    ``doctor.run_all`` walks the whole library - every card file, chat
    session folder and gallery directory - which took long enough to freeze
    the window before the dialog was even painted.
    """

    report_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db

    def run(self) -> None:  # pragma: no cover - exercised through the dialog
        try:
            self.report_ready.emit(doctor.run_all(self._db))
        except Exception as exc:
            logger.exception("Library check failed")
            self.failed.emit(str(exc))


class _RepairWorker(QThread):
    """Applies the selected repairs off the GUI thread.

    A repair can delete dozens of session and gallery folders and rewrite
    database rows; doing that on the GUI thread froze the window for the
    whole operation.
    """

    finished_ok = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, db, report, plans, parent=None):
        super().__init__(parent)
        self._db = db
        self._report = report
        self._plans = plans

    def run(self) -> None:
        try:
            self.finished_ok.emit(doctor.repair(self._db, self._report, self._plans))
        except Exception as exc:
            logger.exception("Library repair failed")
            self.failed.emit(str(exc))


class DoctorDialog(QDialog):
    """Report library problems and offer to apply the safe automatic fixes."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Library Check')
        self.resize(760, 520)
        self._db = db
        self._report: doctor.DoctorReport | None = None
        self._plans: list[doctor.RepairPlan] = []
        # Which plan indices the user wants applied (index -> bool).
        self._selected: dict[int, bool] = {}
        self._worker: _CheckWorker | None = None
        self._repair_worker: _RepairWorker | None = None
        self._check_done = False

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

        self._preview = QTreeWidget()
        self._preview.setColumnCount(2)
        self._preview.setHeaderLabels(['Apply', 'Planned change'])
        self._preview.setRootIsDecorated(False)
        self._preview.setAlternatingRowColors(True)
        self._preview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._preview.itemChanged.connect(self._on_preview_item_changed)
        self._preview.setVisible(False)
        layout.addWidget(self._preview, 1)

        self._result = QLabel('')
        self._result.setWordWrap(True)
        layout.addWidget(self._result)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._preview_btn = QPushButton('Preview Fixes')
        self._preview_btn.setEnabled(False)
        self._preview_btn.clicked.connect(self._on_preview)
        btn_row.addWidget(self._preview_btn)
        self._fix_btn = QPushButton('Apply Automatic Fixes')
        self._fix_btn.setEnabled(False)
        self._fix_btn.clicked.connect(self._on_fix)
        btn_row.addWidget(self._fix_btn)
        self._rescan_btn = QPushButton('Re-check')
        self._rescan_btn.clicked.connect(self.run_check)
        btn_row.addWidget(self._rescan_btn)
        close_btn = QPushButton('Close')
        # close() (not accept()) so the button path goes through closeEvent:
        # accept() bypasses it, and closing while a worker runs would destroy
        # the live QThread and abort the process.
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # The check runs in a worker thread, so starting it here does not
        # block the dialog.
        self.run_check()

    # -- check -----------------------------------------------------------

    def run_check(self) -> None:
        """(Re-)run every check and repopulate the list."""
        if self._worker is not None and self._worker.isRunning():
            return
        self._check_done = False
        self._summary.setText('Checking...')
        self._tree.clear()
        self._preview.clear()
        self._preview.setVisible(False)
        self._fix_btn.setEnabled(False)
        self._preview_btn.setEnabled(False)
        self._rescan_btn.setEnabled(False)
        self._result.clear()
        self._worker = _CheckWorker(self._db, self)
        self._worker.report_ready.connect(self._on_report)
        self._worker.failed.connect(self._on_check_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_worker_finished(self) -> None:
        self._check_done = True
        self._rescan_btn.setEnabled(True)

    def _on_check_failed(self, message: str) -> None:
        self._summary.setText(f'The library check could not run: {message}')

    def _on_report(self, report) -> None:
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
        self._preview_btn.setEnabled(self._fix_btn.isEnabled())

    def wait_for_check(self, timeout_ms: int = 10000) -> bool:
        """Spin the event loop until the running check has finished.

        Returns True when a report is available.  Used by the tests; the
        dialog itself never blocks.
        """
        from PyQt6.QtCore import QElapsedTimer

        timer = QElapsedTimer()
        timer.start()
        while timer.elapsed() < timeout_ms:
            if self._check_done:
                return self._report is not None
            QApplication.processEvents()
            QThread.msleep(10)
        return self._report is not None

    # -- dry run ---------------------------------------------------------

    def closeEvent(self, event) -> None:
        # The workers are children of the dialog: they must not be running
        # when the dialog is destroyed (Qt aborts on "QThread: Destroyed
        # while thread is still running").  The check is read-only and
        # short; the repair can take longer, so it gets a longer bounded
        # wait.  If a worker is still busy past the wait, refuse to close
        # rather than destroy the thread.
        if not self._wait_for_workers():
            QMessageBox.warning(
                self, 'Library Check',
                'A check or repair is still running. Please wait for it to '
                'finish before closing.',
            )
            event.ignore()
            return
        super().closeEvent(event)

    def _wait_for_workers(self) -> bool:
        """Bounded-wait for the running workers; True only when all done."""
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.wait(10000)
            if worker.isRunning():
                return False
        repair = self._repair_worker
        if repair is not None and repair.isRunning():
            repair.wait(30000)
            if repair.isRunning():
                return False
        return True

    def reject(self) -> None:
        # Esc (and QDialog's own closeEvent) end up here.  Rejecting while a
        # worker runs would let exec_dialog destroy the dialog - and the live
        # QThread - and abort the process, so busy means "stay open".
        if self._wait_for_workers():
            super().reject()

    def _on_preview(self) -> None:

        """Show what the repairs would do, without doing anything."""
        if self._report is None:
            return
        self._plans = doctor.plan_repairs(self._report)
        self._selected = {index: True for index in range(len(self._plans))}
        self._preview.clear()
        self._preview.setVisible(True)
        self._tree.setVisible(False)
        self._preview.blockSignals(True)
        for plan in self._plans:
            text = plan.description or plan.finding.message
            if plan.targets:
                text += '\n' + '\n'.join(f'    {t}' for t in plan.targets[:20])
                if len(plan.targets) > 20:
                    text += f'\n    ... and {len(plan.targets) - 20} more'
            item = QTreeWidgetItem(['', text])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            if plan.destructive:
                item.setForeground(1, Qt.GlobalColor.red)
            self._preview.addTopLevelItem(item)
        self._preview.blockSignals(False)
        for col in range(2):
            self._preview.resizeColumnToContents(col)
        self._summary.setText(doctor.plan_summary(self._plans))
        self._fix_btn.setText('Apply Selected Fixes')
        self._fix_btn.setEnabled(bool(self._plans))

    def _on_preview_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        row = self._preview.indexOfTopLevelItem(item)
        if row >= 0:
            self._selected[row] = item.checkState(0) == Qt.CheckState.Checked

    def _back_to_findings(self) -> None:
        self._preview.setVisible(False)
        self._tree.setVisible(True)
        self._fix_btn.setText('Apply Automatic Fixes')

    # -- apply -----------------------------------------------------------

    def _on_fix(self) -> None:
        if self._report is None:
            return
        if not self._plans:
            # No preview yet: apply everything, but confirm the deletions.
            plans = doctor.plan_repairs(self._report)
        else:
            plans = self._plans
        # Only the plans the user left ticked are applied.  Iterating with the
        # index (instead of ``plans.index(p)``) matters: two plans can be
        # structurally identical, and ``list.index`` compares by value, so it
        # would resolve both to the first one and consult the wrong entry.
        selected = [p for i, p in enumerate(plans) if self._selected.get(i, True)]
        destructive = [p for p in selected if p.destructive]
        if destructive:
            folders = sum(len(p.targets) for p in destructive)
            answer = QMessageBox.question(
                self,
                'Confirm deletions',
                f'This will permanently delete {folders} folder(s) that contain '
                f'chat history or gallery images.\n\nDo you want to continue?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        # The repair runs off the GUI thread: it can delete dozens of
        # session and gallery folders and rewrite database rows.
        self._fix_btn.setEnabled(False)
        self._summary.setText('Applying the selected fixes...')
        self._repair_worker = _RepairWorker(
            self._db, self._report, selected, self,
        )
        self._repair_worker.finished_ok.connect(self._on_repair_ok)
        self._repair_worker.failed.connect(self._on_repair_failed)
        self._repair_worker.finished.connect(self._on_repair_finished)
        self._repair_worker.start()

    def _on_repair_ok(self, applied: list) -> None:
        self._plans = []
        self._selected = {}
        self._back_to_findings()
        if applied:
            self._result.setText('\n'.join(applied))
        else:
            self._result.setText('Nothing needed fixing.')
        self.run_check()
        self._result.setText(
            (self._result.text() + '\n\n' if applied else '') +
            'Re-checked after applying fixes.'
        )

    def _on_repair_failed(self, message: str) -> None:
        self._result.setText(f'The repair failed: {message}')
        self._fix_btn.setEnabled(True)

    def _on_repair_finished(self) -> None:
        self._repair_worker = None
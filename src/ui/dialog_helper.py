"""Helpers for running short-lived modal dialogs.

A ``QDialog`` parented to a tab is owned by Qt, so dropping the local Python
wrapper does *not* destroy it: every invocation of a non-``WA_DeleteOnClose``
dialog leaked one dialog (and all its children) for the life of the window.
:func:`exec_dialog` disposes of the dialog once the result has been read.
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtWidgets import QDialog


def exec_dialog(dialog: QDialog) -> bool:
    """Run *dialog* modally, dispose of it, and return whether it was accepted.

    Read any result getters *before* calling this if they return C++-owned
    data, or capture them from signals: the C++ object is gone on return.
    """
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    dialog.setParent(None)
    dialog.deleteLater()
    return accepted


def exec_dialog_with(dialog: QDialog, collect: Callable[[QDialog], object]) -> object:
    """Run *dialog*, then apply *collect* to it before disposing of it.

    Use this when the result must be read from the dialog itself rather than
    delivered by a signal.
    """
    dialog.exec()
    try:
        return collect(dialog)
    finally:
        dialog.setParent(None)
        dialog.deleteLater()
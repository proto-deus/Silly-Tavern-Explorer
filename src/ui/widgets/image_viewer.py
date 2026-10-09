from __future__ import annotations


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to the range ``[lo, hi]``."""
    return max(lo, min(hi, value))


def next_zoom(current: float, factor: float,
              lo: float = 0.1, hi: float = 10.0) -> float:
    """Return the zoom level after multiplying *current* by *factor*.

    Pure function so the zoom math can be unit-tested without Qt.
    """
    return clamp(current * factor, lo, hi)


def clamp_thumb_size(size: int, lo: int = 80, hi: int = 280) -> int:
    """Clamp a thumbnail pixel dimension to a sane range."""
    return int(max(lo, min(hi, size)))


try:
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import QAction, QPainter, QPixmap, QTransform
    from PyQt6.QtWidgets import (
        QDialog,
        QFileDialog,
        QGraphicsScene,
        QGraphicsView,
        QHBoxLayout,
        QMessageBox,
        QPushButton,
        QVBoxLayout,
    )
except ImportError:
    QAction = None  # type: ignore[misc,assignment]


if QAction is not None:

    class ImageViewer(QDialog):
        """A zoomable, pannable image viewer dialog.

        Uses ``QGraphicsView`` + ``QGraphicsScene`` for smooth zoom/pan.
        Features: Ctrl+=/Ctrl+-/wheel zoom, fit-to-window toggle, Save As,
        and Esc to close.
        """

        def __init__(self, image_path: str, title: str = 'Image Viewer',
                     parent=None):
            super().__init__(parent)
            self.setWindowTitle(title)
            self._image_path = image_path
            self._zoom = 1.0
            self._fit_mode = True

            self._build_ui()
            self._load_image()
            self.resize(800, 700)

        def _build_ui(self) -> None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(4, 4, 4, 4)

            self._view = QGraphicsView()
            self._scene = QGraphicsScene(self)
            self._view.setScene(self._scene)
            self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self._view.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            self._view.setBackgroundBrush(Qt.GlobalColor.darkGray)
            layout.addWidget(self._view, 1)

            btn_row = QHBoxLayout()
            self._zoom_in_btn = QPushButton('Zoom In')
            self._zoom_in_btn.clicked.connect(self._zoom_in)
            btn_row.addWidget(self._zoom_in_btn)

            self._zoom_out_btn = QPushButton('Zoom Out')
            self._zoom_out_btn.clicked.connect(self._zoom_out)
            btn_row.addWidget(self._zoom_out_btn)

            self._fit_btn = QPushButton('Fit to Window')
            self._fit_btn.setCheckable(True)
            self._fit_btn.setChecked(True)
            self._fit_btn.clicked.connect(self._toggle_fit)
            btn_row.addWidget(self._fit_btn)

            btn_row.addStretch()

            save_btn = QPushButton('Save As...')
            save_btn.clicked.connect(self._save_as)
            btn_row.addWidget(save_btn)

            close_btn = QPushButton('Close')
            close_btn.clicked.connect(self.reject)
            btn_row.addWidget(close_btn)
            layout.addLayout(btn_row)

        def _load_image(self) -> None:
            from src import vault
            try:
                data = vault.read_bytes(self._image_path)
            except Exception:
                data = b''
            pixmap = QPixmap()
            if data:
                pixmap.loadFromData(data)
            if pixmap.isNull():
                # Fall back to Pillow for formats Qt can't read directly.
                try:
                    import io
                    from PIL import Image
                    from PIL.ImageQt import ImageQt
                    img = Image.open(io.BytesIO(data)).convert('RGBA')
                    pixmap = QPixmap.fromImage(ImageQt(img))
                except Exception:
                    pass
            if pixmap.isNull():
                self._scene.addText('Could not load image.')
                return
            self._pixmap = pixmap
            self._pixmap_item = self._scene.addPixmap(pixmap)
            self._scene.setSceneRect(QRectF(pixmap.rect()))
            self._fit_to_window()

        def _zoom_in(self) -> None:
            self._set_zoom(self._zoom * 1.25)

        def _zoom_out(self) -> None:
            self._set_zoom(self._zoom / 1.25)

        def _set_zoom(self, zoom: float) -> None:
            self._zoom = next_zoom(zoom, 1.0)
            self._fit_mode = False
            self._fit_btn.setChecked(False)
            self._view.setTransform(QTransform.fromScale(self._zoom, self._zoom))

        def _fit_to_window(self) -> None:
            self._fit_mode = True
            self._fit_btn.setChecked(True)
            self._view.fitInView(self._scene.sceneRect(),
                                 Qt.AspectRatioMode.KeepAspectRatio)
            transform = self._view.transform()
            self._zoom = transform.m11()

        def _toggle_fit(self, checked: bool) -> None:
            if checked:
                self._fit_to_window()
            else:
                self._fit_mode = False

        def _save_as(self) -> None:
            dest, _ = QFileDialog.getSaveFileName(
                self, 'Save Image',
                '',
                'PNG Files (*.png);;All Files (*)',
            )
            if not dest:
                return
            try:
                from src import vault
                vault.copy_out(self._image_path, dest)
            except Exception as e:
                QMessageBox.critical(self, 'Save Image', f'Failed to save image: {e}')

        def wheelEvent(self, event) -> None:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                if delta > 0:
                    self._zoom_in()
                else:
                    self._zoom_out()
                event.accept()
            else:
                super().wheelEvent(event)

        def keyPressEvent(self, event) -> None:
            key = event.key()
            if key == Qt.Key.Key_Equal and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._zoom_in()
                event.accept()
            elif key == Qt.Key.Key_Minus and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._zoom_out()
                event.accept()
            else:
                super().keyPressEvent(event)

        def showEvent(self, event) -> None:
            super().showEvent(event)
            if self._fit_mode:
                # Defer so the viewport has its final geometry.
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(0, self._fit_to_window)

        def resizeEvent(self, event) -> None:
            if self._fit_mode:
                self._fit_to_window()
            super().resizeEvent(event)

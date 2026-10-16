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


def wrap_index(current: int, step: int, count: int) -> int:
    """Return the index *step* away from *current*, wrapping over *count*.

    Pure function so the lightbox navigation can be unit-tested without Qt.
    """
    if count <= 0:
        return 0
    return (current + step) % count


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


    class GalleryLightbox(ImageViewer):
        """A multi-image viewer with arrow-key navigation.

        The list can mix gallery and expression images; ``labels`` names each
        entry in the window title (e.g. the expression name), and
        ``on_index_changed`` lets the calling grid keep its selection in sync
        while the user browses.
        """

        def __init__(self, image_paths: list[str], start_index: int = 0,
                     labels: list[str] | None = None,
                     title: str = 'Gallery',
                     on_index_changed=None,
                     parent=None):
            self._paths = list(image_paths or [])
            self._labels = list(labels or [])
            self._on_index_changed = on_index_changed
            self._base_title = title
            if not self._paths:
                raise ValueError('GalleryLightbox needs at least one image')
            index = min(max(0, start_index), len(self._paths) - 1)
            super().__init__(self._paths[index], title, parent)
            self._index = index
            self._update_title()

        def _entry_label(self) -> str:
            if self._index < len(self._labels) and self._labels[self._index]:
                return self._labels[self._index]
            from pathlib import Path
            return Path(self._paths[self._index]).stem

        def _update_title(self) -> None:
            self.setWindowTitle(
                f'{self._entry_label()} — {self._index + 1}/{len(self._paths)}'
            )

        def _show_index(self, index: int) -> None:
            if index == self._index or not self._paths:
                return
            self._index = index
            self._image_path = self._paths[index]
            self._scene.clear()
            self._load_image()
            self._update_title()
            if self._on_index_changed is not None:
                try:
                    self._on_index_changed(index)
                except Exception:
                    pass

        def next_image(self) -> None:
            self._show_index(wrap_index(self._index, 1, len(self._paths)))

        def previous_image(self) -> None:
            self._show_index(wrap_index(self._index, -1, len(self._paths)))

        def keyPressEvent(self, event) -> None:
            key = event.key()
            if key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_PageDown):
                self.next_image()
                event.accept()
            elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp):
                self.previous_image()
                event.accept()
            elif key == Qt.Key.Key_Home:
                self._show_index(0)
                event.accept()
            elif key == Qt.Key.Key_End:
                self._show_index(len(self._paths) - 1)
                event.accept()
            else:
                super().keyPressEvent(event)

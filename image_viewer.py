"""Reusable QGraphicsView image display with bounded zoom and middle-button pan."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QMouseEvent, QPixmap, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView


class ImageViewerError(Exception):
    """Raised when an image cannot be loaded safely into the viewer."""


class ImageGraphicsView(QGraphicsView):
    """Display an unmodified image in pixel-aligned scene coordinates."""

    ZOOM_STEP = 1.25
    MIN_SCALE = 0.05
    MAX_SCALE = 20.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image_scene = QGraphicsScene(self)
        self._pixmap_item = QGraphicsPixmapItem()
        self._pixmap_item.setPos(0, 0)
        self._image_scene.addItem(self._pixmap_item)
        self.setScene(self._image_scene)

        self._has_image = False
        self._fit_mode = True
        self._panning = False
        self._pan_start = QPoint()

        self.setBackgroundBrush(Qt.GlobalColor.darkGray)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)

    @property
    def pixmap_item(self) -> QGraphicsPixmapItem:
        """Expose the scene item for lightweight verification and future overlays."""
        return self._pixmap_item

    @property
    def has_image(self) -> bool:
        return self._has_image

    @property
    def fit_mode(self) -> bool:
        return self._fit_mode

    def set_image(
        self,
        path: str | Path,
        expected_width: int | None = None,
        expected_height: int | None = None,
    ) -> None:
        """Load one image lazily and place its top-left corner at scene (0, 0)."""
        image_path = Path(path)
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            raise ImageViewerError(f"图片无法由 QPixmap 读取：{image_path}")
        if expected_width is not None and pixmap.width() != expected_width:
            raise ImageViewerError(
                f"Tile 图片宽度与 Manifest 不一致：{image_path}\n"
                f"Manifest：{expected_width}，实际：{pixmap.width()}"
            )
        if expected_height is not None and pixmap.height() != expected_height:
            raise ImageViewerError(
                f"Tile 图片高度与 Manifest 不一致：{image_path}\n"
                f"Manifest：{expected_height}，实际：{pixmap.height()}"
            )

        self._pixmap_item.setPixmap(pixmap)
        self._pixmap_item.setPos(0, 0)
        self._image_scene.setSceneRect(
            QRectF(0.0, 0.0, float(pixmap.width()), float(pixmap.height()))
        )
        self._has_image = True
        self.fit_image()

    def clear_image(self) -> None:
        self._pixmap_item.setPixmap(QPixmap())
        self._pixmap_item.setPos(0, 0)
        self._image_scene.setSceneRect(QRectF())
        self.resetTransform()
        self._has_image = False
        self._fit_mode = True
        self._panning = False
        self.unsetCursor()

    def fit_image(self) -> None:
        """Fit the full image while preserving its aspect ratio."""
        if not self._has_image:
            return
        self.resetTransform()
        self.fitInView(
            self._pixmap_item,
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        self._fit_mode = True

    def reset_to_100_percent(self) -> None:
        """Restore the normal 1 scene unit to 1 view logical unit transform."""
        if not self._has_image:
            return
        self.resetTransform()
        self.centerOn(self._pixmap_item)
        self._fit_mode = False

    def zoom_in(self) -> None:
        self._apply_zoom(self.ZOOM_STEP, anchor_under_mouse=False)

    def zoom_out(self) -> None:
        self._apply_zoom(1.0 / self.ZOOM_STEP, anchor_under_mouse=False)

    def _apply_zoom(self, factor: float, anchor_under_mouse: bool) -> None:
        if not self._has_image:
            return
        current_scale = self.transform().m11()
        requested_scale = current_scale * factor
        if factor > 1.0:
            if current_scale >= self.MAX_SCALE:
                return
            bounded_scale = min(
                self.MAX_SCALE, max(self.MIN_SCALE, requested_scale)
            )
        else:
            if current_scale <= self.MIN_SCALE:
                return
            bounded_scale = max(
                self.MIN_SCALE, min(self.MAX_SCALE, requested_scale)
            )
        if abs(bounded_scale - current_scale) < 1e-12:
            return

        previous_anchor = self.transformationAnchor()
        desired_anchor = (
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
            if anchor_under_mouse
            else QGraphicsView.ViewportAnchor.AnchorViewCenter
        )
        self.setTransformationAnchor(desired_anchor)
        self.scale(bounded_scale / current_scale, bounded_scale / current_scale)
        self.setTransformationAnchor(previous_anchor)
        self._fit_mode = False

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._has_image or event.angleDelta().y() == 0:
            super().wheelEvent(event)
            return
        factor = self.ZOOM_STEP if event.angleDelta().y() > 0 else 1.0 / self.ZOOM_STEP
        self._apply_zoom(factor, anchor_under_mouse=True)
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._has_image and event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._panning:
            current_position = event.position().toPoint()
            delta = current_position - self._pan_start
            if not delta.isNull():
                self.horizontalScrollBar().setValue(
                    self.horizontalScrollBar().value() - delta.x()
                )
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() - delta.y()
                )
                self._pan_start = current_position
                self._fit_mode = False
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._panning and event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._fit_mode and self._has_image:
            self.fit_image()

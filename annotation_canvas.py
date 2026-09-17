"""QGraphicsView annotation canvas built on the Stage 5 image viewer."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsSimpleTextItem,
)

from annotation_model import (
    BBoxAnnotation,
    FOD_CLASSES,
    AnnotationValidationError,
    normalize_bbox,
    validate_annotation,
    validate_class_name,
)
from image_viewer import ImageGraphicsView


CLASS_COLORS = {
    "metal": QColor("#ff3b30"),
    "stone": QColor("#ff9500"),
    "rubber": QColor("#34c759"),
    "other": QColor("#00b7ff"),
}


class BBoxGraphicsItem(QGraphicsRectItem):
    """Selectable, non-movable visual representation of one annotation."""

    def __init__(self, annotation: BBoxAnnotation):
        x1, y1, x2, y2 = annotation.bbox
        super().__init__(QRectF(x1, y1, x2 - x1, y2 - y1))
        self._annotation = annotation
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.setZValue(10)

        self._label = QGraphicsSimpleTextItem(self)
        self._label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._label.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True
        )
        self._label.setPos(float(x1), float(y1))
        self._refresh_label()
        self._apply_style(False)

    @property
    def annotation(self) -> BBoxAnnotation:
        return self._annotation

    def set_class_name(self, class_name: str) -> None:
        self._annotation = self._annotation.with_class(class_name)
        self._refresh_label()
        self._apply_style(self.isSelected())

    def _refresh_label(self) -> None:
        self._label.setText(self._annotation.class_name)
        self._label.setBrush(CLASS_COLORS[self._annotation.class_name])

    def _apply_style(self, selected: bool) -> None:
        color = CLASS_COLORS[self._annotation.class_name]
        if selected:
            color = color.lighter(145)
        pen = QPen(color)
        pen.setCosmetic(True)
        pen.setWidth(4 if selected else 2)
        self.setPen(pen)
        self.update()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._apply_style(bool(value))
        return super().itemChange(change, value)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRect(self.rect())
        return path


class AnnotationGraphicsView(ImageGraphicsView):
    """Image viewer supporting in-memory bbox creation and selection."""

    annotations_changed = Signal(str)
    selection_changed = Signal(object)
    drawing_blocked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active_class = FOD_CLASSES[0]
        self._drawing_enabled = True
        self._bbox_items: list[BBoxGraphicsItem] = []
        self._drawing_start: QPointF | None = None
        self._preview_item: QGraphicsRectItem | None = None
        self.scene().selectionChanged.connect(self._on_scene_selection_changed)

    @property
    def active_class(self) -> str:
        return self._active_class

    def set_active_class(self, class_name: str) -> None:
        self._active_class = validate_class_name(class_name)

    @property
    def drawing_enabled(self) -> bool:
        return self._drawing_enabled

    def set_drawing_enabled(self, enabled: bool) -> None:
        """Enable or block only new bbox creation; viewing stays available."""
        self._drawing_enabled = bool(enabled)
        if not self._drawing_enabled:
            self.cancel_drawing()

    def set_image(
        self,
        path: str | Path,
        expected_width: int | None = None,
        expected_height: int | None = None,
    ) -> None:
        # The base class validates the new pixmap before changing the old one.
        super().set_image(path, expected_width, expected_height)
        self.cancel_drawing()
        self.clear_annotations()

    def clear_image(self) -> None:
        self.cancel_drawing()
        self.clear_annotations()
        super().clear_image()

    def set_annotations(self, annotations: Iterable[BBoxAnnotation]) -> None:
        """Replace Scene boxes without treating Tile loading as an edit."""
        annotations_list = list(annotations)
        width, height = self._image_dimensions()
        for annotation in annotations_list:
            validate_annotation(annotation, width, height)
        self.cancel_drawing()
        self.clear_annotations()
        for annotation in annotations_list:
            self._append_annotation_item(annotation, select=False)
        self.scene().clearSelection()

    def get_annotations(self) -> list[BBoxAnnotation]:
        return [item.annotation for item in self._bbox_items]

    def clear_annotations(self) -> None:
        self.cancel_drawing()
        for item in self._bbox_items:
            self.scene().removeItem(item)
        self._bbox_items.clear()
        self.selection_changed.emit(None)

    def add_annotation(
        self, annotation: BBoxAnnotation, select: bool = True
    ) -> BBoxGraphicsItem:
        width, height = self._image_dimensions()
        validate_annotation(annotation, width, height)
        item = self._append_annotation_item(annotation, select=select)
        self.annotations_changed.emit("bbox_added")
        return item

    def selected_annotation(self) -> BBoxAnnotation | None:
        item = self._selected_bbox_item()
        return item.annotation if item is not None else None

    def selected_annotation_index(self) -> int:
        item = self._selected_bbox_item()
        if item is None:
            return -1
        return self._bbox_items.index(item)

    def select_annotation(self, index: int) -> bool:
        if not 0 <= index < len(self._bbox_items):
            self.scene().clearSelection()
            return False
        self.scene().clearSelection()
        self._bbox_items[index].setSelected(True)
        return True

    def delete_selected_annotation(self) -> bool:
        item = self._selected_bbox_item()
        if item is None:
            return False
        self._bbox_items.remove(item)
        self.scene().removeItem(item)
        self.annotations_changed.emit("bbox_deleted")
        self.selection_changed.emit(None)
        return True

    def change_selected_class(self, class_name: str) -> bool:
        validate_class_name(class_name)
        item = self._selected_bbox_item()
        if item is None:
            return False
        item.set_class_name(class_name)
        self.annotations_changed.emit("bbox_class_changed")
        self.selection_changed.emit(item.annotation)
        return True

    def cancel_drawing(self) -> None:
        if self._preview_item is not None:
            self.scene().removeItem(self._preview_item)
        self._preview_item = None
        self._drawing_start = None

    def _append_annotation_item(
        self, annotation: BBoxAnnotation, select: bool
    ) -> BBoxGraphicsItem:
        item = BBoxGraphicsItem(annotation)
        self.scene().addItem(item)
        self._bbox_items.append(item)
        if select:
            self.scene().clearSelection()
            item.setSelected(True)
        return item

    def _image_dimensions(self) -> tuple[int, int]:
        if not self.has_image:
            raise AnnotationValidationError("当前没有可标注的 Tile 图片。")
        pixmap = self.pixmap_item.pixmap()
        return pixmap.width(), pixmap.height()

    def _selected_bbox_item(self) -> BBoxGraphicsItem | None:
        for item in self._bbox_items:
            if item.isSelected():
                return item
        return None

    def _bbox_item_at(self, view_position) -> BBoxGraphicsItem | None:
        for item in self.items(view_position):
            candidate = item
            while candidate is not None:
                if isinstance(candidate, BBoxGraphicsItem):
                    return candidate
                candidate = candidate.parentItem()
        return None

    def _point_inside_image(self, point: QPointF) -> bool:
        if not self.has_image:
            return False
        width, height = self._image_dimensions()
        return 0.0 <= point.x() < width and 0.0 <= point.y() < height

    def _clamp_to_image(self, point: QPointF) -> QPointF:
        width, height = self._image_dimensions()
        return QPointF(
            max(0.0, min(float(width), point.x())),
            max(0.0, min(float(height), point.y())),
        )

    def _create_preview_item(self) -> QGraphicsRectItem:
        preview = QGraphicsRectItem()
        pen = QPen(QColor("#ffffff"))
        pen.setCosmetic(True)
        pen.setWidth(2)
        pen.setStyle(Qt.PenStyle.DashLine)
        preview.setPen(pen)
        preview.setBrush(QColor(255, 255, 255, 24))
        preview.setZValue(20)
        self.scene().addItem(preview)
        return preview

    def _on_scene_selection_changed(self) -> None:
        annotation = self.selected_annotation()
        self.selection_changed.emit(annotation)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self.cancel_drawing()
            super().mousePressEvent(event)
            return
        if event.button() != Qt.MouseButton.LeftButton or not self.has_image:
            super().mousePressEvent(event)
            return

        existing_item = self._bbox_item_at(event.position().toPoint())
        if existing_item is not None:
            self.scene().clearSelection()
            existing_item.setSelected(True)
            event.accept()
            return

        if not self._drawing_enabled:
            self.drawing_blocked.emit()
            event.accept()
            return

        scene_point = self.mapToScene(event.position().toPoint())
        if not self._point_inside_image(scene_point):
            self.scene().clearSelection()
            event.accept()
            return

        self.scene().clearSelection()
        self._drawing_start = scene_point
        self._preview_item = self._create_preview_item()
        self._preview_item.setRect(QRectF(scene_point, scene_point))
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drawing_start is not None and self._preview_item is not None:
            current_point = self._clamp_to_image(
                self.mapToScene(event.position().toPoint())
            )
            self._preview_item.setRect(
                QRectF(self._drawing_start, current_point).normalized()
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._drawing_start is not None
        ):
            start_point = self._drawing_start
            end_point = self.mapToScene(event.position().toPoint())
            width, height = self._image_dimensions()
            self.cancel_drawing()
            bbox = normalize_bbox(
                start_point.x(),
                start_point.y(),
                end_point.x(),
                end_point.y(),
                width,
                height,
            )
            if bbox is not None:
                self.add_annotation(
                    BBoxAnnotation(class_name=self._active_class, bbox=bbox)
                )
            event.accept()
            return
        super().mouseReleaseEvent(event)

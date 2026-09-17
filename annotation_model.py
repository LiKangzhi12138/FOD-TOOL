"""Pure in-memory Tile/bbox models, validation, and coordinate normalization."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field


FOD_CLASSES = ("metal", "stone", "rubber", "other")
FOD_CLASS_LABELS = {
    "metal": "金属异物",
    "stone": "石块异物",
    "rubber": "橡胶异物",
    "other": "其它异物",
}
MIN_BBOX_SIZE = 2
VALID_TILE_STATUSES = (
    "positive",
    "negative",
    "exclude",
    "uncertain",
)
TILE_STATUS_LABELS = {
    "positive": "有 FOD / Positive",
    "negative": "无 FOD 有效场地 / Negative",
    "exclude": "非场地 / Exclude",
    "uncertain": "待确认 / Uncertain",
}


class AnnotationValidationError(ValueError):
    """Raised when an annotation class or pixel bbox is invalid."""


def validate_class_name(class_name: str) -> str:
    """Return a valid canonical English FOD class name."""
    if class_name not in FOD_CLASSES:
        raise AnnotationValidationError(
            f"非法 bbox 类别：{class_name!r}；合法类别为 {', '.join(FOD_CLASSES)}"
        )
    return class_name


def validate_tile_status(status: str | None) -> str | None:
    """Validate a canonical Tile status; None is the unannotated state."""
    if status is not None and status not in VALID_TILE_STATUSES:
        raise AnnotationValidationError(
            f"非法 Tile 状态：{status!r}；合法状态为 "
            f"{', '.join(VALID_TILE_STATUSES)}，或使用 None 表示未标注。"
        )
    return status


def derive_trainable(status: str | None) -> bool:
    """Derive whether a Tile participates in training from its status."""
    validate_tile_status(status)
    return status in {"positive", "negative"}


def drawing_enabled_for_status(status: str | None) -> bool:
    """Return whether the status permits creation of new bbox objects."""
    validate_tile_status(status)
    return status in {None, "positive", "uncertain"}


def validate_status_objects(
    status: str | None, objects: Iterable["BBoxAnnotation"]
) -> None:
    """Validate the semantic relationship between Tile status and objects."""
    validate_tile_status(status)
    object_list = list(objects)
    if any(not isinstance(item, BBoxAnnotation) for item in object_list):
        raise AnnotationValidationError(
            "TileAnnotation.objects 必须全部是 BBoxAnnotation。"
        )
    object_count = len(object_list)
    if status == "positive" and object_count == 0:
        raise AnnotationValidationError(
            "Positive 状态要求至少存在一个 FOD bbox。请先绘制 bbox。"
        )
    if status == "negative" and object_count > 0:
        raise AnnotationValidationError(
            f"当前 Tile 已存在 {object_count} 个 bbox，不能设为 negative。"
            "请先删除 bbox，或选择 positive / uncertain。"
        )
    if status == "exclude" and object_count > 0:
        raise AnnotationValidationError(
            f"当前 Tile 已存在 {object_count} 个 bbox，不能设为 exclude。"
            "请先删除 bbox，或选择 positive / uncertain。"
        )


def validate_bbox_deletion(status: str | None, object_count: int) -> None:
    """Reject a deletion that would make the current Tile state invalid."""
    validate_tile_status(status)
    if (
        not isinstance(object_count, int)
        or isinstance(object_count, bool)
        or object_count <= 0
    ):
        raise AnnotationValidationError("删除 bbox 时当前对象数量必须大于 0。")
    if status == "positive" and object_count == 1:
        raise AnnotationValidationError(
            "这是当前 Positive Tile 的最后一个 bbox。\n"
            "删除后 Positive 状态将不再合法。\n\n"
            "请先修改 Tile 状态，或取消删除。"
        )
    if status in {"negative", "exclude"}:
        raise AnnotationValidationError(
            f"检测到内部数据不一致：{status} 状态不应包含 bbox。"
        )


def validate_bbox(
    bbox: tuple[int, int, int, int],
    image_width: int | None = None,
    image_height: int | None = None,
) -> tuple[int, int, int, int]:
    """Validate integer boundary coordinates and optional image bounds."""
    if not isinstance(bbox, tuple) or len(bbox) != 4:
        raise AnnotationValidationError("bbox 必须是包含 4 个整数的 tuple。")
    if any(not isinstance(value, int) or isinstance(value, bool) for value in bbox):
        raise AnnotationValidationError("bbox 坐标必须全部是整数。")
    x1, y1, x2, y2 = bbox
    if x1 < 0 or y1 < 0:
        raise AnnotationValidationError("bbox 左边界和上边界不能小于 0。")
    if x2 - x1 < MIN_BBOX_SIZE or y2 - y1 < MIN_BBOX_SIZE:
        raise AnnotationValidationError(
            f"bbox 宽和高都必须至少为 {MIN_BBOX_SIZE} 像素。"
        )
    if image_width is not None:
        if image_width <= 0:
            raise AnnotationValidationError("image_width 必须大于 0。")
        if x2 > image_width:
            raise AnnotationValidationError("bbox 右边界超出图片宽度。")
    if image_height is not None:
        if image_height <= 0:
            raise AnnotationValidationError("image_height 必须大于 0。")
        if y2 > image_height:
            raise AnnotationValidationError("bbox 下边界超出图片高度。")
    return bbox


def normalize_bbox(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    image_width: int,
    image_height: int,
) -> tuple[int, int, int, int] | None:
    """Floor left/top, ceil right/bottom, clamp, and reject sub-2px boxes."""
    if (
        not isinstance(image_width, int)
        or isinstance(image_width, bool)
        or image_width <= 0
        or not isinstance(image_height, int)
        or isinstance(image_height, bool)
        or image_height <= 0
    ):
        raise AnnotationValidationError("图片宽高必须是大于 0 的整数。")
    coordinates = (start_x, start_y, end_x, end_y)
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        for value in coordinates
    ):
        raise AnnotationValidationError("Scene 坐标必须是有限数值。")

    x1 = max(0, min(image_width, math.floor(min(start_x, end_x))))
    y1 = max(0, min(image_height, math.floor(min(start_y, end_y))))
    x2 = max(0, min(image_width, math.ceil(max(start_x, end_x))))
    y2 = max(0, min(image_height, math.ceil(max(start_y, end_y))))
    bbox = (x1, y1, x2, y2)
    if x2 - x1 < MIN_BBOX_SIZE or y2 - y1 < MIN_BBOX_SIZE:
        return None
    return validate_bbox(bbox, image_width, image_height)


@dataclass(frozen=True)
class BBoxAnnotation:
    """One immutable bbox in Tile pixel boundary coordinates."""

    class_name: str
    bbox: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        validate_class_name(self.class_name)
        validate_bbox(self.bbox)

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]

    def with_class(self, class_name: str) -> "BBoxAnnotation":
        return BBoxAnnotation(
            class_name=validate_class_name(class_name), bbox=self.bbox
        )


@dataclass
class TileAnnotation:
    """One Tile's in-memory status and ordered bbox objects."""

    status: str | None = None
    objects: list[BBoxAnnotation] = field(default_factory=list)
    notes: str = ""
    completed: bool = False
    completed_at: str | None = None

    def __post_init__(self) -> None:
        self.objects = list(self.objects)
        validate_status_objects(self.status, self.objects)
        if not isinstance(self.notes, str):
            raise AnnotationValidationError("Tile notes 必须是字符串。")
        if not isinstance(self.completed, bool):
            raise AnnotationValidationError("Tile completed 必须是布尔值。")
        if self.completed:
            if self.status is None:
                raise AnnotationValidationError(
                    "未标注 Tile 不能设置 completed=true。"
                )
            if not isinstance(self.completed_at, str) or not self.completed_at:
                raise AnnotationValidationError(
                    "completed=true 时 completed_at 必须是 UTC 时间字符串。"
                )
        elif self.completed_at is not None:
            raise AnnotationValidationError(
                "completed=false 时 completed_at 必须为 None。"
            )

    @property
    def trainable(self) -> bool:
        return derive_trainable(self.status)

    def with_status(self, status: str | None) -> "TileAnnotation":
        """Return a validated copy with a different status."""
        changed = status != self.status
        return TileAnnotation(
            status=status,
            objects=self.objects,
            notes=self.notes,
            completed=False if changed else self.completed,
            completed_at=None if changed else self.completed_at,
        )

    def with_objects(
        self, objects: Iterable[BBoxAnnotation]
    ) -> "TileAnnotation":
        """Return a validated copy with a different ordered object list."""
        object_list = list(objects)
        changed = object_list != self.objects
        return TileAnnotation(
            status=self.status,
            objects=object_list,
            notes=self.notes,
            completed=False if changed else self.completed,
            completed_at=None if changed else self.completed_at,
        )

    def with_notes(self, notes: str) -> "TileAnnotation":
        """Return a copy with notes changed and completion invalidated."""
        if not isinstance(notes, str):
            raise AnnotationValidationError("Tile notes 必须是字符串。")
        changed = notes != self.notes
        return TileAnnotation(
            status=self.status,
            objects=self.objects,
            notes=notes,
            completed=False if changed else self.completed,
            completed_at=None if changed else self.completed_at,
        )

    def mark_completed(self, completed_at: str) -> "TileAnnotation":
        """Return a completed copy after validating status and objects."""
        validate_tile_completion(self)
        return TileAnnotation(
            status=self.status,
            objects=self.objects,
            notes=self.notes,
            completed=True,
            completed_at=completed_at,
        )

    def mark_incomplete(self) -> "TileAnnotation":
        """Return a copy with only the completion confirmation cleared."""
        return TileAnnotation(
            status=self.status,
            objects=self.objects,
            notes=self.notes,
            completed=False,
            completed_at=None,
        )


def validate_annotation(
    annotation: BBoxAnnotation, image_width: int, image_height: int
) -> BBoxAnnotation:
    """Validate one annotation against a particular Tile's dimensions."""
    if not isinstance(annotation, BBoxAnnotation):
        raise AnnotationValidationError("annotation 必须是 BBoxAnnotation。")
    validate_class_name(annotation.class_name)
    validate_bbox(annotation.bbox, image_width, image_height)
    return annotation


def validate_tile_annotation(tile_annotation: TileAnnotation) -> TileAnnotation:
    """Validate a complete in-memory Tile annotation object."""
    if not isinstance(tile_annotation, TileAnnotation):
        raise AnnotationValidationError(
            "tile_annotation 必须是 TileAnnotation。"
        )
    validate_status_objects(tile_annotation.status, tile_annotation.objects)
    if not isinstance(tile_annotation.notes, str):
        raise AnnotationValidationError("Tile notes 必须是字符串。")
    if not isinstance(tile_annotation.completed, bool):
        raise AnnotationValidationError("Tile completed 必须是布尔值。")
    if tile_annotation.completed:
        if tile_annotation.status is None:
            raise AnnotationValidationError(
                "未标注 Tile 不能设置 completed=true。"
            )
        if (
            not isinstance(tile_annotation.completed_at, str)
            or not tile_annotation.completed_at
        ):
            raise AnnotationValidationError(
                "completed=true 时 completed_at 必须是 UTC 时间字符串。"
            )
    elif tile_annotation.completed_at is not None:
        raise AnnotationValidationError(
            "completed=false 时 completed_at 必须为 None。"
        )
    return tile_annotation


def validate_tile_completion(tile_annotation: TileAnnotation) -> TileAnnotation:
    """Require an explicit, semantically valid status before completion."""
    validate_tile_annotation(tile_annotation)
    if tile_annotation.status is None:
        raise AnnotationValidationError(
            "当前 Tile 尚未设置状态，不能标记为完成。"
        )
    return tile_annotation


def can_complete_tile(
    status: str | None, objects: Iterable[BBoxAnnotation]
) -> bool:
    """Return whether status and objects satisfy the Stage 7 completion rule."""
    try:
        validate_tile_completion(TileAnnotation(status=status, objects=list(objects)))
    except AnnotationValidationError:
        return False
    return True

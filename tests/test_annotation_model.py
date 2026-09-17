import pytest

from annotation_model import (
    AnnotationValidationError,
    BBoxAnnotation,
    FOD_CLASSES,
    normalize_bbox,
    validate_annotation,
)


def test_fod_classes_are_fixed_and_ordered():
    assert FOD_CLASSES == ("metal", "stone", "rubber", "other")


def test_normalize_bbox_uses_floor_for_left_top_and_ceil_for_right_bottom():
    assert normalize_bbox(10.2, 20.7, 50.1, 70.2, 1024, 1024) == (
        10,
        20,
        51,
        71,
    )


def test_normalize_bbox_supports_reverse_dragging():
    assert normalize_bbox(50, 70, 10, 20, 1024, 1024) == (10, 20, 50, 70)


def test_normalize_bbox_clamps_to_image_boundaries():
    assert normalize_bbox(-8.5, -4.2, 1100.1, 1200.7, 1024, 1024) == (
        0,
        0,
        1024,
        1024,
    )


def test_two_by_two_bbox_is_valid():
    annotation = BBoxAnnotation("metal", (10, 20, 12, 22))
    assert validate_annotation(annotation, 1024, 1024) is annotation
    assert annotation.width == 2
    assert annotation.height == 2


@pytest.mark.parametrize(
    "coordinates",
    [
        (10, 10, 11, 11),
        (10, 10, 10, 12),
    ],
)
def test_one_pixel_or_zero_size_bbox_is_rejected(coordinates):
    assert normalize_bbox(*coordinates, 1024, 1024) is None


def test_invalid_class_name_is_rejected():
    with pytest.raises(AnnotationValidationError, match="非法 bbox 类别"):
        BBoxAnnotation("unknown", (10, 10, 20, 20))


def test_bbox_touching_bottom_right_edge_is_valid():
    annotation = BBoxAnnotation("other", (1000, 1000, 1024, 1024))
    assert validate_annotation(annotation, 1024, 1024) is annotation


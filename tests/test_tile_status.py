import pytest

from annotation_model import (
    AnnotationValidationError,
    BBoxAnnotation,
    TileAnnotation,
    VALID_TILE_STATUSES,
    can_complete_tile,
    derive_trainable,
    drawing_enabled_for_status,
    validate_bbox_deletion,
)


BBOX_1 = BBoxAnnotation("metal", (10, 20, 30, 40))
BBOX_2 = BBoxAnnotation("stone", (50, 60, 80, 90))


def test_valid_tile_statuses_are_fixed_and_ordered():
    assert VALID_TILE_STATUSES == (
        "positive",
        "negative",
        "exclude",
        "uncertain",
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("positive", True),
        ("negative", True),
        ("exclude", False),
        ("uncertain", False),
        (None, False),
    ],
)
def test_trainable_is_derived_from_status(status, expected):
    assert derive_trainable(status) is expected


def test_positive_requires_at_least_one_bbox():
    annotation = TileAnnotation(status="positive", objects=[BBOX_1])
    assert annotation.trainable

    with pytest.raises(AnnotationValidationError, match="至少存在一个"):
        TileAnnotation(status="positive", objects=[])


def test_negative_requires_zero_bbox():
    annotation = TileAnnotation(status="negative", objects=[])
    assert annotation.trainable

    with pytest.raises(AnnotationValidationError, match="不能设为 negative"):
        TileAnnotation(status="negative", objects=[BBOX_1])


def test_exclude_requires_zero_bbox():
    annotation = TileAnnotation(status="exclude", objects=[])
    assert not annotation.trainable

    with pytest.raises(AnnotationValidationError, match="不能设为 exclude"):
        TileAnnotation(status="exclude", objects=[BBOX_1])


def test_uncertain_allows_zero_or_more_bbox():
    assert TileAnnotation(status="uncertain", objects=[]).objects == []
    assert TileAnnotation(status="uncertain", objects=[BBOX_1]).objects == [
        BBOX_1
    ]


def test_none_is_the_unannotated_state_and_allows_bbox():
    annotation = TileAnnotation(status=None, objects=[BBOX_1])
    assert annotation.status is None
    assert not annotation.trainable


def test_status_transitions_validate_without_changing_objects():
    empty = TileAnnotation()
    with pytest.raises(AnnotationValidationError):
        empty.with_status("positive")

    with_object = TileAnnotation(objects=[BBOX_1])
    with pytest.raises(AnnotationValidationError):
        with_object.with_status("negative")
    with pytest.raises(AnnotationValidationError):
        with_object.with_status("exclude")
    assert with_object.with_status("uncertain").objects == [BBOX_1]
    assert with_object.with_status(None).objects == [BBOX_1]


def test_positive_last_bbox_deletion_is_rejected_but_one_of_two_is_allowed():
    with pytest.raises(AnnotationValidationError, match="最后一个 bbox"):
        validate_bbox_deletion("positive", 1)

    validate_bbox_deletion("positive", 2)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, True),
        ("positive", True),
        ("uncertain", True),
        ("negative", False),
        ("exclude", False),
    ],
)
def test_drawing_enabled_is_derived_from_status(status, expected):
    assert drawing_enabled_for_status(status) is expected


@pytest.mark.parametrize(
    ("status", "objects", "expected"),
    [
        (None, [], False),
        ("positive", [], False),
        ("positive", [BBOX_1], True),
        ("negative", [], True),
        ("exclude", [], True),
        ("uncertain", [], True),
        ("uncertain", [BBOX_1], True),
    ],
)
def test_tile_completion_rules(status, objects, expected):
    assert can_complete_tile(status, objects) is expected


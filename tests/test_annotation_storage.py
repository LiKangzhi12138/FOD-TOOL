import json

import pytest
from PIL import Image

import annotation_storage
from annotation_model import BBoxAnnotation, TileAnnotation
from annotation_storage import (
    AnnotationStorage,
    AnnotationStorageError,
    TOP_LEVEL_FIELDS,
    TILE_FIELDS,
)
from source_manager import (
    SourceRecord,
    atomic_write_json,
    generate_source_id,
    write_sources_manifest,
)
from tile_manager import TileRecord, build_tile_id, write_tiles_manifest


INITIAL_TIME = "2026-09-16T06:30:00Z"
UPDATED_TIME = "2026-09-16T06:45:00Z"


def build_stage4_project(tmp_path, source_count=1, tiles_per_source=2):
    output_root = tmp_path / "output"
    output_root.mkdir()
    sources = []
    tiles = []
    for source_index in range(source_count):
        sha256_hex = f"{source_index + 1:x}" * 64
        source_id = generate_source_id("S01", sha256_hex)
        source = SourceRecord(
            schema_version="1.0",
            source_id=source_id,
            scene="S01",
            group_id=source_id,
            original_filename=f"source_{source_index}.jpg",
            original_relative_path=f"source_{source_index}.jpg",
            original_sha256=sha256_hex,
            original_width=tiles_per_source * 64,
            original_height=64,
            tile_size=64,
            overlap=0,
            stride=64,
            annotator_id="A01",
            created_at=INITIAL_TIME,
        )
        sources.append(source)
        atomic_write_json(
            output_root / "sources" / source_id / "source.json",
            source.to_dict(),
        )
        for tile_index in range(tiles_per_source):
            x = tile_index * 64
            tile_id = build_tile_id(source_id, x, 0)
            relative_path = f"sources/{source_id}/tiles/{tile_id}.jpg"
            tile = TileRecord(
                tile_id=tile_id,
                source_id=source_id,
                scene="S01",
                x=x,
                y=0,
                width=64,
                height=64,
                tile_relative_path=relative_path,
                annotator="A01",
            )
            tiles.append(tile)
            image_path = output_root.joinpath(*relative_path.split("/"))
            image_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (64, 64), (30, 60, 90)).save(
                image_path, format="JPEG"
            )
    write_sources_manifest(output_root / "manifests" / "sources.csv", sources)
    write_tiles_manifest(output_root / "manifests" / "tiles.csv", tiles)
    return output_root, sources, tiles


def annotation_path(output_root, source_id):
    return output_root / "sources" / source_id / "annotations.json"


def read_annotation_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rewrite_annotation_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def initialized_project(tmp_path, source_count=1, tiles_per_source=2):
    output_root, sources, tiles = build_stage4_project(
        tmp_path, source_count=source_count, tiles_per_source=tiles_per_source
    )
    storage = AnnotationStorage.open_or_initialize(
        output_root, "A01", now_provider=lambda: INITIAL_TIME
    )
    return output_root, sources, tiles, storage


def test_initialize_annotations_uses_manifest_count_order_and_defaults(tmp_path):
    output_root, sources, tiles, storage = initialized_project(tmp_path)
    path = annotation_path(output_root, sources[0].source_id)
    data = read_annotation_json(path)

    assert path.is_file()
    assert list(data) == list(TOP_LEVEL_FIELDS)
    assert len(data["tiles"]) == len(tiles)
    assert [item["tile_id"] for item in data["tiles"]] == [
        item.tile_id for item in tiles
    ]
    assert list(data["tiles"][0]) == list(TILE_FIELDS)
    assert data["tiles"][0] == {
        "tile_id": tiles[0].tile_id,
        "x": 0,
        "y": 0,
        "width": 64,
        "height": 64,
        "status": None,
        "trainable": False,
        "objects": [],
        "annotator": "A01",
        "created_by": "A01",
        "review_status": None,
        "reviewed_by": None,
        "notes": "",
        "completed": False,
        "completed_at": None,
        "created_at": INITIAL_TIME,
        "updated_at": INITIAL_TIME,
    }
    assert not list(output_root.glob(".tmp_annotation_init_*"))
    storage.close()


def test_round_trip_restores_bbox_unicode_notes_and_completion(tmp_path):
    output_root, sources, tiles, storage = initialized_project(tmp_path)
    tile_id = tiles[0].tile_id
    annotation = TileAnnotation(
        status="positive",
        objects=[
            BBoxAnnotation("metal", (2, 3, 12, 13)),
            BBoxAnnotation("other", (20, 21, 30, 31)),
        ],
        notes="疑似金属异物，需要复核。",
        completed=True,
        completed_at=UPDATED_TIME,
    )
    storage._now_provider = lambda: UPDATED_TIME
    storage.save_tile_annotation(tile_id, annotation, "round_trip")
    storage.close()

    loaded = AnnotationStorage.open_or_initialize(output_root, "A01")
    restored = loaded.get_tile_annotation(tile_id)
    assert restored == annotation
    data = read_annotation_json(annotation_path(output_root, sources[0].source_id))
    assert data["created_at"] == INITIAL_TIME
    assert data["updated_at"] == UPDATED_TIME
    assert data["tiles"][0]["created_at"] == INITIAL_TIME
    assert data["tiles"][0]["updated_at"] == UPDATED_TIME
    loaded.close()


def test_load_rejects_trainable_inconsistent_with_status(tmp_path):
    output_root, sources, _tiles, storage = initialized_project(tmp_path)
    storage.close()
    path = annotation_path(output_root, sources[0].source_id)
    data = read_annotation_json(path)
    data["tiles"][0]["status"] = "uncertain"
    data["tiles"][0]["trainable"] = True
    rewrite_annotation_json(path, data)

    with pytest.raises(AnnotationStorageError, match="trainable 与 status 不一致"):
        AnnotationStorage.open_or_initialize(output_root, "A01")


@pytest.mark.parametrize("status", ["positive", "negative", "exclude"])
def test_load_rejects_invalid_status_object_combination(tmp_path, status):
    output_root, sources, _tiles, storage = initialized_project(tmp_path)
    storage.close()
    path = annotation_path(output_root, sources[0].source_id)
    data = read_annotation_json(path)
    data["tiles"][0]["status"] = status
    data["tiles"][0]["trainable"] = status in {"positive", "negative"}
    data["tiles"][0]["objects"] = (
        []
        if status == "positive"
        else [{"class_name": "metal", "bbox": [2, 2, 10, 10]}]
    )
    rewrite_annotation_json(path, data)

    with pytest.raises(AnnotationStorageError):
        AnnotationStorage.open_or_initialize(output_root, "A01")


@pytest.mark.parametrize(
    "bad_object",
    [
        {"class_name": "unknown", "bbox": [2, 2, 10, 10]},
        {"class_name": "metal", "bbox": [2, 2, 80, 10]},
        {"class_name": "metal", "bbox": [10, 2, 5, 10]},
        {"class_name": "metal", "bbox": [2.5, 2, 10, 10]},
        {"class_name": "metal", "bbox": [2, 2, 3, 3]},
    ],
)
def test_load_rejects_invalid_bbox_data(tmp_path, bad_object):
    output_root, sources, _tiles, storage = initialized_project(tmp_path)
    storage.close()
    path = annotation_path(output_root, sources[0].source_id)
    data = read_annotation_json(path)
    data["tiles"][0]["status"] = "uncertain"
    data["tiles"][0]["objects"] = [bad_object]
    rewrite_annotation_json(path, data)

    with pytest.raises(AnnotationStorageError):
        AnnotationStorage.open_or_initialize(output_root, "A01")


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate"])
def test_load_rejects_wrong_tile_collection(tmp_path, mutation):
    output_root, sources, _tiles, storage = initialized_project(tmp_path)
    storage.close()
    path = annotation_path(output_root, sources[0].source_id)
    data = read_annotation_json(path)
    if mutation == "missing":
        data["tiles"].pop()
    elif mutation == "extra":
        extra = dict(data["tiles"][0])
        extra["tile_id"] += "_EXTRA"
        data["tiles"].append(extra)
    else:
        data["tiles"][1]["tile_id"] = data["tiles"][0]["tile_id"]
    rewrite_annotation_json(path, data)

    with pytest.raises(AnnotationStorageError):
        AnnotationStorage.open_or_initialize(output_root, "A01")


def test_load_rejects_tile_geometry_mismatch(tmp_path):
    output_root, sources, _tiles, storage = initialized_project(tmp_path)
    storage.close()
    path = annotation_path(output_root, sources[0].source_id)
    data = read_annotation_json(path)
    data["tiles"][1]["x"] += 1
    rewrite_annotation_json(path, data)

    with pytest.raises(AnnotationStorageError, match="与 tiles.csv 不一致"):
        AnnotationStorage.open_or_initialize(output_root, "A01")


@pytest.mark.parametrize("field", ["source_id", "scene", "annotator_id"])
def test_load_rejects_source_metadata_mismatch(tmp_path, field):
    output_root, sources, _tiles, storage = initialized_project(tmp_path)
    storage.close()
    path = annotation_path(output_root, sources[0].source_id)
    data = read_annotation_json(path)
    data[field] = "WRONG"
    rewrite_annotation_json(path, data)

    with pytest.raises(AnnotationStorageError, match=field):
        AnnotationStorage.open_or_initialize(output_root, "A01")


def test_current_annotator_must_match_existing_worker_metadata(tmp_path):
    output_root, _sources, _tiles, storage = initialized_project(tmp_path)
    storage.close()

    with pytest.raises(AnnotationStorageError, match="annotator_id"):
        AnnotationStorage.open_or_initialize(output_root, "A02")


def test_partial_annotation_initialization_is_rejected(tmp_path):
    output_root, sources, _tiles, storage = initialized_project(
        tmp_path, source_count=2
    )
    storage.close()
    annotation_path(output_root, sources[1].source_id).unlink()

    with pytest.raises(AnnotationStorageError, match="仅部分初始化"):
        AnnotationStorage.open_or_initialize(output_root, "A01")


def test_unknown_schema_version_is_rejected(tmp_path):
    output_root, sources, _tiles, storage = initialized_project(tmp_path)
    storage.close()
    path = annotation_path(output_root, sources[0].source_id)
    data = read_annotation_json(path)
    data["schema_version"] = "9.9"
    rewrite_annotation_json(path, data)

    with pytest.raises(AnnotationStorageError, match="schema_version"):
        AnnotationStorage.open_or_initialize(output_root, "A01")


@pytest.mark.parametrize(
    "mutator",
    [
        lambda tile: tile.update(
            status=None,
            trainable=False,
            completed=True,
            completed_at=UPDATED_TIME,
        ),
        lambda tile: tile.update(
            status="positive",
            trainable=True,
            objects=[{"class_name": "metal", "bbox": [2, 2, 10, 10]}],
            completed=True,
            completed_at=None,
        ),
        lambda tile: tile.update(completed=False, completed_at=UPDATED_TIME),
    ],
)
def test_load_rejects_invalid_completed_relationship(tmp_path, mutator):
    output_root, sources, _tiles, storage = initialized_project(tmp_path)
    storage.close()
    path = annotation_path(output_root, sources[0].source_id)
    data = read_annotation_json(path)
    mutator(data["tiles"][0])
    rewrite_annotation_json(path, data)

    with pytest.raises(AnnotationStorageError):
        AnnotationStorage.open_or_initialize(output_root, "A01")


def test_atomic_save_failure_keeps_original_json_and_dirty_state(
    tmp_path, monkeypatch
):
    output_root, sources, tiles, storage = initialized_project(tmp_path)
    path = annotation_path(output_root, sources[0].source_id)
    original_bytes = path.read_bytes()
    storage.stage_tile_change(
        tiles[0].tile_id,
        TileAnnotation(status="negative"),
        "status_changed",
    )

    monkeypatch.setattr(
        annotation_storage.os,
        "replace",
        lambda _source, _target: (_ for _ in ()).throw(OSError("disk failure")),
    )
    with pytest.raises(AnnotationStorageError, match="原子写入失败"):
        storage.save_source(sources[0].source_id)

    assert path.read_bytes() == original_bytes
    assert storage.has_dirty_data
    assert not list(path.parent.glob(".annotations.json.*.tmp"))
    storage.close()


def test_saving_one_source_does_not_rewrite_another_source(tmp_path):
    output_root, sources, tiles, storage = initialized_project(
        tmp_path, source_count=2
    )
    second_path = annotation_path(output_root, sources[1].source_id)
    second_before = second_path.read_bytes()
    first_tile = next(
        tile for tile in tiles if tile.source_id == sources[0].source_id
    )

    storage.save_tile_annotation(
        first_tile.tile_id,
        TileAnnotation(status="negative"),
        "status_changed",
    )

    assert second_path.read_bytes() == second_before
    storage.close()


def test_autosave_does_not_modify_stage3_stage4_or_tile_assets(tmp_path):
    output_root, sources, tiles, storage = initialized_project(tmp_path)
    protected_paths = [
        output_root / "manifests" / "sources.csv",
        output_root / "manifests" / "tiles.csv",
        output_root / "sources" / sources[0].source_id / "source.json",
        output_root.joinpath(*tiles[0].tile_relative_path.split("/")),
    ]
    before = {path: path.read_bytes() for path in protected_paths}

    storage.save_tile_annotation(
        tiles[0].tile_id,
        TileAnnotation(status="negative", notes="只允许写 annotation"),
        "status_changed",
    )

    assert {path: path.read_bytes() for path in protected_paths} == before
    log_text = (output_root / "logs" / "operations.log").read_text(
        encoding="utf-8"
    )
    assert "Annotation 初始化成功" in log_text
    assert f"source_id={sources[0].source_id}" in log_text
    assert f"tile_id={tiles[0].tile_id}" in log_text
    assert "change_type=status_changed" in log_text
    assert "只允许写 annotation" not in log_text
    storage.close()


def test_load_rejects_tile_order_different_from_manifest(tmp_path):
    output_root, sources, _tiles, storage = initialized_project(tmp_path)
    storage.close()
    path = annotation_path(output_root, sources[0].source_id)
    data = read_annotation_json(path)
    data["tiles"][0], data["tiles"][1] = data["tiles"][1], data["tiles"][0]
    rewrite_annotation_json(path, data)

    with pytest.raises(AnnotationStorageError, match="顺序"):
        AnnotationStorage.open_or_initialize(output_root, "A01")


@pytest.mark.parametrize(
    "change",
    ["add", "delete", "class", "status", "notes"],
)
def test_annotation_data_changes_invalidate_completed(tmp_path, change):
    _output_root, _sources, tiles, storage = initialized_project(tmp_path)
    tile_id = tiles[0].tile_id
    original = TileAnnotation(
        status="positive",
        objects=[
            BBoxAnnotation("metal", (2, 2, 10, 10)),
            BBoxAnnotation("stone", (20, 20, 30, 30)),
        ],
        notes="original",
        completed=True,
        completed_at=UPDATED_TIME,
    )
    storage.save_tile_annotation(tile_id, original, "setup")
    if change == "add":
        changed = original.with_objects(
            original.objects + [BBoxAnnotation("other", (40, 40, 50, 50))]
        )
    elif change == "delete":
        changed = original.with_objects(original.objects[:1])
    elif change == "class":
        changed = original.with_objects(
            [original.objects[0].with_class("rubber"), original.objects[1]]
        )
    elif change == "status":
        changed = original.with_status("uncertain")
    else:
        changed = original.with_notes("changed")

    storage.stage_tile_change(tile_id, changed, change)
    restored = storage.get_tile_annotation(tile_id)
    assert not restored.completed
    assert restored.completed_at is None
    storage.close()

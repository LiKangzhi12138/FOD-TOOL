import json
from pathlib import Path

import pytest
from PIL import Image

from source_manager import (
    SourceRecord,
    atomic_write_json,
    calculate_sha256,
    generate_source_id,
    write_sources_manifest,
)
from tile_manager import TileRecord, build_tile_id, write_tiles_manifest


TEST_TIMESTAMP = "2026-09-16T06:30:00Z"


def read_json(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as file_handle:
        return json.load(file_handle)


def write_json(path: Path, data: dict[str, object]) -> None:
    atomic_write_json(path, data)


@pytest.fixture
def worker_factory():
    def create(
        base: Path,
        *,
        annotator: str = "A01",
        scene: str = "S01",
        color: tuple[int, int, int] = (20, 40, 60),
        status: str | None = "positive",
        completed: bool = True,
        with_bbox: bool = True,
    ) -> dict[str, object]:
        input_dir = base / "input"
        output_root = base / "worker_output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_root.mkdir(parents=True, exist_ok=True)
        original_path = input_dir / "source.jpg"
        Image.new("RGB", (64, 64), color).save(
            original_path, format="JPEG", quality=95, subsampling=0
        )
        sha256_hex = calculate_sha256(original_path)
        source_id = generate_source_id(scene, sha256_hex)
        source = SourceRecord(
            schema_version="1.0",
            source_id=source_id,
            scene=scene,
            group_id=source_id,
            original_filename="source.jpg",
            original_relative_path="source.jpg",
            original_sha256=sha256_hex,
            original_width=64,
            original_height=64,
            tile_size=64,
            overlap=0,
            stride=64,
            annotator_id=annotator,
            created_at=TEST_TIMESTAMP,
        )
        source_dir = output_root / "sources" / source_id
        atomic_write_json(source_dir / "source.json", source.to_dict())
        write_sources_manifest(
            output_root / "manifests" / "sources.csv", [source]
        )

        tile_id = build_tile_id(source_id, 0, 0)
        relative_path = f"sources/{source_id}/tiles/{tile_id}.jpg"
        tile_path = output_root.joinpath(*relative_path.split("/"))
        tile_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), color).save(
            tile_path, format="JPEG", quality=95, subsampling=0
        )
        tile = TileRecord(
            tile_id=tile_id,
            source_id=source_id,
            scene=scene,
            x=0,
            y=0,
            width=64,
            height=64,
            tile_relative_path=relative_path,
            annotator=annotator,
        )
        write_tiles_manifest(
            output_root / "manifests" / "tiles.csv", [tile]
        )

        objects = (
            [{"class_name": "metal", "bbox": [1, 2, 10, 12]}]
            if with_bbox
            else []
        )
        trainable = status in {"positive", "negative"}
        annotation = {
            "schema_version": "1.0",
            "source_id": source_id,
            "scene": scene,
            "annotator_id": annotator,
            "created_by": annotator,
            "created_at": TEST_TIMESTAMP,
            "updated_at": TEST_TIMESTAMP,
            "tiles": [
                {
                    "tile_id": tile_id,
                    "x": 0,
                    "y": 0,
                    "width": 64,
                    "height": 64,
                    "status": status,
                    "trainable": trainable,
                    "objects": objects,
                    "annotator": annotator,
                    "created_by": annotator,
                    "review_status": None,
                    "reviewed_by": None,
                    "notes": "测试备注",
                    "completed": completed,
                    "completed_at": TEST_TIMESTAMP if completed else None,
                    "created_at": TEST_TIMESTAMP,
                    "updated_at": TEST_TIMESTAMP,
                }
            ],
        }
        annotation_path = source_dir / "annotations.json"
        atomic_write_json(annotation_path, annotation)
        return {
            "input_dir": input_dir,
            "output_root": output_root,
            "original_path": original_path,
            "source_id": source_id,
            "tile_id": tile_id,
            "tile_path": tile_path,
            "annotation_path": annotation_path,
        }

    return create

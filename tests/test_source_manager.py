import csv
import json
from pathlib import Path

import pytest

from source_manager import (
    SourceInitializationError,
    SourceRecord,
    calculate_stride,
    generate_source_id,
    initialize_sources,
    relative_posix_path,
    validate_source_records,
)


def make_record(relative_path: str, sha256_hex: str, scene: str = "S01") -> SourceRecord:
    source_id = generate_source_id(scene, sha256_hex)
    return SourceRecord(
        schema_version="1.0",
        source_id=source_id,
        scene=scene,
        group_id=source_id,
        original_filename=Path(relative_path).name,
        original_relative_path=relative_path,
        original_sha256=sha256_hex,
        original_width=8192,
        original_height=5460,
        tile_size=1024,
        overlap=256,
        stride=768,
        annotator_id="A01",
        created_at="2026-09-16T01:30:20Z",
    )


def test_source_id_uses_uppercase_first_16_hash_characters():
    sha256_hex = "a84f39bc21d7f238" + "9a3e" * 12

    assert generate_source_id("S01", sha256_hex) == "S01_A84F39BC21D7F238"


def test_duplicate_full_sha256_is_rejected():
    sha256_hex = "1" * 64
    records = [
        make_record("folder1/DJI_0001.JPG", sha256_hex),
        make_record("folder2/abc.JPG", sha256_hex),
    ]

    with pytest.raises(SourceInitializationError, match="完全相同的重复原图"):
        validate_source_records(records)


def test_source_id_prefix_collision_is_rejected():
    shared_prefix = "abcdef0123456789"
    records = [
        make_record("first.jpg", shared_prefix + "1" * 48),
        make_record("second.jpg", shared_prefix + "2" * 48),
    ]

    with pytest.raises(SourceInitializationError, match="SourceID 前缀碰撞"):
        validate_source_records(records)


def test_relative_metadata_path_uses_forward_slashes(tmp_path):
    input_dir = tmp_path / "input"
    image_path = input_dir / "flight_01" / "images" / "DJI_0001.JPG"

    assert (
        relative_posix_path(image_path, input_dir)
        == "flight_01/images/DJI_0001.JPG"
    )


def test_stride_is_always_derived_from_tile_size_and_overlap():
    assert calculate_stride(1024, 256) == 768

    with pytest.raises(SourceInitializationError, match="Overlap 必须小于"):
        calculate_stride(1024, 1024)


def test_initialize_sources_commits_complete_metadata_from_staging(
    tmp_path, monkeypatch
):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    image_path = input_dir / "flight_01" / "DJI_0001.JPG"
    image_path.parent.mkdir(parents=True)
    output_dir.mkdir()
    image_path.write_bytes(b"small test payload")
    monkeypatch.setattr(
        "source_manager.inspect_image", lambda _path: (8192, 5460)
    )

    result = initialize_sources(
        input_dir=input_dir,
        output_root=output_dir,
        scene="S01",
        annotator_id="A01",
        tile_size=1024,
        overlap=256,
    )

    assert result.source_count == 1
    source_directories = list((output_dir / "sources").iterdir())
    assert len(source_directories) == 1
    metadata = json.loads(
        (source_directories[0] / "source.json").read_text(encoding="utf-8")
    )
    assert list(metadata) == ["schema_version", *SOURCE_METADATA_FIELDS]
    assert metadata["original_relative_path"] == "flight_01/DJI_0001.JPG"
    assert str(input_dir.resolve()) not in json.dumps(metadata)

    with result.manifest_path.open(encoding="utf-8-sig", newline="") as manifest:
        rows = list(csv.DictReader(manifest))
    assert list(rows[0]) == SOURCE_METADATA_FIELDS
    assert rows[0]["source_id"] == metadata["source_id"]
    assert not list(output_dir.glob(".tmp_source_init_*"))


SOURCE_METADATA_FIELDS = [
    "source_id",
    "scene",
    "group_id",
    "original_filename",
    "original_relative_path",
    "original_sha256",
    "original_width",
    "original_height",
    "tile_size",
    "overlap",
    "stride",
    "annotator_id",
    "created_at",
]

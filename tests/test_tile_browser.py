import csv

import pytest
from PIL import Image

from tile_manager import (
    TILE_CSV_FIELDS,
    TileInitializationError,
    TileRecord,
    load_tile_records,
    write_tiles_manifest,
)


SOURCE_ID = "S01_A84F39BC21D7F238"


def make_tile_record(x: int, y: int = 0) -> TileRecord:
    tile_id = f"{SOURCE_ID}_X{x:06d}_Y{y:06d}"
    return TileRecord(
        tile_id=tile_id,
        source_id=SOURCE_ID,
        scene="S01",
        x=x,
        y=y,
        width=64,
        height=64,
        tile_relative_path=f"sources/{SOURCE_ID}/tiles/{tile_id}.jpg",
        annotator="A01",
    )


def write_browser_fixture(output_root, records, create_images=True):
    source_directory = output_root / "sources" / SOURCE_ID
    tiles_directory = source_directory / "tiles"
    tiles_directory.mkdir(parents=True)
    if create_images:
        for record in records:
            Image.new("RGB", (record.width, record.height), (30, 60, 90)).save(
                output_root.joinpath(*record.tile_relative_path.split("/")),
                format="JPEG",
            )
    write_tiles_manifest(output_root / "manifests" / "tiles.csv", records)


def write_raw_manifest(output_root, fieldnames, row):
    manifest_path = output_root / "manifests" / "tiles.csv"
    manifest_path.parent.mkdir(parents=True)
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as manifest:
        writer = csv.DictWriter(manifest, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)


def test_load_tile_records_preserves_manifest_order(tmp_path):
    output_root = tmp_path / "output"
    records = [make_tile_record(768), make_tile_record(0), make_tile_record(1536)]
    write_browser_fixture(output_root, records)

    loaded = load_tile_records(output_root)

    assert [record.tile_id for record in loaded] == [
        record.tile_id for record in records
    ]
    assert loaded[0].status == ""
    assert loaded[0].trainable == ""
    assert loaded[0].review_status == ""


def test_load_tile_records_rejects_duplicate_tile_id(tmp_path):
    output_root = tmp_path / "output"
    record = make_tile_record(0)
    write_browser_fixture(output_root, [record, record])

    with pytest.raises(TileInitializationError, match="重复 TileID"):
        load_tile_records(output_root)


def test_load_tile_records_rejects_missing_required_field(tmp_path):
    output_root = tmp_path / "output"
    record = make_tile_record(0).to_dict()
    fields = [field for field in TILE_CSV_FIELDS if field != "tile_relative_path"]
    record.pop("tile_relative_path")
    write_raw_manifest(output_root, fields, record)

    with pytest.raises(TileInitializationError, match="tile_relative_path"):
        load_tile_records(output_root)


def test_load_tile_records_rejects_missing_tile_image(tmp_path):
    output_root = tmp_path / "output"
    record = make_tile_record(0)
    write_browser_fixture(output_root, [record], create_images=False)

    with pytest.raises(TileInitializationError, match="Tile 图片不存在"):
        load_tile_records(output_root)


def test_load_tile_records_rejects_relative_path_escape(tmp_path):
    output_root = tmp_path / "output"
    record = make_tile_record(0)
    escaped = TileRecord(
        **{
            **record.to_dict(),
            "tile_relative_path": "../../abc.jpg",
        }
    )
    write_browser_fixture(output_root, [escaped], create_images=False)

    with pytest.raises(TileInitializationError, match="不安全"):
        load_tile_records(output_root)


def test_load_tile_records_rejects_non_integer_geometry(tmp_path):
    output_root = tmp_path / "output"
    record = make_tile_record(0).to_dict()
    record["x"] = "abc"
    (output_root / "sources" / SOURCE_ID).mkdir(parents=True)
    write_raw_manifest(output_root, TILE_CSV_FIELDS, record)

    with pytest.raises(TileInitializationError, match="字段 x 不是整数"):
        load_tile_records(output_root)

import csv

import pytest
from PIL import Image

from source_manager import (
    SourceRecord,
    atomic_write_json,
    calculate_sha256,
    generate_source_id,
    write_sources_manifest,
)
from tile_manager import (
    TILE_CSV_FIELDS,
    SourceMetadata,
    TileInitializationError,
    build_tile_id,
    build_tile_plan,
    generate_axis_positions,
    initialize_tiles,
)


EXPECTED_X_POSITIONS = [
    0,
    768,
    1536,
    2304,
    3072,
    3840,
    4608,
    5376,
    6144,
    6912,
    7168,
]
EXPECTED_Y_POSITIONS = [0, 768, 1536, 2304, 3072, 3840, 4436]


def make_source_metadata(
    width: int = 8192,
    height: int = 5460,
    tile_size: int = 1024,
    overlap: int = 256,
) -> SourceMetadata:
    return SourceMetadata(
        source_id="S01_A84F39BC21D7F238",
        scene="S01",
        group_id="S01_A84F39BC21D7F238",
        original_filename="DJI_0001.JPG",
        original_relative_path="DJI_0001.JPG",
        original_sha256="a84f39bc21d7f238" + "0" * 48,
        original_width=width,
        original_height=height,
        tile_size=tile_size,
        overlap=overlap,
        stride=tile_size - overlap,
        annotator_id="A01",
        created_at="2026-09-16T01:30:20Z",
    )


def test_default_width_positions():
    assert generate_axis_positions(8192, 1024, 768) == EXPECTED_X_POSITIONS


def test_default_height_positions():
    assert generate_axis_positions(5460, 1024, 768) == EXPECTED_Y_POSITIONS


def test_default_source_has_77_tiles():
    assert len(build_tile_plan(make_source_metadata())) == 77


def test_every_default_tile_stays_in_bounds_and_edges_are_covered():
    records = build_tile_plan(make_source_metadata())

    assert all(
        record.x >= 0
        and record.y >= 0
        and record.x + 1024 <= 8192
        and record.y + 1024 <= 5460
        for record in records
    )
    assert max(record.x for record in records) + 1024 == 8192
    assert max(record.y for record in records) + 1024 == 5460


def test_every_planned_tile_has_the_fixed_tile_size():
    records = build_tile_plan(make_source_metadata())

    assert all(record.width == 1024 and record.height == 1024 for record in records)


def test_default_plan_has_no_duplicate_coordinates():
    records = build_tile_plan(make_source_metadata())
    coordinates = [(record.x, record.y) for record in records]

    assert len(coordinates) == len(set(coordinates)) == 77


def test_tile_id_uses_six_digit_decimal_coordinates():
    assert (
        build_tile_id("S01_A84F39BC21D7F238", 768, 1536)
        == "S01_A84F39BC21D7F238_X000768_Y001536"
    )


def test_default_plan_has_77_unique_tile_ids():
    tile_ids = [record.tile_id for record in build_tile_plan(make_source_metadata())]

    assert len(tile_ids) == len(set(tile_ids)) == 77


def test_aligned_edge_position_is_not_appended_twice():
    assert generate_axis_positions(2560, 1024, 768) == [0, 768, 1536]


@pytest.mark.parametrize(
    ("length", "tile_size", "stride"),
    [(0, 512, 384), (1800, 0, 384), (1800, 512, 0), (500, 512, 384)],
)
def test_invalid_axis_parameters_are_rejected(length, tile_size, stride):
    with pytest.raises(TileInitializationError):
        generate_axis_positions(length, tile_size, stride)


def test_tile_plan_is_row_major_y_outer_x_inner():
    records = build_tile_plan(
        make_source_metadata(width=1400, height=1100, tile_size=512, overlap=128)
    )
    x_positions = generate_axis_positions(1400, 512, 384)

    assert [(record.x, record.y) for record in records[: len(x_positions)]] == [
        (x, 0) for x in x_positions
    ]
    assert records[len(x_positions)].y == 384
    assert records[len(x_positions)].x == 0


def create_small_stage3_fixture(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    original_path = input_dir / "flight_01" / "source.JPG"
    original_path.parent.mkdir(parents=True)
    output_dir.mkdir()
    Image.new("RGB", (1800, 1300), (120, 80, 40)).save(
        original_path, format="JPEG", quality=98, subsampling=0
    )

    original_sha256 = calculate_sha256(original_path)
    source_id = generate_source_id("S01", original_sha256)
    source_record = SourceRecord(
        schema_version="1.0",
        source_id=source_id,
        scene="S01",
        group_id=source_id,
        original_filename=original_path.name,
        original_relative_path="flight_01/source.JPG",
        original_sha256=original_sha256,
        original_width=1800,
        original_height=1300,
        tile_size=512,
        overlap=128,
        stride=384,
        annotator_id="A01",
        created_at="2026-09-16T01:30:20Z",
    )
    atomic_write_json(
        output_dir / "sources" / source_id / "source.json",
        source_record.to_dict(),
    )
    write_sources_manifest(
        output_dir / "manifests" / "sources.csv", [source_record]
    )
    return input_dir, output_dir, original_path, source_id


def test_small_image_stage4_integration_uses_production_validation(tmp_path):
    input_dir, output_dir, _original_path, source_id = create_small_stage3_fixture(
        tmp_path
    )

    result = initialize_tiles(input_dir, output_dir)

    assert result.source_count == 1
    assert result.tile_count == 20
    tiles_directory = output_dir / "sources" / source_id / "tiles"
    tile_paths = sorted(tiles_directory.glob("*.jpg"))
    assert len(tile_paths) == 20
    assert tile_paths[0].name.startswith(f"{source_id}_X")
    for tile_path in tile_paths:
        with Image.open(tile_path) as tile:
            assert tile.mode == "RGB"
            assert tile.size == (512, 512)

    with result.manifest_path.open("r", encoding="utf-8-sig", newline="") as manifest:
        rows = list(csv.DictReader(manifest))
    assert list(rows[0]) == TILE_CSV_FIELDS
    assert len(rows) == 20
    assert rows[0]["status"] == ""
    assert rows[0]["trainable"] == ""
    assert rows[0]["object_count"] == "0"
    assert rows[0]["review_status"] == ""
    assert not list(output_dir.glob(".tmp_tile_init_*"))

    with pytest.raises(TileInitializationError, match="已经存在 Tile"):
        initialize_tiles(input_dir, output_dir)


def test_changed_original_sha256_stops_before_creating_tile_data(tmp_path):
    input_dir, output_dir, original_path, source_id = create_small_stage3_fixture(
        tmp_path
    )
    Image.new("RGB", (1800, 1300), (10, 20, 30)).save(
        original_path, format="JPEG", quality=98, subsampling=0
    )

    with pytest.raises(TileInitializationError, match="内容已发生变化"):
        initialize_tiles(input_dir, output_dir)

    assert not (output_dir / "manifests" / "tiles.csv").exists()
    assert not (output_dir / "sources" / source_id / "tiles").exists()
    assert not list(output_dir.glob(".tmp_tile_init_*"))

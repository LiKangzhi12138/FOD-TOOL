"""Deterministic tile planning and image generation for Stage 4."""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from PIL import Image, UnidentifiedImageError

from source_manager import (
    SourceInitializationError,
    calculate_sha256,
    validate_path_relationship,
)


SOURCE_SCHEMA_VERSION = "1.0"
SOURCE_ID_PATTERN = re.compile(r"^(S01|S02|S03)_[0-9A-F]{16}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SOURCE_CSV_FIELDS = [
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
TILE_CSV_FIELDS = [
    "tile_id",
    "source_id",
    "scene",
    "x",
    "y",
    "width",
    "height",
    "tile_relative_path",
    "status",
    "trainable",
    "object_count",
    "annotator",
    "review_status",
]
JPEG_QUALITY = 95
JPEG_SUBSAMPLING = 0

TileProgressCallback = Callable[[int, int, int, int, int, int, str], None]


class TileInitializationError(Exception):
    """A user-actionable failure during Tile initialization."""

    def __init__(self, message: str, issues: Iterable[str] | None = None):
        super().__init__(message)
        self.message = message
        self.issues = list(issues or [])

    def __str__(self) -> str:
        if not self.issues:
            return self.message
        details = "\n\n".join(
            f"{index}. {issue}" for index, issue in enumerate(self.issues, start=1)
        )
        return f"{self.message}，共发现 {len(self.issues)} 个问题：\n\n{details}"

    def format_for_gui(self, limit: int = 10) -> str:
        if not self.issues:
            return self.message
        visible = self.issues[:limit]
        details = "\n\n".join(
            f"{index}. {issue}" for index, issue in enumerate(visible, start=1)
        )
        suffix = ""
        if len(self.issues) > limit:
            suffix = f"\n\n仅显示前 {limit} 个问题；完整信息已写入操作日志。"
        return (
            f"{self.message}，共发现 {len(self.issues)} 个问题：\n\n"
            f"{details}{suffix}"
        )


@dataclass(frozen=True)
class SourceMetadata:
    source_id: str
    scene: str
    group_id: str
    original_filename: str
    original_relative_path: str
    original_sha256: str
    original_width: int
    original_height: int
    tile_size: int
    overlap: int
    stride: int
    annotator_id: str
    created_at: str


@dataclass(frozen=True)
class ValidatedSource:
    metadata: SourceMetadata
    original_path: Path


@dataclass(frozen=True)
class TileRecord:
    tile_id: str
    source_id: str
    scene: str
    x: int
    y: int
    width: int
    height: int
    tile_relative_path: str
    status: str = ""
    trainable: str = ""
    object_count: int = 0
    annotator: str = ""
    review_status: str = ""

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True)
class TileInitializationResult:
    source_count: int
    tile_count: int
    tile_size: int
    overlap: int
    stride: int
    manifest_path: Path
    log_warning: str | None = None


def _parse_tile_integer(row: dict[str, str], key: str, row_number: int) -> int:
    value = row.get(key)
    try:
        return int(value) if value is not None else int("")
    except ValueError as exc:
        raise TileInitializationError(
            f"tiles.csv 第 {row_number} 行字段 {key} 不是整数：{value!r}"
        ) from exc


def resolve_tile_image_path(
    output_root: str | Path, tile_relative_path: str
) -> Path:
    """Resolve a POSIX Tile path while preventing output-root escape."""
    root = Path(output_root).resolve()
    relative_path = PurePosixPath(tile_relative_path)
    if (
        not tile_relative_path
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or "\\" in tile_relative_path
        or (relative_path.parts and relative_path.parts[0].endswith(":"))
    ):
        raise TileInitializationError(
            f"tile_relative_path 不安全：{tile_relative_path!r}"
        )
    candidate = root.joinpath(*relative_path.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise TileInitializationError(
            f"Tile 路径逃逸成果输出目录：{tile_relative_path}"
        ) from exc
    return candidate


def load_tile_records(output_root: str | Path) -> list[TileRecord]:
    """Read and validate Stage 4 tiles.csv without modifying project data."""
    root = Path(output_root).resolve()
    manifest_path = root / "manifests" / "tiles.csv"
    if not manifest_path.is_file():
        raise TileInitializationError(f"Tile Manifest 不存在：{manifest_path}")

    try:
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as file_handle:
            reader = csv.DictReader(file_handle)
            if reader.fieldnames is None:
                raise TileInitializationError("tiles.csv 缺少表头。")
            missing_fields = [
                field for field in TILE_CSV_FIELDS if field not in reader.fieldnames
            ]
            if missing_fields:
                raise TileInitializationError(
                    "tiles.csv 缺少必要字段：" + ", ".join(missing_fields)
                )
            rows = list(reader)
    except TileInitializationError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise TileInitializationError(
            f"tiles.csv 无法读取或格式损坏：{manifest_path}\n{exc}"
        ) from exc

    if not rows:
        raise TileInitializationError("tiles.csv 中没有 Tile 记录。")

    records: list[TileRecord] = []
    issues: list[str] = []
    seen_tile_ids: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        try:
            missing_values = [
                field for field in TILE_CSV_FIELDS if row.get(field) is None
            ]
            if missing_values:
                raise TileInitializationError(
                    f"tiles.csv 第 {row_number} 行缺少字段值："
                    + ", ".join(missing_values)
                )
            tile_id = row.get("tile_id", "")
            source_id = row.get("source_id", "")
            if not tile_id:
                raise TileInitializationError(
                    f"tiles.csv 第 {row_number} 行 tile_id 为空。"
                )
            if not source_id:
                raise TileInitializationError(
                    f"tiles.csv 第 {row_number} 行 source_id 为空。"
                )
            if tile_id in seen_tile_ids:
                raise TileInitializationError(f"检测到重复 TileID：{tile_id}")
            seen_tile_ids.add(tile_id)

            x = _parse_tile_integer(row, "x", row_number)
            y = _parse_tile_integer(row, "y", row_number)
            width = _parse_tile_integer(row, "width", row_number)
            height = _parse_tile_integer(row, "height", row_number)
            object_count = _parse_tile_integer(row, "object_count", row_number)
            if x < 0 or y < 0:
                raise TileInitializationError(
                    f"tiles.csv 第 {row_number} 行 x / y 不能小于 0。"
                )
            if width <= 0 or height <= 0:
                raise TileInitializationError(
                    f"tiles.csv 第 {row_number} 行 width / height 必须大于 0。"
                )
            if object_count < 0:
                raise TileInitializationError(
                    f"tiles.csv 第 {row_number} 行 object_count 不能小于 0。"
                )
            expected_tile_id = build_tile_id(source_id, x, y)
            if tile_id != expected_tile_id:
                raise TileInitializationError(
                    f"tiles.csv 第 {row_number} 行 TileID 与 SourceID、坐标不一致："
                    f"{tile_id}"
                )

            scene = row.get("scene", "")
            if scene not in {"S01", "S02", "S03"}:
                raise TileInitializationError(
                    f"tiles.csv 第 {row_number} 行 scene 不合法：{scene!r}"
                )
            if not source_id.startswith(f"{scene}_"):
                raise TileInitializationError(
                    f"tiles.csv 第 {row_number} 行 source_id 与 scene 不一致。"
                )

            relative_path = row.get("tile_relative_path", "")
            if not relative_path:
                raise TileInitializationError(
                    f"tiles.csv 第 {row_number} 行 tile_relative_path 为空。"
                )
            tile_path = resolve_tile_image_path(root, relative_path)
            source_directory = (root / "sources" / source_id).resolve()
            if not source_directory.is_dir():
                raise TileInitializationError(
                    f"Tile 对应的 Source 目录不存在：{source_directory}"
                )
            try:
                tile_path.relative_to(source_directory)
            except ValueError as exc:
                raise TileInitializationError(
                    f"Tile 路径不属于对应 Source：{relative_path}"
                ) from exc
            if not tile_path.is_file():
                raise TileInitializationError(f"Tile 图片不存在：{tile_path}")

            records.append(
                TileRecord(
                    tile_id=tile_id,
                    source_id=source_id,
                    scene=scene,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    tile_relative_path=relative_path,
                    status=row.get("status", "") or "",
                    trainable=row.get("trainable", "") or "",
                    object_count=object_count,
                    annotator=row.get("annotator", "") or "",
                    review_status=row.get("review_status", "") or "",
                )
            )
        except TileInitializationError as exc:
            issues.append(str(exc))

    if issues:
        raise TileInitializationError("Tile Manifest 校验失败", issues)
    return records


def generate_axis_positions(length: int, tile_size: int, stride: int) -> list[int]:
    """Return ascending full-tile starts, adding one edge-aligned final start."""
    if length <= 0:
        raise TileInitializationError("轴长度 length 必须大于 0。")
    if tile_size <= 0:
        raise TileInitializationError("Tile Size 必须大于 0。")
    if stride <= 0:
        raise TileInitializationError("Stride 必须大于 0。")
    if tile_size > length:
        raise TileInitializationError(
            f"Tile Size {tile_size} 大于轴长度 {length}，无法生成固定尺寸切片。"
        )

    max_start = length - tile_size
    positions = list(range(0, max_start + 1, stride))
    if positions[-1] != max_start:
        positions.append(max_start)
    return positions


def build_tile_id(source_id: str, x: int, y: int) -> str:
    """Build a TileID solely from SourceID and decimal top-left coordinates."""
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise TileInitializationError(f"SourceID 格式不合法：{source_id}")
    if x < 0 or y < 0:
        raise TileInitializationError("Tile 坐标不能小于 0。")
    if x > 999999 or y > 999999:
        raise TileInitializationError("Tile 坐标超过 6 位十进制可表示范围。")
    return f"{source_id}_X{x:06d}_Y{y:06d}"


def build_tile_plan(source: SourceMetadata) -> list[TileRecord]:
    """Build one Source's stable row-major Tile plan without image I/O."""
    if source.stride != source.tile_size - source.overlap:
        raise TileInitializationError(
            f"{source.source_id}：stride 必须等于 tile_size - overlap。"
        )
    x_positions = generate_axis_positions(
        source.original_width, source.tile_size, source.stride
    )
    y_positions = generate_axis_positions(
        source.original_height, source.tile_size, source.stride
    )
    records: list[TileRecord] = []
    for y in y_positions:
        for x in x_positions:
            tile_id = build_tile_id(source.source_id, x, y)
            records.append(
                TileRecord(
                    tile_id=tile_id,
                    source_id=source.source_id,
                    scene=source.scene,
                    x=x,
                    y=y,
                    width=source.tile_size,
                    height=source.tile_size,
                    tile_relative_path=(
                        f"sources/{source.source_id}/tiles/{tile_id}.jpg"
                    ),
                    annotator=source.annotator_id,
                )
            )
    return records


def validate_tile_id_uniqueness(records: Iterable[TileRecord]) -> None:
    """Reject every duplicate TileID instead of silently renaming it."""
    seen: dict[str, TileRecord] = {}
    duplicates: list[str] = []
    for record in records:
        previous = seen.get(record.tile_id)
        if previous is not None:
            duplicates.append(
                f"{record.tile_id}\n"
                f"首次坐标：({previous.x}, {previous.y})；"
                f"重复坐标：({record.x}, {record.y})"
            )
        else:
            seen[record.tile_id] = record
    if duplicates:
        raise TileInitializationError("检测到重复 TileID", duplicates)


def _require_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise TileInitializationError(f"字段 {key} 缺失或不是非空字符串。")
    return value


def _require_integer(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TileInitializationError(f"字段 {key} 缺失或不是整数。")
    return value


def _parse_csv_integer(row: dict[str, str], key: str) -> int:
    value = row.get(key)
    try:
        return int(value) if value is not None else int("")
    except ValueError as exc:
        raise TileInitializationError(f"sources.csv 字段 {key} 不是整数：{value!r}") from exc


def _load_source_metadata(source_json_path: Path) -> SourceMetadata:
    try:
        with source_json_path.open("r", encoding="utf-8") as file_handle:
            data = json.load(file_handle)
    except FileNotFoundError as exc:
        raise TileInitializationError(f"source.json 不存在：{source_json_path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TileInitializationError(
            f"source.json 无法读取或格式损坏：{source_json_path}\n{exc}"
        ) from exc
    if not isinstance(data, dict):
        raise TileInitializationError(f"source.json 顶层必须是对象：{source_json_path}")
    if data.get("schema_version") != SOURCE_SCHEMA_VERSION:
        raise TileInitializationError(
            f"source.json schema_version 必须为 {SOURCE_SCHEMA_VERSION}："
            f"{source_json_path}"
        )

    metadata = SourceMetadata(
        source_id=_require_string(data, "source_id"),
        scene=_require_string(data, "scene"),
        group_id=_require_string(data, "group_id"),
        original_filename=_require_string(data, "original_filename"),
        original_relative_path=_require_string(data, "original_relative_path"),
        original_sha256=_require_string(data, "original_sha256"),
        original_width=_require_integer(data, "original_width"),
        original_height=_require_integer(data, "original_height"),
        tile_size=_require_integer(data, "tile_size"),
        overlap=_require_integer(data, "overlap"),
        stride=_require_integer(data, "stride"),
        annotator_id=_require_string(data, "annotator_id"),
        created_at=_require_string(data, "created_at"),
    )
    _validate_source_metadata(metadata, source_json_path)
    return metadata


def _validate_source_metadata(metadata: SourceMetadata, source_json_path: Path) -> None:
    source_match = SOURCE_ID_PATTERN.fullmatch(metadata.source_id)
    if source_match is None:
        raise TileInitializationError(f"SourceID 格式不合法：{metadata.source_id}")
    if source_json_path.parent.name != metadata.source_id:
        raise TileInitializationError(
            "SourceID 与 source.json 所在目录名不一致："
            f"{metadata.source_id} != {source_json_path.parent.name}"
        )
    if metadata.scene not in {"S01", "S02", "S03"}:
        raise TileInitializationError(f"scene 不合法：{metadata.scene}")
    if source_match.group(1) != metadata.scene:
        raise TileInitializationError(
            f"SourceID 与 scene 不一致：{metadata.source_id} / {metadata.scene}"
        )
    if not SHA256_PATTERN.fullmatch(metadata.original_sha256):
        raise TileInitializationError("original_sha256 必须是 64 位小写十六进制字符串。")
    expected_source_id = (
        f"{metadata.scene}_{metadata.original_sha256[:16].upper()}"
    )
    if metadata.source_id != expected_source_id:
        raise TileInitializationError(
            f"SourceID 与完整 SHA256 不一致：{metadata.source_id}"
        )
    if metadata.original_width <= 0 or metadata.original_height <= 0:
        raise TileInitializationError("原图 width / height 必须大于 0。")
    if metadata.tile_size <= 0:
        raise TileInitializationError("tile_size 必须大于 0。")
    if metadata.overlap < 0 or metadata.overlap >= metadata.tile_size:
        raise TileInitializationError("overlap 必须满足 0 <= overlap < tile_size。")
    if metadata.stride != metadata.tile_size - metadata.overlap:
        raise TileInitializationError(
            "stride 与 tile_size - overlap 不一致："
            f"{metadata.stride} != {metadata.tile_size} - {metadata.overlap}"
        )
    if (
        metadata.tile_size > metadata.original_width
        or metadata.tile_size > metadata.original_height
    ):
        raise TileInitializationError(
            "tile_size 大于原图宽度或高度，无法生成固定正方形 Tile。"
        )

    relative_path = PurePosixPath(metadata.original_relative_path)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or "\\" in metadata.original_relative_path
        or (relative_path.parts and relative_path.parts[0].endswith(":"))
    ):
        raise TileInitializationError(
            f"original_relative_path 不安全：{metadata.original_relative_path}"
        )
    if relative_path.name != metadata.original_filename:
        raise TileInitializationError(
            "original_filename 与 original_relative_path 的文件名不一致。"
        )


def _validate_csv_consistency(
    row: dict[str, str], metadata: SourceMetadata, row_number: int
) -> None:
    expected = {
        "source_id": metadata.source_id,
        "scene": metadata.scene,
        "group_id": metadata.group_id,
        "original_filename": metadata.original_filename,
        "original_relative_path": metadata.original_relative_path,
        "original_sha256": metadata.original_sha256,
        "original_width": metadata.original_width,
        "original_height": metadata.original_height,
        "tile_size": metadata.tile_size,
        "overlap": metadata.overlap,
        "stride": metadata.stride,
        "annotator_id": metadata.annotator_id,
        "created_at": metadata.created_at,
    }
    mismatches: list[str] = []
    for key, expected_value in expected.items():
        if isinstance(expected_value, int):
            actual_value: object = _parse_csv_integer(row, key)
        else:
            actual_value = row.get(key)
        if actual_value != expected_value:
            mismatches.append(f"{key}: CSV={actual_value!r}, JSON={expected_value!r}")
    if mismatches:
        raise TileInitializationError(
            f"sources.csv 第 {row_number} 行与 source.json 不一致：\n"
            + "\n".join(mismatches)
        )


def validate_stage3_sources(output_root: str | Path) -> list[SourceMetadata]:
    """Load and cross-check Stage 3's manifest and every source.json."""
    root = Path(output_root).resolve()
    manifest_path = root / "manifests" / "sources.csv"
    if not manifest_path.is_file():
        raise TileInitializationError(f"Stage 3 Source Manifest 不存在：{manifest_path}")

    try:
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as file_handle:
            reader = csv.DictReader(file_handle)
            if reader.fieldnames is None:
                raise TileInitializationError("sources.csv 缺少表头。")
            missing_fields = [
                field for field in SOURCE_CSV_FIELDS if field not in reader.fieldnames
            ]
            if missing_fields:
                raise TileInitializationError(
                    "sources.csv 缺少必要字段：" + ", ".join(missing_fields)
                )
            rows = list(reader)
    except TileInitializationError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise TileInitializationError(f"sources.csv 无法读取：{manifest_path}\n{exc}") from exc

    if not rows:
        raise TileInitializationError("sources.csv 中没有 Source 记录。")

    sources_root = root / "sources"
    if not sources_root.is_dir():
        raise TileInitializationError(f"Source 根目录不存在：{sources_root}")

    metadata_records: list[SourceMetadata] = []
    issues: list[str] = []
    manifest_source_ids: list[str] = []
    for row_number, row in enumerate(rows, start=2):
        source_id = row.get("source_id", "")
        manifest_source_ids.append(source_id)
        if not SOURCE_ID_PATTERN.fullmatch(source_id):
            issues.append(f"sources.csv 第 {row_number} 行 SourceID 不合法：{source_id!r}")
            continue
        source_json_path = sources_root / source_id / "source.json"
        try:
            metadata = _load_source_metadata(source_json_path)
            _validate_csv_consistency(row, metadata, row_number)
            metadata_records.append(metadata)
        except TileInitializationError as exc:
            issues.append(str(exc))

    duplicate_source_ids = sorted(
        source_id
        for source_id in set(manifest_source_ids)
        if manifest_source_ids.count(source_id) > 1
    )
    if duplicate_source_ids:
        issues.append(
            "sources.csv 包含重复 SourceID：" + ", ".join(duplicate_source_ids)
        )

    directory_names = {
        child.name for child in sources_root.iterdir() if child.is_dir()
    }
    manifest_names = set(manifest_source_ids)
    missing_directories = sorted(manifest_names - directory_names)
    extra_directories = sorted(directory_names - manifest_names)
    if missing_directories:
        issues.append("缺少 Source 目录：" + ", ".join(missing_directories))
    if extra_directories:
        issues.append("发现未登记在 sources.csv 的 Source 目录：" + ", ".join(extra_directories))

    if issues:
        raise TileInitializationError("Stage 3 Source 数据校验失败", issues)

    metadata_records.sort(key=lambda source: source.source_id)
    first = metadata_records[0]
    for metadata in metadata_records[1:]:
        if (
            metadata.tile_size,
            metadata.overlap,
            metadata.stride,
        ) != (first.tile_size, first.overlap, first.stride):
            raise TileInitializationError(
                "不同 Source 的 tile_size / overlap / stride 不一致，拒绝切片。"
            )
    return metadata_records


def _resolve_original_path(input_dir: Path, relative_path: str) -> Path:
    posix_path = PurePosixPath(relative_path)
    candidate = input_dir.joinpath(*posix_path.parts).resolve()
    try:
        candidate.relative_to(input_dir)
    except ValueError as exc:
        raise TileInitializationError(
            f"原图路径逃逸 input_dir，拒绝访问：{relative_path}"
        ) from exc
    return candidate


def validate_original_source(input_dir: Path, source: SourceMetadata) -> ValidatedSource:
    """Recheck path containment, integrity, dimensions, and full SHA256."""
    original_path = _resolve_original_path(input_dir, source.original_relative_path)
    if not original_path.is_file():
        raise TileInitializationError(
            f"原始图片不存在：{source.original_relative_path}\n{original_path}"
        )
    try:
        with Image.open(original_path) as image:
            actual_width, actual_height = image.size
            image.verify()
    except UnidentifiedImageError as exc:
        raise TileInitializationError(
            f"Pillow 无法识别原始图片：{source.original_relative_path}"
        ) from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise TileInitializationError(
            f"原始图片损坏或无法读取：{source.original_relative_path}\n{exc}"
        ) from exc
    if (actual_width, actual_height) != (
        source.original_width,
        source.original_height,
    ):
        raise TileInitializationError(
            f"原始图片尺寸已发生变化：{source.original_relative_path}\n"
            f"metadata：{source.original_width} × {source.original_height}\n"
            f"实际：{actual_width} × {actual_height}"
        )
    try:
        actual_sha256 = calculate_sha256(original_path)
    except OSError as exc:
        raise TileInitializationError(
            f"原始图片 SHA256 计算失败：{source.original_relative_path}\n{exc}"
        ) from exc
    if actual_sha256 != source.original_sha256:
        raise TileInitializationError(
            f"原始图片内容已发生变化，不能继续生成 Tile：\n"
            f"{source.original_relative_path}\n"
            f"metadata SHA256：{source.original_sha256}\n"
            f"实际 SHA256：{actual_sha256}"
        )
    return ValidatedSource(metadata=source, original_path=original_path)


def validate_original_sources(
    input_dir: Path, sources: Iterable[SourceMetadata]
) -> list[ValidatedSource]:
    """Recheck all originals and report all Source problems together."""
    validated: list[ValidatedSource] = []
    issues: list[str] = []
    for source in sources:
        try:
            validated.append(validate_original_source(input_dir, source))
        except TileInitializationError as exc:
            issues.append(f"{source.source_id}\n{exc}")
    if issues:
        raise TileInitializationError("原始图片重新校验失败", issues)
    return validated


def crop_source_tiles(
    source: ValidatedSource,
    records: list[TileRecord],
    destination_dir: str | Path,
    source_index: int = 1,
    source_total: int = 1,
    overall_completed: int = 0,
    overall_total: int | None = None,
    progress_callback: TileProgressCallback | None = None,
) -> int:
    """Crop and save one Source's planned fixed-size RGB JPEG tiles."""
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=False)
    tile_size = source.metadata.tile_size
    total_tiles = len(records)
    if overall_total is None:
        overall_total = overall_completed + total_tiles

    try:
        with Image.open(source.original_path) as image:
            if image.size != (
                source.metadata.original_width,
                source.metadata.original_height,
            ):
                raise TileInitializationError(
                    f"切片前原图尺寸再次检查失败：{source.original_path}"
                )
            for tile_index, record in enumerate(records, start=1):
                crop_box = (
                    record.x,
                    record.y,
                    record.x + tile_size,
                    record.y + tile_size,
                )
                tile = image.crop(crop_box)
                try:
                    if tile.size != (tile_size, tile_size):
                        raise TileInitializationError(
                            f"Tile 尺寸错误：{record.tile_id}，实际 {tile.size}"
                        )
                    if tile.mode != "RGB":
                        rgb_tile = tile.convert("RGB")
                        tile.close()
                        tile = rgb_tile
                    tile.save(
                        destination / f"{record.tile_id}.jpg",
                        format="JPEG",
                        quality=JPEG_QUALITY,
                        subsampling=JPEG_SUBSAMPLING,
                    )
                finally:
                    tile.close()
                if progress_callback is not None:
                    progress_callback(
                        source_index,
                        source_total,
                        tile_index,
                        total_tiles,
                        overall_completed + tile_index,
                        overall_total,
                        source.metadata.source_id,
                    )
    except TileInitializationError:
        raise
    except (OSError, ValueError, SyntaxError) as exc:
        raise TileInitializationError(
            f"生成 Tile 失败：{source.metadata.source_id}\n{exc}"
        ) from exc

    generated_files = list(destination.glob("*.jpg"))
    if len(generated_files) != total_tiles:
        raise TileInitializationError(
            f"{source.metadata.source_id} 的 Tile 数量错误："
            f"期望 {total_tiles}，实际 {len(generated_files)}"
        )
    return total_tiles


def write_tiles_manifest(target: str | Path, records: Iterable[TileRecord]) -> None:
    """Atomically write the fixed-schema UTF-8-SIG tiles.csv."""
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            dir=target_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            writer = csv.DictWriter(temporary_file, fieldnames=TILE_CSV_FIELDS)
            writer.writeheader()
            for record in records:
                writer.writerow(record.to_dict())
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target_path)
    except Exception as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise TileInitializationError(f"tiles.csv 写入失败：{target_path}\n{exc}") from exc


def _ensure_no_existing_tile_data(output_root: Path) -> None:
    manifest_path = output_root / "manifests" / "tiles.csv"
    if manifest_path.exists():
        raise TileInitializationError(
            "当前成果目录已经存在 Tile 数据。\n\n"
            "Stage 4 不会自动覆盖、删除、补齐或重新切片。"
        )
    sources_root = output_root / "sources"
    if sources_root.is_dir():
        existing_tiles = sorted(
            str(child / "tiles")
            for child in sources_root.iterdir()
            if child.is_dir() and (child / "tiles").exists()
        )
        if existing_tiles:
            raise TileInitializationError(
                "当前成果目录已经存在 Tile 目录。\n\n"
                "Stage 4 不会自动覆盖、删除、补齐或重新切片。\n\n"
                + "\n".join(existing_tiles)
            )


def _create_operation_logger(output_root: Path) -> tuple[logging.Logger, logging.FileHandler]:
    log_path = output_root / "logs" / "operations.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"fod_tile_init.{id(output_root)}.{os.getpid()}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.FileHandler(log_path, encoding="utf-8")
        formatter = logging.Formatter("%(asctime)sZ | %(levelname)s | %(message)s")
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        return logger, handler
    except OSError as exc:
        raise TileInitializationError(f"操作日志创建失败：{log_path}\n{exc}") from exc


def _flush_operation_log(handler: logging.FileHandler) -> None:
    try:
        handler.flush()
        if handler.stream is not None:
            os.fsync(handler.stream.fileno())
    except OSError as exc:
        raise TileInitializationError(f"操作日志写入失败：{exc}") from exc


def _remove_tree(path: Path) -> list[str]:
    problems: list[str] = []
    if not path.exists():
        return problems
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink(missing_ok=True)
        except OSError as exc:
            problems.append(f"无法清理 {child}：{exc}")
    try:
        path.rmdir()
    except OSError as exc:
        problems.append(f"无法清理 {path}：{exc}")
    return problems


def _cleanup_staging(staging_root: Path, output_root: Path) -> list[str]:
    try:
        resolved_staging = staging_root.resolve()
        resolved_staging.relative_to(output_root.resolve())
        if not resolved_staging.name.startswith(".tmp_tile_init_"):
            return [f"拒绝清理非 Tile staging 目录：{resolved_staging}"]
    except (OSError, ValueError) as exc:
        return [f"无法确认 Tile staging 清理范围：{exc}"]
    return _remove_tree(staging_root)


def _commit_staging(
    staging_root: Path, output_root: Path, sources: list[ValidatedSource]
) -> None:
    committed_tiles_directories: list[Path] = []
    manifest_path = output_root / "manifests" / "tiles.csv"
    manifest_committed = False
    try:
        for source in sources:
            source_id = source.metadata.source_id
            staged_tiles = staging_root / source_id / "tiles"
            final_tiles = output_root / "sources" / source_id / "tiles"
            if final_tiles.exists():
                raise TileInitializationError(
                    f"提交时发现 Tile 目录已存在，拒绝覆盖：{final_tiles}"
                )
            os.rename(staged_tiles, final_tiles)
            committed_tiles_directories.append(final_tiles)

        if manifest_path.exists():
            raise TileInitializationError(
                f"提交时发现 tiles.csv 已存在，拒绝覆盖：{manifest_path}"
            )
        os.rename(staging_root / "manifests" / "tiles.csv", manifest_path)
        manifest_committed = True
    except Exception as exc:
        rollback_problems: list[str] = []
        if manifest_committed:
            try:
                manifest_path.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_problems.append(f"无法撤回 {manifest_path}：{rollback_exc}")
        for tiles_directory in reversed(committed_tiles_directories):
            rollback_problems.extend(_remove_tree(tiles_directory))
        message = str(exc) if isinstance(exc, TileInitializationError) else (
            f"Tile 数据提交失败：{exc}"
        )
        if rollback_problems:
            message += "\n\n回滚未完全成功：\n" + "\n".join(rollback_problems)
        raise TileInitializationError(message) from exc


def initialize_tiles(
    input_dir: str | Path,
    output_root: str | Path,
    progress_callback: TileProgressCallback | None = None,
) -> TileInitializationResult:
    """Validate Stage 3 and originals, generate all tiles in staging, then commit."""
    try:
        resolved_input, resolved_output = validate_path_relationship(
            input_dir, output_root
        )
    except SourceInitializationError as exc:
        raise TileInitializationError(str(exc)) from exc
    if not resolved_input.is_dir():
        raise TileInitializationError(f"原图输入目录不存在或不是文件夹：{resolved_input}")
    if not resolved_output.is_dir():
        raise TileInitializationError(f"成果输出目录不存在或不是文件夹：{resolved_output}")

    logger, log_handler = _create_operation_logger(resolved_output)
    staging_root: Path | None = None
    try:
        logger.info("Tile 初始化开始")
        logger.info("input_dir=%s", resolved_input)
        logger.info("output_dir=%s", resolved_output)

        sources = validate_stage3_sources(resolved_output)
        _ensure_no_existing_tile_data(resolved_output)
        logger.info("Source 数量=%s", len(sources))
        logger.info("tile_size=%s", sources[0].tile_size)
        logger.info("overlap=%s", sources[0].overlap)
        logger.info("stride=%s", sources[0].stride)

        validated_sources = validate_original_sources(resolved_input, sources)
        plans: dict[str, list[TileRecord]] = {}
        all_records: list[TileRecord] = []
        for source in sources:
            source_plan = build_tile_plan(source)
            plans[source.source_id] = source_plan
            all_records.extend(source_plan)
            logger.info(
                "SourceID=%s | Tile 数量=%s", source.source_id, len(source_plan)
            )
        validate_tile_id_uniqueness(all_records)
        logger.info("总 Tile 数量=%s", len(all_records))
        _flush_operation_log(log_handler)

        staging_root = Path(
            tempfile.mkdtemp(prefix=".tmp_tile_init_", dir=resolved_output)
        )
        overall_completed = 0
        for source_index, source in enumerate(validated_sources, start=1):
            source_plan = plans[source.metadata.source_id]
            crop_source_tiles(
                source=source,
                records=source_plan,
                destination_dir=(
                    staging_root / source.metadata.source_id / "tiles"
                ),
                source_index=source_index,
                source_total=len(validated_sources),
                overall_completed=overall_completed,
                overall_total=len(all_records),
                progress_callback=progress_callback,
            )
            overall_completed += len(source_plan)

        if overall_completed != len(all_records):
            raise TileInitializationError(
                f"Tile 总数校验失败：计划 {len(all_records)}，实际 {overall_completed}"
            )
        write_tiles_manifest(
            staging_root / "manifests" / "tiles.csv", all_records
        )
        _commit_staging(staging_root, resolved_output, validated_sources)

        log_warning = None
        try:
            logger.info("Tile 初始化成功 | 总 Tile 数量=%s", len(all_records))
            _flush_operation_log(log_handler)
        except TileInitializationError as exc:
            log_warning = str(exc)
        first = sources[0]
        return TileInitializationResult(
            source_count=len(sources),
            tile_count=len(all_records),
            tile_size=first.tile_size,
            overlap=first.overlap,
            stride=first.stride,
            manifest_path=resolved_output / "manifests" / "tiles.csv",
            log_warning=log_warning,
        )
    except TileInitializationError as exc:
        logger.error("Tile 初始化失败：%s", exc)
        try:
            _flush_operation_log(log_handler)
        except TileInitializationError:
            pass
        raise
    except Exception as exc:
        logger.exception("Tile 初始化失败（意外错误）：%s", exc)
        try:
            _flush_operation_log(log_handler)
        except TileInitializationError:
            pass
        raise TileInitializationError(f"Tile 初始化发生意外错误：{exc}") from exc
    finally:
        if staging_root is not None:
            cleanup_problems = _cleanup_staging(staging_root, resolved_output)
            for problem in cleanup_problems:
                logger.error(problem)
        log_handler.close()
        logger.removeHandler(log_handler)

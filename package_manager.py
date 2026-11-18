"""Strict Worker Package finalization, validation, and fingerprinting."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, UnidentifiedImageError

from annotation_model import FOD_CLASSES
from annotation_storage import (
    AnnotationStorage,
    AnnotationStorageError,
    validate_utc_timestamp,
)
from source_manager import (
    SourceInitializationError,
    SourceRecord,
    atomic_write_json,
    calculate_sha256,
    utc_now_iso8601,
    write_sources_manifest,
)
from tile_manager import (
    SourceMetadata,
    TileInitializationError,
    TileRecord,
    load_tile_records,
    validate_original_sources,
    validate_stage3_sources,
    write_tiles_manifest,
)


from protected_negatives import MANIFEST_PATH, load_protected_negatives

TOOL_VERSION = "0.2.0"
PACKAGE_SCHEMA_VERSION = "1.0"
PACKAGE_TYPE = "worker"
PACKAGE_REQUIRED_FIELDS = (
    "schema_version",
    "package_type",
    "tool_version",
    "annotator_id",
    "created_at",
    "finalized_at",
    "scenes",
    "source_count",
    "tile_count",
    "annotation_statistics",
    "content_fingerprint",
)
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")

PackageProgressCallback = Callable[[int, int, str, str], None]


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = "error"
    package: str | None = None
    source_id: str | None = None
    tile_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "package": self.package,
            "source_id": self.source_id,
            "tile_id": self.tile_id,
        }


class PackageValidationError(Exception):
    """A complete, user-facing Worker Package validation failure."""

    def __init__(
        self,
        message: str,
        issues: Iterable[ValidationIssue] | None = None,
        report_path: Path | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.issues = list(issues or [])
        self.report_path = report_path

    @property
    def errors(self) -> list[ValidationIssue]:
        return [item for item in self.issues if item.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [item for item in self.issues if item.severity == "warning"]

    def __str__(self) -> str:
        if not self.issues:
            return self.message
        details = "\n\n".join(
            f"{index}. [{item.code}] {item.message}"
            for index, item in enumerate(self.issues, start=1)
        )
        return f"{self.message}，共发现 {len(self.errors)} 个错误：\n\n{details}"

    def format_for_gui(self, limit: int = 10) -> str:
        visible = self.issues[:limit]
        details = "\n\n".join(
            f"{index}. [{item.code}] {item.message}"
            for index, item in enumerate(visible, start=1)
        )
        suffix = ""
        if len(self.issues) > limit:
            suffix = f"\n\n仅显示前 {limit} 项。"
        report = f"\n\n完整报告：\n{self.report_path}" if self.report_path else ""
        return (
            f"{self.message}\n\nERROR：{len(self.errors)}\n"
            f"WARNING：{len(self.warnings)}\n\n{details}{suffix}{report}"
        )


@dataclass
class WorkerPackageSnapshot:
    root: Path
    annotator_id: str
    sources: list[SourceMetadata]
    tiles: list[TileRecord]
    statistics: dict[str, object]
    scenes: list[str]
    content_fingerprint: str | None = None
    package_data: dict[str, object] | None = None
    warnings: list[ValidationIssue] = field(default_factory=list)
    protected_negatives: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class WorkerPackageResult:
    package_path: Path
    validation_report_path: Path
    annotator_id: str
    scenes: tuple[str, ...]
    source_count: int
    tile_count: int
    statistics: dict[str, object]
    content_fingerprint: str
    log_warning: str | None = None


def _issue(
    code: str,
    message: str,
    *,
    package: Path | None = None,
    source_id: str | None = None,
    tile_id: str | None = None,
    severity: str = "error",
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        message=message,
        severity=severity,
        package=str(package) if package is not None else None,
        source_id=source_id,
        tile_id=tile_id,
    )


def _read_json(path: Path, description: str) -> dict[str, object]:
    schema_code = (
        "PACKAGE_SCHEMA_MISMATCH"
        if "package.json" in description
        else "ANNOTATION_SCHEMA_ERROR"
    )
    try:
        with path.open("r", encoding="utf-8") as file_handle:
            data = json.load(file_handle)
    except FileNotFoundError as exc:
        raise PackageValidationError(
            f"{description} 不存在",
            [_issue("MISSING_FILE", f"{description} 不存在：{path}")],
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageValidationError(
            f"{description} 无法读取",
            [_issue(schema_code, f"{path}\n{exc}")],
        ) from exc
    if not isinstance(data, dict):
        raise PackageValidationError(
            f"{description} 格式错误",
            [_issue(schema_code, f"{path} 顶层必须是 JSON object。")],
        )
    return data


def _source_record(source: SourceMetadata) -> SourceRecord:
    return SourceRecord(
        schema_version="1.0",
        source_id=source.source_id,
        scene=source.scene,
        group_id=source.group_id,
        original_filename=source.original_filename,
        original_relative_path=source.original_relative_path,
        original_sha256=source.original_sha256,
        original_width=source.original_width,
        original_height=source.original_height,
        tile_size=source.tile_size,
        overlap=source.overlap,
        stride=source.stride,
        annotator_id=source.annotator_id,
        created_at=source.created_at,
    )


def _build_statistics(
    annotations: dict[str, object],
) -> dict[str, object]:
    status_counts = {status: 0 for status in ("positive", "negative", "exclude", "uncertain")}
    objects_by_class = {class_name: 0 for class_name in FOD_CLASSES}
    completed = 0
    trainable = 0
    object_count = 0
    for annotation in annotations.values():
        status = annotation.status
        if status in status_counts:
            status_counts[status] += 1
        if annotation.completed:
            completed += 1
        if annotation.trainable:
            trainable += 1
        object_count += len(annotation.objects)
        for item in annotation.objects:
            objects_by_class[item.class_name] += 1
    return {
        **status_counts,
        "completed": completed,
        "trainable": trainable,
        "object_count": object_count,
        "objects_by_class": objects_by_class,
    }


def _canonical_tiles(
    records: list[TileRecord], annotations: dict[str, object]
) -> list[TileRecord]:
    canonical: list[TileRecord] = []
    for record in records:
        annotation = annotations[record.tile_id]
        canonical.append(
            TileRecord(
                tile_id=record.tile_id,
                source_id=record.source_id,
                scene=record.scene,
                x=record.x,
                y=record.y,
                width=record.width,
                height=record.height,
                tile_relative_path=(
                    f"sources/{record.source_id}/tiles/{record.tile_id}.jpg"
                ),
                status=annotation.status or "",
                trainable="true" if annotation.trainable else "false",
                object_count=len(annotation.objects),
                annotator=record.annotator,
                review_status="",
            )
        )
    return canonical


def _verify_tile_images(
    root: Path,
    records: list[TileRecord],
    progress_callback: PackageProgressCallback | None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    total = len(records)
    for current, record in enumerate(records, start=1):
        expected_relative = f"sources/{record.source_id}/tiles/{record.tile_id}.jpg"
        if record.tile_relative_path != expected_relative:
            issues.append(
                _issue(
                    "MANIFEST_MISMATCH",
                    "Tile 路径必须为 canonical Source 内路径："
                    f"{record.tile_relative_path!r} != {expected_relative!r}",
                    package=root,
                    source_id=record.source_id,
                    tile_id=record.tile_id,
                )
            )
            image_path = root.joinpath(*expected_relative.split("/"))
        else:
            image_path = root.joinpath(*record.tile_relative_path.split("/"))
        if not image_path.is_file():
            issues.append(
                _issue(
                    "MISSING_FILE",
                    f"Tile 图片不存在：{image_path}",
                    package=root,
                    source_id=record.source_id,
                    tile_id=record.tile_id,
                )
            )
        else:
            try:
                with Image.open(image_path) as image:
                    size = image.size
                    image_format = image.format
                    image.verify()
                if image_format != "JPEG":
                    issues.append(
                        _issue(
                            "CORRUPT_IMAGE",
                            f"Tile 文件不是 JPEG 格式：{image_path}",
                            package=root,
                            source_id=record.source_id,
                            tile_id=record.tile_id,
                        )
                    )
                if size != (record.width, record.height):
                    issues.append(
                        _issue(
                            "CORRUPT_IMAGE",
                            f"Tile 图片尺寸错误：实际 {size[0]} × {size[1]}，"
                            f"期望 {record.width} × {record.height}",
                            package=root,
                            source_id=record.source_id,
                            tile_id=record.tile_id,
                        )
                    )
            except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
                issues.append(
                    _issue(
                        "CORRUPT_IMAGE",
                        f"Tile 图片损坏或无法读取：{image_path}\n{exc}",
                        package=root,
                        source_id=record.source_id,
                        tile_id=record.tile_id,
                    )
                )
        if progress_callback is not None:
            progress_callback(current, total, record.source_id, record.tile_id)
    expected_paths = {
        root / "sources" / record.source_id / "tiles" / f"{record.tile_id}.jpg"
        for record in records
    }
    source_ids = {record.source_id for record in records}
    actual_paths = {
        path
        for source_id in source_ids
        for path in (root / "sources" / source_id / "tiles").glob("*.jpg")
        if path.is_file()
    }
    for extra_path in sorted(actual_paths - expected_paths):
        issues.append(
            _issue(
                "MANIFEST_MISMATCH",
                f"发现未登记在 tiles.csv 的 Tile 图片：{extra_path}",
                package=root,
                source_id=extra_path.parent.parent.name,
            )
        )
    return issues


def _compare_canonical_manifest(
    actual: list[TileRecord], canonical: list[TileRecord], root: Path
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for actual_record, expected in zip(actual, canonical):
        for field_name in ("status", "trainable", "object_count", "annotator", "review_status"):
            actual_value = getattr(actual_record, field_name)
            expected_value = getattr(expected, field_name)
            if actual_value != expected_value:
                issues.append(
                    _issue(
                        "MANIFEST_MISMATCH",
                        f"tiles.csv 字段 {field_name} 与 annotations.json 不一致："
                        f"CSV={actual_value!r}，期望={expected_value!r}",
                        package=root,
                        source_id=actual_record.source_id,
                        tile_id=actual_record.tile_id,
                    )
                )
    return issues


def _load_worker_content(
    output_root: str | Path,
    *,
    input_dir: str | Path | None,
    require_completed: bool,
    require_canonical_manifest: bool,
    progress_callback: PackageProgressCallback | None = None,
) -> WorkerPackageSnapshot:
    root = Path(output_root).resolve()
    if not root.is_dir():
        raise PackageValidationError(
            "Worker 成果目录不存在",
            [_issue("MISSING_FILE", f"Worker 成果目录不存在：{root}", package=root)],
        )

    try:
        sources = validate_stage3_sources(root)
    except TileInitializationError as exc:
        raise PackageValidationError(
            "Source 数据校验失败",
            [_issue("MANIFEST_MISMATCH", str(exc), package=root)],
        ) from exc
    try:
        tile_records = load_tile_records(root)
    except TileInitializationError as exc:
        message = str(exc)
        code = "MISSING_FILE" if "不存在" in message else "MANIFEST_MISMATCH"
        raise PackageValidationError(
            "Tile Manifest 校验失败",
            [_issue(code, message, package=root)],
        ) from exc

    issues: list[ValidationIssue] = []
    annotators = {source.annotator_id for source in sources}
    if len(annotators) != 1:
        issues.append(
            _issue(
                "MANIFEST_MISMATCH",
                "Worker Package 内所有 Source 的 annotator_id 必须一致："
                + ", ".join(sorted(annotators)),
                package=root,
            )
        )
    annotator_id = sources[0].annotator_id

    by_sha: dict[str, list[str]] = {}
    for source in sources:
        by_sha.setdefault(source.original_sha256, []).append(source.source_id)
        if not (root / "sources" / source.source_id / "annotations.json").is_file():
            issues.append(
                _issue(
                    "MISSING_FILE",
                    "annotations.json 不存在："
                    f"{root / 'sources' / source.source_id / 'annotations.json'}",
                    package=root,
                    source_id=source.source_id,
                )
            )
        if not (root / "sources" / source.source_id / "tiles").is_dir():
            issues.append(
                _issue(
                    "MISSING_FILE",
                    f"Tile 目录不存在：{root / 'sources' / source.source_id / 'tiles'}",
                    package=root,
                    source_id=source.source_id,
                )
            )
    for sha256_hex, source_ids in by_sha.items():
        if len(source_ids) > 1:
            issues.append(
                _issue(
                    "DUPLICATE_SOURCE_SHA256",
                    f"Worker 内检测到重复原图 SHA256 {sha256_hex}："
                    + ", ".join(source_ids),
                    package=root,
                )
            )
    if issues:
        raise PackageValidationError("Worker Package 结构校验失败", issues)

    try:
        storage = AnnotationStorage.load_existing_read_only(
            root,
            annotator_id,
            sources=sources,
            tile_records=tile_records,
        )
    except AnnotationStorageError as exc:
        message = str(exc)
        if "非法 bbox 类别" in message:
            code = "UNKNOWN_CLASS"
        elif any(
            marker in message
            for marker in (
                "Positive 状态",
                "不能设为 negative",
                "不能设为 exclude",
                "bbox",
                "completed",
                "trainable 与 status",
            )
        ):
            code = "ANNOTATION_INCONSISTENT"
        else:
            code = "ANNOTATION_SCHEMA_ERROR"
        raise PackageValidationError(
            "Annotation 数据校验失败",
            [_issue(code, message, package=root)],
        ) from exc
    try:
        annotations = dict(storage.annotations)
    finally:
        storage.close()

    completion_issues: list[ValidationIssue] = []
    for record in tile_records:
        annotation = annotations[record.tile_id]
        if annotation.status is None:
            completion_issues.append(
                _issue(
                    "INCOMPLETE_TILE",
                    "Tile 尚未设置 status。",
                    package=root,
                    source_id=record.source_id,
                    tile_id=record.tile_id,
                )
            )
        if require_completed and not annotation.completed:
            completion_issues.append(
                _issue(
                    "INCOMPLETE_TILE",
                    "Tile completed=false。",
                    package=root,
                    source_id=record.source_id,
                    tile_id=record.tile_id,
                )
            )

    image_issues = _verify_tile_images(root, tile_records, progress_callback)
    original_issues: list[ValidationIssue] = []
    if input_dir is not None:
        resolved_input = Path(input_dir).resolve()
        if not resolved_input.is_dir():
            original_issues.append(
                _issue(
                    "MISSING_FILE",
                    f"原图输入目录不存在：{resolved_input}",
                    package=root,
                )
            )
        else:
            try:
                validate_original_sources(resolved_input, sources)
            except TileInitializationError as exc:
                original_issues.append(
                    _issue("SOURCE_ORIGINAL_MISMATCH", str(exc), package=root)
                )

    canonical_tiles = _canonical_tiles(tile_records, annotations)
    manifest_issues = (
        _compare_canonical_manifest(tile_records, canonical_tiles, root)
        if require_canonical_manifest
        else []
    )
    all_issues = completion_issues + image_issues + original_issues + manifest_issues
    if all_issues:
        raise PackageValidationError("Worker Package 全量校验失败", all_issues)

    return WorkerPackageSnapshot(
        root=root,
        annotator_id=annotator_id,
        sources=sources,
        tiles=canonical_tiles,
        statistics=_build_statistics(annotations),
        scenes=sorted({source.scene for source in sources}),
    )


def compute_content_fingerprint(
    package_root: str | Path,
    *,
    sources_manifest: str | Path | None = None,
    tiles_manifest: str | Path | None = None,
    sources: Iterable[SourceMetadata] | None = None,
) -> str:
    """Hash canonical manifests plus every Source and Annotation JSON digest."""
    root = Path(package_root).resolve()
    source_records = list(sources) if sources is not None else validate_stage3_sources(root)
    paths: list[tuple[str, Path]] = [
        (
            "manifests/sources.csv",
            Path(sources_manifest) if sources_manifest else root / "manifests" / "sources.csv",
        ),
        (
            "manifests/tiles.csv",
            Path(tiles_manifest) if tiles_manifest else root / "manifests" / "tiles.csv",
        ),
    ]
    for source in sorted(source_records, key=lambda item: item.source_id):
        paths.extend(
            [
                (
                    f"sources/{source.source_id}/source.json",
                    root / "sources" / source.source_id / "source.json",
                ),
                (
                    f"sources/{source.source_id}/annotations.json",
                    root / "sources" / source.source_id / "annotations.json",
                ),
            ]
        )
    # 1.0 包保持原始指纹算法。1.1 额外绑定唯一清单的完整字节。
    if (root / MANIFEST_PATH).exists():
        paths.append((MANIFEST_PATH.as_posix(), root / MANIFEST_PATH))
    digest = hashlib.sha256()
    for label, path in paths:
        if not path.is_file():
            raise PackageValidationError(
                "content_fingerprint 计算失败",
                [_issue("MISSING_FILE", f"Fingerprint 输入文件不存在：{path}")],
            )
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(calculate_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_statistics(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    scalar_fields = (
        "positive",
        "negative",
        "exclude",
        "uncertain",
        "completed",
        "trainable",
        "object_count",
    )
    if any(
        not isinstance(value.get(field), int)
        or isinstance(value.get(field), bool)
        or value[field] < 0
        for field in scalar_fields
    ):
        return False
    by_class = value.get("objects_by_class")
    return isinstance(by_class, dict) and set(by_class) == set(FOD_CLASSES) and all(
        isinstance(by_class[name], int)
        and not isinstance(by_class[name], bool)
        and by_class[name] >= 0
        for name in FOD_CLASSES
    )


def load_finalized_worker_package(
    package_root: str | Path,
    progress_callback: PackageProgressCallback | None = None,
) -> WorkerPackageSnapshot:
    """Strictly revalidate a finalized Worker Package for Merge."""
    root = Path(package_root).resolve()
    package_path = root / "package.json"
    data = _read_json(package_path, "package.json")
    issues: list[ValidationIssue] = []
    missing = [field for field in PACKAGE_REQUIRED_FIELDS if field not in data]
    if missing:
        issues.append(
            _issue(
                "PACKAGE_SCHEMA_MISMATCH",
                "package.json 缺少字段：" + ", ".join(missing),
                package=root,
            )
        )
    if data.get("schema_version") not in {PACKAGE_SCHEMA_VERSION, "1.1"}:
        issues.append(
            _issue(
                "PACKAGE_SCHEMA_MISMATCH",
                f"package schema_version={data.get('schema_version')!r}，"
                "支持 '1.0' 或 '1.1'。",
                package=root,
            )
        )
    if data.get("package_type") != PACKAGE_TYPE:
        issues.append(
            _issue(
                "PACKAGE_SCHEMA_MISMATCH",
                f"package_type 必须为 {PACKAGE_TYPE!r}。",
                package=root,
            )
        )
    parsed_times = {}
    for field_name in ("created_at", "finalized_at"):
        try:
            parsed_times[field_name] = validate_utc_timestamp(
                data.get(field_name), field_name
            )
        except AnnotationStorageError as exc:
            issues.append(
                _issue("PACKAGE_SCHEMA_MISMATCH", str(exc), package=root)
            )
    if (
        "created_at" in parsed_times
        and "finalized_at" in parsed_times
        and parsed_times["finalized_at"] < parsed_times["created_at"]
    ):
        issues.append(
            _issue(
                "PACKAGE_SCHEMA_MISMATCH",
                "package finalized_at 不能早于 created_at。",
                package=root,
            )
        )
    if issues:
        raise PackageValidationError("Worker package.json 校验失败", issues)

    protected_path = root / MANIFEST_PATH
    if data["schema_version"] == "1.1" and not protected_path.is_file():
        raise PackageValidationError("重点负样本清单缺失", [
            _issue("MISSING_FILE", str(protected_path), package=root)
        ])
    if data["schema_version"] == "1.0" and protected_path.exists():
        raise PackageValidationError("1.0 包包含未声明的重点负样本清单，请重新 Finalize", [
            _issue("PACKAGE_SCHEMA_MISMATCH", str(protected_path), package=root)
        ])

    snapshot = _load_worker_content(
        root,
        input_dir=None,
        require_completed=True,
        require_canonical_manifest=True,
        progress_callback=progress_callback,
    )
    comparison_issues: list[ValidationIssue] = []
    expected_values = {
        "annotator_id": snapshot.annotator_id,
        "scenes": snapshot.scenes,
        "source_count": len(snapshot.sources),
        "tile_count": len(snapshot.tiles),
        "annotation_statistics": snapshot.statistics,
    }
    if not _validate_statistics(data.get("annotation_statistics")):
        comparison_issues.append(
            _issue(
                "PACKAGE_STATISTICS_MISMATCH",
                "package.json annotation_statistics 格式不合法。",
                package=root,
            )
        )
    for field_name, expected in expected_values.items():
        if data.get(field_name) != expected:
            comparison_issues.append(
                _issue(
                    "PACKAGE_STATISTICS_MISMATCH",
                    f"package.json {field_name} 与真实内容不一致："
                    f"声明={data.get(field_name)!r}，实际={expected!r}",
                    package=root,
                )
            )
    actual_fingerprint = compute_content_fingerprint(root, sources=snapshot.sources)
    declared_fingerprint = data.get("content_fingerprint")
    if (
        not isinstance(declared_fingerprint, str)
        or not FINGERPRINT_PATTERN.fullmatch(declared_fingerprint)
        or declared_fingerprint != actual_fingerprint
    ):
        comparison_issues.append(
            _issue(
                "PACKAGE_FINGERPRINT_MISMATCH",
                "Package 在 Finalize 后发生变化，请回到 Worker 工具重新封装。"
                f"\n声明={declared_fingerprint!r}\n实际={actual_fingerprint}",
                package=root,
            )
        )
    warnings: list[ValidationIssue] = []
    if data.get("tool_version") != TOOL_VERSION:
        warnings.append(
            _issue(
                "TOOL_VERSION_MISMATCH",
                f"Package tool_version={data.get('tool_version')!r}，"
                f"当前工具={TOOL_VERSION!r}；schema 相同，允许继续。",
                package=root,
                severity="warning",
            )
        )
    if comparison_issues:
        raise PackageValidationError("Worker Package 声明校验失败", comparison_issues + warnings)
    snapshot.content_fingerprint = actual_fingerprint
    snapshot.protected_negatives = _validate_protected_manifest(root, snapshot)
    snapshot.package_data = data
    snapshot.warnings = warnings
    return snapshot


def _validate_protected_manifest(root: Path, snapshot: WorkerPackageSnapshot) -> set[str]:
    """Finalize/Merge 共用严格校验，错误必须带具体 SourceID/TileID。"""
    try:
        storage = AnnotationStorage.load_existing_read_only(
            root, snapshot.annotator_id, sources=snapshot.sources,
            tile_records=snapshot.tiles,
        )
        try:
            return load_protected_negatives(
                root, snapshot.tiles, storage.annotations, require_completed=True
            )
        finally:
            storage.close()
    except (ValueError, OSError, AnnotationStorageError) as exc:
        raise PackageValidationError("重点负样本校验失败", [
            _issue("PROTECTED_NEGATIVE_INVALID", str(exc), package=root)
        ]) from exc


def _timestamp_for_filename(timestamp: str) -> str:
    return timestamp.replace("-", "").replace(":", "").replace("T", "_").rstrip("Z")


def _unique_validation_report_relative(root: Path, timestamp: str) -> Path:
    stem = f"package_validation_{_timestamp_for_filename(timestamp)}"
    candidate = Path("logs") / f"{stem}.json"
    suffix = 1
    while (root / candidate).exists():
        candidate = Path("logs") / f"{stem}_{suffix}.json"
        suffix += 1
    return candidate


def _write_validation_report(
    root: Path,
    status: str,
    timestamp: str,
    issues: list[ValidationIssue],
    summary: dict[str, object] | None = None,
) -> Path:
    report_path = root / _unique_validation_report_relative(root, timestamp)
    data: dict[str, object] = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "report_type": "worker_package_validation",
        "status": status,
        "created_at": timestamp,
        "error_count": sum(item.severity == "error" for item in issues),
        "warning_count": sum(item.severity == "warning" for item in issues),
        "errors": [item.to_dict() for item in issues if item.severity == "error"],
        "warnings": [item.to_dict() for item in issues if item.severity == "warning"],
    }
    if summary is not None:
        data["summary"] = summary
    try:
        atomic_write_json(report_path, data)
    except SourceInitializationError as exc:
        raise PackageValidationError(
            "Package validation report 写入失败",
            [_issue("REPORT_WRITE_FAILED", str(exc), package=root)],
        ) from exc
    return report_path


def _validation_report_data(
    status: str,
    timestamp: str,
    issues: list[ValidationIssue],
    summary: dict[str, object] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "report_type": "worker_package_validation",
        "status": status,
        "created_at": timestamp,
        "error_count": sum(item.severity == "error" for item in issues),
        "warning_count": sum(item.severity == "warning" for item in issues),
        "errors": [item.to_dict() for item in issues if item.severity == "error"],
        "warnings": [item.to_dict() for item in issues if item.severity == "warning"],
    }
    if summary is not None:
        data["summary"] = summary
    return data


def _append_operation_log(root: Path, message: str) -> None:
    log_path = root / "logs" / "operations.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"fod_package.{id(root)}.{os.getpid()}.{time.time_ns()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(log_path, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)sZ | %(levelname)s | %(message)s")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    try:
        logger.info(message)
        handler.flush()
        if handler.stream is not None:
            os.fsync(handler.stream.fileno())
    finally:
        handler.close()
        logger.removeHandler(handler)


def _cleanup_staging(path: Path, root: Path) -> None:
    try:
        resolved_path = path.resolve()
        resolved_path.relative_to(root.resolve())
        if not resolved_path.name.startswith(".tmp_package_finalize_"):
            return
    except (OSError, ValueError):
        return
    if resolved_path.exists():
        shutil.rmtree(resolved_path, ignore_errors=True)


def _commit_finalize_files(
    staging: Path, root: Path, report_relative_path: Path
) -> None:
    targets = [
        (staging / "manifests" / "sources.csv", root / "manifests" / "sources.csv"),
        (staging / "manifests" / "tiles.csv", root / "manifests" / "tiles.csv"),
        (staging / "package.json", root / "package.json"),
        (staging / report_relative_path, root / report_relative_path),
    ]
    backup_root = staging / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backups: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for _staged, target in targets:
            if target.exists():
                backup = backup_root / f"{len(backups)}_{target.name}"
                shutil.copy2(target, backup)
                backups[target] = backup
        for staged, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
            committed.append(target)
    except Exception as exc:
        rollback_issues: list[str] = []
        for target in reversed(committed):
            try:
                backup = backups.get(target)
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_issues.append(f"无法恢复 {target}：{rollback_exc}")
        message = f"Worker Package 提交失败：{exc}"
        if rollback_issues:
            message += "\n\n回滚未完全成功：\n" + "\n".join(rollback_issues)
        raise PackageValidationError(
            message,
            [_issue("COMMIT_FAILED", message, package=root)],
        ) from exc


def finalize_worker_package(
    input_dir: str | Path,
    output_root: str | Path,
    annotator_id: str | None = None,
    progress_callback: PackageProgressCallback | None = None,
) -> WorkerPackageResult:
    """Fully validate Stage 8 and atomically finalize the Worker Package."""
    root = Path(output_root).resolve()
    timestamp = utc_now_iso8601()
    staging: Path | None = None
    try:
        _append_operation_log(root, "Package finalize start")
        existing_package = root / "package.json"
        if existing_package.is_file():
            previous_schema = _read_json(existing_package, "已有 package.json").get("schema_version")
            if previous_schema == "1.1" and not (root / MANIFEST_PATH).is_file():
                raise PackageValidationError("1.1 Worker 的重点负样本清单丢失，拒绝降级封装", [
                    _issue("MISSING_FILE", str(root / MANIFEST_PATH), package=root)
                ])
        snapshot = _load_worker_content(
            root,
            input_dir=input_dir,
            require_completed=True,
            require_canonical_manifest=False,
            progress_callback=progress_callback,
        )
        if annotator_id is not None and annotator_id.strip() != snapshot.annotator_id:
            raise PackageValidationError(
                "当前 annotator_id 与 Worker 数据不一致",
                [
                    _issue(
                        "MANIFEST_MISMATCH",
                        f"GUI annotator_id={annotator_id.strip()!r}，"
                        f"Worker annotator_id={snapshot.annotator_id!r}。",
                        package=root,
                    )
                ],
            )
        _validate_protected_manifest(root, snapshot)
        staging = Path(
            tempfile.mkdtemp(prefix=".tmp_package_finalize_", dir=root)
        )
        canonical_sources = [_source_record(source) for source in snapshot.sources]
        write_sources_manifest(
            staging / "manifests" / "sources.csv", canonical_sources
        )
        write_tiles_manifest(staging / "manifests" / "tiles.csv", snapshot.tiles)
        fingerprint = compute_content_fingerprint(
            root,
            sources_manifest=staging / "manifests" / "sources.csv",
            tiles_manifest=staging / "manifests" / "tiles.csv",
            sources=snapshot.sources,
        )

        created_at = timestamp
        existing_package = root / "package.json"
        if existing_package.is_file():
            try:
                previous = _read_json(existing_package, "已有 package.json")
                validate_utc_timestamp(previous.get("created_at"), "created_at")
                if previous.get("package_type") == PACKAGE_TYPE:
                    created_at = str(previous["created_at"])
            except (PackageValidationError, AnnotationStorageError):
                created_at = timestamp
        package_data = {
            "schema_version": "1.1" if (root / MANIFEST_PATH).exists() else PACKAGE_SCHEMA_VERSION,
            "package_type": PACKAGE_TYPE,
            "tool_version": TOOL_VERSION,
            "annotator_id": snapshot.annotator_id,
            "created_at": created_at,
            "finalized_at": timestamp,
            "scenes": snapshot.scenes,
            "source_count": len(snapshot.sources),
            "tile_count": len(snapshot.tiles),
            "annotation_statistics": snapshot.statistics,
            "content_fingerprint": fingerprint,
        }
        atomic_write_json(staging / "package.json", package_data)

        summary = {
            "annotator_id": snapshot.annotator_id,
            "scenes": snapshot.scenes,
            "source_count": len(snapshot.sources),
            "tile_count": len(snapshot.tiles),
            "annotation_statistics": snapshot.statistics,
            "content_fingerprint": fingerprint,
        }
        report_relative_path = _unique_validation_report_relative(root, timestamp)
        atomic_write_json(
            staging / report_relative_path,
            _validation_report_data("success", timestamp, [], summary),
        )
        _commit_finalize_files(staging, root, report_relative_path)
        report_path = root / report_relative_path
        log_warning = None
        try:
            _append_operation_log(
                root,
                "Package finalize success | "
                f"fingerprint={fingerprint} | sources={len(snapshot.sources)} | "
                f"tiles={len(snapshot.tiles)}",
            )
        except OSError as exc:
            log_warning = f"Package 已成功提交，但成功日志写入失败：{exc}"
        return WorkerPackageResult(
            package_path=root / "package.json",
            validation_report_path=report_path,
            annotator_id=snapshot.annotator_id,
            scenes=tuple(snapshot.scenes),
            source_count=len(snapshot.sources),
            tile_count=len(snapshot.tiles),
            statistics=snapshot.statistics,
            content_fingerprint=fingerprint,
            log_warning=log_warning,
        )
    except PackageValidationError as exc:
        try:
            report_path = _write_validation_report(
                root, "failed", timestamp, exc.issues
            )
            exc.report_path = report_path
        except PackageValidationError:
            pass
        try:
            _append_operation_log(root, f"Package finalize fail | {exc}")
        except OSError:
            pass
        raise
    except (SourceInitializationError, TileInitializationError, OSError) as exc:
        wrapped = PackageValidationError(
            "Worker Package 封装失败",
            [_issue("FINALIZE_FAILED", str(exc), package=root)],
        )
        try:
            wrapped.report_path = _write_validation_report(
                root, "failed", timestamp, wrapped.issues
            )
        except PackageValidationError:
            pass
        try:
            _append_operation_log(root, f"Package finalize fail | {exc}")
        except OSError:
            pass
        raise wrapped from exc
    finally:
        if staging is not None:
            _cleanup_staging(staging, root)

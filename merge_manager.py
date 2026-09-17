"""Strict multi-Worker validation and staging-based Master Dataset merge."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from package_manager import (
    PACKAGE_SCHEMA_VERSION,
    TOOL_VERSION,
    PackageValidationError,
    ValidationIssue,
    WorkerPackageSnapshot,
    _source_record,
    load_finalized_worker_package,
)
from source_manager import (
    SourceInitializationError,
    atomic_write_json,
    utc_now_iso8601,
    write_sources_manifest,
)
from tile_manager import TileInitializationError, TileRecord, write_tiles_manifest


MergeProgressCallback = Callable[[int, int, int, int, str, str], None]


class MergeValidationError(Exception):
    """A strict merge preflight/build failure with a persisted report path."""

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

    @property
    def conflicts(self) -> list[ValidationIssue]:
        conflict_codes = {
            "DUPLICATE_SOURCE_ID",
            "DUPLICATE_SOURCE_SHA256",
            "SOURCE_ID_COLLISION",
            "DUPLICATE_TILE_ID",
            "DUPLICATE_PACKAGE",
        }
        return [item for item in self.errors if item.code in conflict_codes]

    def format_for_gui(self, limit: int = 10) -> str:
        visible = self.issues[:limit]
        details = "\n\n".join(
            f"{index}. [{item.code}] {item.message}"
            for index, item in enumerate(visible, start=1)
        )
        suffix = f"\n\n仅显示前 {limit} 项。" if len(self.issues) > limit else ""
        report = f"\n\n失败报告：\n{self.report_path}" if self.report_path else ""
        return (
            f"{self.message}\n\nERROR：{len(self.errors)}\n"
            f"WARNING：{len(self.warnings)}\n"
            f"CONFLICT：{len(self.conflicts)}\n\n{details}{suffix}{report}"
        )


@dataclass(frozen=True)
class MergeResult:
    master_root: Path
    master_json_path: Path
    report_path: Path
    worker_package_count: int
    source_count: int
    tile_count: int
    scenes: tuple[str, ...]
    scene_source_counts: dict[str, int]
    statistics: dict[str, object]
    warning_count: int


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


def _timestamp_for_filename(timestamp: str) -> str:
    return timestamp.replace("-", "").replace(":", "").replace("T", "_").rstrip("Z")


def _aggregate_statistics(
    snapshots: Iterable[WorkerPackageSnapshot],
) -> dict[str, object]:
    scalar_fields = (
        "positive",
        "negative",
        "exclude",
        "uncertain",
        "completed",
        "trainable",
        "object_count",
    )
    result: dict[str, object] = {field: 0 for field in scalar_fields}
    classes = {"metal": 0, "stone": 0, "rubber": 0, "other": 0}
    for snapshot in snapshots:
        for field in scalar_fields:
            result[field] = int(result[field]) + int(snapshot.statistics[field])
        package_classes = snapshot.statistics["objects_by_class"]
        for class_name in classes:
            classes[class_name] += int(package_classes[class_name])
    result["objects_by_class"] = classes
    return result


def _cross_package_issues(
    snapshots: list[WorkerPackageSnapshot],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    by_source_id: dict[str, list[tuple[WorkerPackageSnapshot, object]]] = {}
    by_sha: dict[str, list[tuple[WorkerPackageSnapshot, object]]] = {}
    by_tile_id: dict[str, list[tuple[WorkerPackageSnapshot, TileRecord]]] = {}
    for snapshot in snapshots:
        for source in snapshot.sources:
            by_source_id.setdefault(source.source_id, []).append((snapshot, source))
            by_sha.setdefault(source.original_sha256, []).append((snapshot, source))
        for tile in snapshot.tiles:
            by_tile_id.setdefault(tile.tile_id, []).append((snapshot, tile))

    for source_id, matches in sorted(by_source_id.items()):
        if len(matches) <= 1:
            continue
        hashes = {source.original_sha256 for _snapshot, source in matches}
        packages = ", ".join(str(snapshot.root) for snapshot, _source in matches)
        code = "DUPLICATE_SOURCE_ID" if len(hashes) == 1 else "SOURCE_ID_COLLISION"
        message = (
            f"SourceID {source_id} 同时出现在多个 Worker Package：{packages}。"
        )
        if len(hashes) == 1:
            message += " SHA256 完全一致，疑似同一原图被重复分配。"
        else:
            message += " 但完整 SHA256 不同，属于 SourceID 碰撞。"
        issues.append(_issue(code, message, source_id=source_id))

    for sha256_hex, matches in sorted(by_sha.items()):
        distinct_owners = {(snapshot.root, source.source_id) for snapshot, source in matches}
        if len(distinct_owners) <= 1:
            continue
        details = ", ".join(
            f"{source.source_id}@{snapshot.root.name}"
            for snapshot, source in matches
        )
        issues.append(
            _issue(
                "DUPLICATE_SOURCE_SHA256",
                f"跨 Package 原图 SHA256 重复：{sha256_hex}；{details}",
            )
        )

    for tile_id, matches in sorted(by_tile_id.items()):
        if len(matches) > 1:
            packages = ", ".join(str(snapshot.root) for snapshot, _tile in matches)
            issues.append(
                _issue(
                    "DUPLICATE_TILE_ID",
                    f"TileID {tile_id} 同时出现在多个 Package：{packages}",
                    tile_id=tile_id,
                )
            )
    return issues


def _validate_output_path(
    target: Path, package_roots: list[Path]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if target.exists():
        if not target.is_dir():
            issues.append(
                _issue("OUTPUT_NOT_EMPTY", f"Master 输出路径不是目录：{target}")
            )
        else:
            try:
                if any(target.iterdir()):
                    issues.append(
                        _issue(
                            "OUTPUT_NOT_EMPTY",
                            f"Master 输出目录已经存在且非空，拒绝覆盖：{target}",
                        )
                    )
            except OSError as exc:
                issues.append(_issue("OUTPUT_NOT_EMPTY", f"无法检查输出目录：{exc}"))
    for package_root in package_roots:
        try:
            target.relative_to(package_root)
        except ValueError:
            pass
        else:
            issues.append(
                _issue(
                    "OUTPUT_PATH_CONFLICT",
                    f"Master 输出目录不能位于 Worker Package 内：{target}",
                    package=package_root,
                )
            )
        try:
            package_root.relative_to(target)
        except ValueError:
            pass
        else:
            issues.append(
                _issue(
                    "OUTPUT_PATH_CONFLICT",
                    f"Worker Package 不能位于 Master 输出目录内：{package_root}",
                    package=package_root,
                )
            )
    return issues


def _write_failure_report(
    target: Path,
    timestamp: str,
    package_roots: list[Path],
    issues: list[ValidationIssue],
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    stem = f"{target.name}_merge_report_{_timestamp_for_filename(timestamp)}"
    report_path = target.parent / f"{stem}.json"
    suffix = 1
    while report_path.exists():
        report_path = target.parent / f"{stem}_{suffix}.json"
        suffix += 1
    data = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "report_type": "worker_package_merge",
        "status": "failed",
        "created_at": timestamp,
        "package_count": len(package_roots),
        "packages": [str(path) for path in package_roots],
        "package_summaries": [
            {
                "package": str(path),
                "directory_exists": path.is_dir(),
                "package_json_exists": (path / "package.json").is_file(),
            }
            for path in package_roots
        ],
        "error_count": sum(item.severity == "error" for item in issues),
        "warning_count": sum(item.severity == "warning" for item in issues),
        "conflict_count": sum(
            item.code
            in {
                "DUPLICATE_SOURCE_ID",
                "DUPLICATE_SOURCE_SHA256",
                "SOURCE_ID_COLLISION",
                "DUPLICATE_TILE_ID",
                "DUPLICATE_PACKAGE",
            }
            for item in issues
        ),
        "errors": [item.to_dict() for item in issues if item.severity == "error"],
        "warnings": [item.to_dict() for item in issues if item.severity == "warning"],
        "conflicts": [
            item.to_dict()
            for item in issues
            if item.code
            in {
                "DUPLICATE_SOURCE_ID",
                "DUPLICATE_SOURCE_SHA256",
                "SOURCE_ID_COLLISION",
                "DUPLICATE_TILE_ID",
                "DUPLICATE_PACKAGE",
            }
        ],
    }
    try:
        atomic_write_json(report_path, data)
    except SourceInitializationError as exc:
        raise MergeValidationError(
            "Merge 失败报告写入失败",
            [_issue("REPORT_WRITE_FAILED", str(exc))],
        ) from exc
    return report_path


def _write_merge_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"fod_merge.{id(path)}.{os.getpid()}.{time.time_ns()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(path, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)sZ | %(levelname)s | %(message)s")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    try:
        for line in lines:
            logger.info(line)
        handler.flush()
        if handler.stream is not None:
            os.fsync(handler.stream.fileno())
    finally:
        handler.close()
        logger.removeHandler(handler)


def _cleanup_master_staging(staging: Path, parent: Path) -> None:
    try:
        resolved_staging = staging.resolve()
        resolved_staging.relative_to(parent.resolve())
        if not resolved_staging.name.startswith(".tmp_master_merge_"):
            return
    except (OSError, ValueError):
        return
    if resolved_staging.exists():
        shutil.rmtree(resolved_staging, ignore_errors=True)


def _build_master(
    staging: Path,
    snapshots: list[WorkerPackageSnapshot],
    timestamp: str,
    warnings: list[ValidationIssue],
) -> tuple[dict[str, object], dict[str, object]]:
    source_pairs = sorted(
        (
            (source.scene, source.source_id, snapshot, source)
            for snapshot in snapshots
            for source in snapshot.sources
        ),
        key=lambda item: (item[0], item[1]),
    )
    source_order = {source_id: index for index, (_scene, source_id, _snapshot, _source) in enumerate(source_pairs)}
    all_tiles = sorted(
        (tile for snapshot in snapshots for tile in snapshot.tiles),
        key=lambda tile: (source_order[tile.source_id], tile.y, tile.x),
    )

    for _scene, source_id, snapshot, _source in source_pairs:
        source_root = snapshot.root / "sources" / source_id
        destination = staging / "sources" / source_id
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copy2(source_root / "source.json", destination / "source.json")
        shutil.copy2(
            source_root / "annotations.json", destination / "annotations.json"
        )
        shutil.copytree(source_root / "tiles", destination / "tiles")

    write_sources_manifest(
        staging / "manifests" / "sources.csv",
        [_source_record(source) for _scene, _source_id, _snapshot, source in source_pairs],
    )
    write_tiles_manifest(staging / "manifests" / "tiles.csv", all_tiles)

    statistics = _aggregate_statistics(snapshots)
    scenes = sorted({scene for snapshot in snapshots for scene in snapshot.scenes})
    worker_packages = [
        {
            "annotator_id": snapshot.annotator_id,
            "content_fingerprint": snapshot.content_fingerprint,
            "tool_version": snapshot.package_data["tool_version"],
        }
        for snapshot in snapshots
    ]
    master_data = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "dataset_type": "master",
        "tool_version": TOOL_VERSION,
        "created_at": timestamp,
        "worker_package_count": len(snapshots),
        "worker_packages": worker_packages,
        "scenes": scenes,
        "source_count": len(source_pairs),
        "tile_count": len(all_tiles),
        "annotation_statistics": statistics,
    }
    report_data = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "report_type": "worker_package_merge",
        "status": "success",
        "created_at": timestamp,
        "package_count": len(snapshots),
        "source_count": len(source_pairs),
        "tile_count": len(all_tiles),
        "warnings": [item.to_dict() for item in warnings],
        "conflicts": [],
        "statistics": statistics,
    }
    atomic_write_json(staging / "master.json", master_data)
    atomic_write_json(staging / "merge_report.json", report_data)
    _write_merge_log(
        staging / "logs" / "merge.log",
        [
            "merge start",
            "packages=" + ", ".join(str(snapshot.root) for snapshot in snapshots),
            f"validation result | errors=0 | warnings={len(warnings)}",
            "copy start",
            f"merge success | sources={len(source_pairs)} | tiles={len(all_tiles)}",
        ],
    )
    return master_data, report_data


def merge_worker_packages(
    package_roots: Iterable[str | Path],
    master_output_root: str | Path,
    progress_callback: MergeProgressCallback | None = None,
) -> MergeResult:
    """Preflight every package and conflict before staging the Master Dataset."""
    roots = [Path(path).resolve() for path in package_roots]
    target = Path(master_output_root).resolve()
    timestamp = utc_now_iso8601()
    initial_issues: list[ValidationIssue] = []
    if not roots:
        initial_issues.append(_issue("MISSING_PACKAGE", "至少需要选择一个 Worker Package。"))
    duplicates = sorted({path for path in roots if roots.count(path) > 1})
    for path in duplicates:
        initial_issues.append(
            _issue("DUPLICATE_PACKAGE", f"重复选择了 Worker Package：{path}", package=path)
        )
    initial_issues.extend(_validate_output_path(target, roots))

    snapshots: list[WorkerPackageSnapshot] = []
    validation_issues = list(initial_issues)
    package_total = len(roots)
    for package_index, root in enumerate(roots, start=1):
        def package_progress(
            current: int,
            total: int,
            source_id: str,
            tile_id: str,
            index: int = package_index,
        ) -> None:
            if progress_callback is not None:
                progress_callback(index, package_total, current, total, source_id, tile_id)

        try:
            snapshots.append(load_finalized_worker_package(root, package_progress))
        except PackageValidationError as exc:
            validation_issues.extend(exc.issues)

    if len(snapshots) == len(roots):
        validation_issues.extend(_cross_package_issues(snapshots))
    errors = [item for item in validation_issues if item.severity == "error"]
    warnings = [item for item in validation_issues if item.severity == "warning"]
    if errors:
        report_path = _write_failure_report(target, timestamp, roots, validation_issues)
        raise MergeValidationError(
            "Merge 全量预检失败，未生成 Master Dataset。",
            validation_issues,
            report_path,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".tmp_master_merge_", dir=target.parent)
    )
    target_was_empty = target.is_dir()
    try:
        master_data, _report_data = _build_master(
            staging, snapshots, timestamp, warnings
        )
        if target_was_empty:
            target.rmdir()
        os.replace(staging, target)
    except Exception as exc:
        _cleanup_master_staging(staging, target.parent)
        if target_was_empty and not target.exists():
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        issue = _issue("MERGE_BUILD_FAILED", f"Master staging 构建或提交失败：{exc}")
        report_path = _write_failure_report(
            target, timestamp, roots, warnings + [issue]
        )
        raise MergeValidationError(
            "Merge 构建失败，未留下正式半成品。",
            warnings + [issue],
            report_path,
        ) from exc

    scene_counts: dict[str, int] = {}
    for snapshot in snapshots:
        for source in snapshot.sources:
            scene_counts[source.scene] = scene_counts.get(source.scene, 0) + 1
    return MergeResult(
        master_root=target,
        master_json_path=target / "master.json",
        report_path=target / "merge_report.json",
        worker_package_count=len(snapshots),
        source_count=int(master_data["source_count"]),
        tile_count=int(master_data["tile_count"]),
        scenes=tuple(master_data["scenes"]),
        scene_source_counts=scene_counts,
        statistics=master_data["annotation_statistics"],
        warning_count=len(warnings),
    )

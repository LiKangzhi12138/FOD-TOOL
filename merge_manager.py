"""Strict multi-Worker validation and staging-based Master Dataset merge."""

from __future__ import annotations

import json
import csv
import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from package_manager import (
    PACKAGE_SCHEMA_VERSION,
    TOOL_VERSION,
    PackageValidationError,
    ValidationIssue,
    WorkerPackageSnapshot,
    _source_record,
    _build_statistics,
    load_finalized_worker_package,
)
from source_manager import (
    SourceInitializationError,
    atomic_write_json,
    calculate_sha256,
    utc_now_iso8601,
    write_sources_manifest,
)
from tile_manager import TileInitializationError, TileRecord, write_tiles_manifest, load_tile_records, validate_stage3_sources
from protected_negatives import MANIFEST_PATH, write_protected_negatives, load_protected_negatives
from annotation_storage import AnnotationStorage
from PIL import Image


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
    resolutions: list[dict] | None = None,
    input_source_count: int | None = None,
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
    # 每个 Worker 已逐条验证。原有跨包 Source/Tile 冲突检测先于此处执行。
    protected = set().union(*(snapshot.protected_negatives for snapshot in snapshots))
    write_protected_negatives(staging / MANIFEST_PATH, all_tiles, protected)
    master_data["schema_version"] = "1.1"
    master_data["protected_negatives"] = {
        "path": MANIFEST_PATH.as_posix(),
        "sha256": calculate_sha256(staging / MANIFEST_PATH),
        "count": len(protected),
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
    resolutions = resolutions or []
    summary = {
        "input_source_record_count": input_source_count if input_source_count is not None else len(source_pairs),
        "duplicate_source_count": len(resolutions),
        "unique_source_count": len(source_pairs),
        "skipped_source_record_count": sum(len(row["skipped_workers"]) for row in resolutions),
        "annotation_difference_source_count": sum(row["annotation_difference_detected"] for row in resolutions),
        "protected_negative_merge_count": sum(row["protected_negative_merge_count"] for row in resolutions),
        "protected_negative_count": len(protected),
    }
    report_data["duplicate_resolution"] = summary
    master_data["duplicate_resolution"] = summary
    with (staging / "duplicate_resolution_report.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "source_id", "selected_worker", "skipped_workers", "resolution",
            "annotation_difference_detected", "protected_negative_merge_count",
            "original_scenes", "final_scene", "final_group_id",
        ])
        writer.writeheader()
        for row in resolutions:
            writer.writerow({**row, "skipped_workers": json.dumps(row["skipped_workers"], ensure_ascii=False),
                "original_scenes": json.dumps(row["original_scenes"], ensure_ascii=False)})
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
    # 指纹从实际 Master 内容重建，包含图片、annotation、清单及去重报告。
    master_data["content_fingerprint"] = compute_master_fingerprint(staging)
    atomic_write_json(staging / "master.json", master_data)
    return master_data, report_data


def compute_master_fingerprint(root: str | Path) -> str:
    """按 POSIX 路径及文件 SHA256 绑定实际数据，排除日志和自引用 master.json。"""
    root = Path(root)
    paths = [path for path in root.rglob("*") if path.is_file()
             and path.relative_to(root).parts[0] != "logs"
             and path.relative_to(root).as_posix() != "master.json"]
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(calculate_sha256(path).encode("ascii") + b"\n")
    return digest.hexdigest()


def validate_master_dataset(root: str | Path) -> dict:
    """独立重读实际 Master 的格式、统计、保护清单和全文件内容指纹。"""
    root = Path(root)
    data = json.loads((root / "master.json").read_text(encoding="utf-8"))
    if data["content_fingerprint"] != compute_master_fingerprint(root):
        raise ValueError("Master content_fingerprint 不一致")
    # Master 保留各独立 Source 的真实切片参数，Worker/切片入口仍要求统一参数。
    sources = validate_stage3_sources(root, require_uniform_tiling=False)
    tiles = load_tile_records(root)
    annotations = {}
    for source in sources:
        # Master 可包含不同 annotator；按 Source 校验 JSON，最后统一校验整份保护清单。
        storage = AnnotationStorage(root, source.annotator_id,
            [source], [tile for tile in tiles if tile.source_id == source.source_id],
            enable_logging=False)
        storage._validate_project_metadata()
        storage._load_all()
        annotations.update(storage.annotations)
        storage.close()
    protected = load_protected_negatives(root, tiles, annotations, require_completed=True)
    if any(not annotation.completed for annotation in annotations.values()):
        raise ValueError("Master 包含尚未完成的 Tile")
    for tile in tiles:
        with Image.open(root / tile.tile_relative_path) as image:
            if image.size != (tile.width, tile.height):
                raise ValueError(f"Master Tile 尺寸不符：{tile.tile_id}")
            image.verify()
    if (len(sources) != data["source_count"] or len(tiles) != data["tile_count"]
            or _build_statistics(annotations) != data["annotation_statistics"]
            or len(protected) != data["protected_negatives"]["count"]
            or calculate_sha256(root / MANIFEST_PATH) != data["protected_negatives"]["sha256"]):
        raise ValueError("Master 实际内容与统计或重点负样本摘要不一致")
    return data


def _first_wins(snapshots, resolve_source_variants=False):
    """先验证几何与原图身份，再按传入顺序保留整份 Source。"""
    owners = {}
    hashes = {}
    source_ids = {}
    annotations = {}
    for snapshot in snapshots:
        storage = AnnotationStorage.load_existing_read_only(
            snapshot.root, snapshot.annotator_id, snapshot.sources, snapshot.tiles
        )
        annotations[snapshot.root] = storage.annotations
        storage.close()
        for source in snapshot.sources:
            previous_sha = source_ids.setdefault(source.source_id, source.original_sha256)
            if previous_sha != source.original_sha256:
                raise MergeValidationError("同 SourceID 的完整 SHA256 不同", [
                    _issue("SOURCE_ID_COLLISION", source.source_id, source_id=source.source_id)
                ])
            key = hashes.setdefault(source.original_sha256, source.source_id)
            owners.setdefault(key, []).append((snapshot, source))
    selected = {snapshot.root: [] for snapshot in snapshots}
    protected = {snapshot.root: set() for snapshot in snapshots}
    resolutions = []
    compatibility_issues = []
    for matches in owners.values():
        original_matches = list(matches)
        if resolve_source_variants and len(matches) > 1:
            # 同原图存在不同几何时，必须找到完整标准版本，保持整份 Source 不变。
            geometries = {(s.tile_size, s.overlap, s.stride) for _, s in matches}
            if len(geometries) > 1:
                standard = [(owner, s) for owner, s in matches
                            if (s.tile_size, s.overlap, s.stride) == (1024, 256, 768)
                            and len([t for t in owner.tiles if t.source_id == s.source_id]) == 77]
                if not standard:
                    raise MergeValidationError("切片冲突缺少完整标准77 Tile版本", [
                        _issue("SOURCE_METADATA_CONFLICT", matches[0][1].source_id)
                    ])
                chosen_match = standard[0]
                matches = [chosen_match] + [m for m in matches if m != chosen_match]
        winner, source = matches[0]
        selected[winner.root].append(source)
        tiles = [tile for tile in winner.tiles if tile.source_id == source.source_id]
        geometry = [(tile.x, tile.y, tile.width, tile.height) for tile in tiles]
        chosen = {tile.tile_id for tile in tiles} & winner.protected_negatives
        different = False
        extra = set()
        for other, candidate in matches[1:]:
            fields = ("scene", "group_id", "original_width", "original_height", "tile_size", "overlap", "stride")
            other_tiles = [tile for tile in other.tiles if tile.source_id == candidate.source_id]
            required_fields = ("original_width", "original_height") if resolve_source_variants else fields
            if any(getattr(source, field) != getattr(candidate, field) for field in required_fields) or (not resolve_source_variants and geometry != [
                (tile.x, tile.y, tile.width, tile.height) for tile in other_tiles
            ]):
                differences = {field: [getattr(source, field), getattr(candidate, field)]
                               for field in fields if getattr(source, field) != getattr(candidate, field)}
                compatibility_issues.append(_issue("SOURCE_METADATA_CONFLICT",
                    f"{source.source_id}：{winner.root} vs {other.root}；"
                    f"metadata差异={differences}；Tile数量={len(tiles)} vs {len(other_tiles)}",
                    source_id=source.source_id, package=other.root))
                continue
            # 忽略标注人员和时间。备注也参与比较，存在任何内容分歧时整份使用第一份。
            same = geometry == [(t.x, t.y, t.width, t.height) for t in other_tiles] and all(
                (annotations[winner.root][a.tile_id].status,
                 annotations[winner.root][a.tile_id].objects,
                 annotations[winner.root][a.tile_id].notes) ==
                (annotations[other.root][b.tile_id].status,
                 annotations[other.root][b.tile_id].objects,
                 annotations[other.root][b.tile_id].notes)
                for a, b in zip(tiles, other_tiles)
            )
            different |= not same
            if same:
                extra.update(a.tile_id for a, b in zip(tiles, other_tiles)
                             if b.tile_id in other.protected_negatives)
            if resolve_source_variants:
                # 逐 Tile 只转移合法 Negative 的保护标记，坐标/宽高必须完全一致。
                by_geometry = {(t.x, t.y, t.width, t.height): t for t in other_tiles}
                for tile in tiles:
                    other_tile = by_geometry.get((tile.x, tile.y, tile.width, tile.height))
                    if other_tile is None:
                        continue
                    a = annotations[winner.root][tile.tile_id]
                    b = annotations[other.root][other_tile.tile_id]
                    if (a.status == b.status == "negative" and not a.objects and not b.objects
                            and other_tile.tile_id in other.protected_negatives):
                        extra.add(tile.tile_id)
        # 只有整份 Source 标注一致才允许取保护标记并集。
        added = extra - chosen if resolve_source_variants or not different else set()
        protected[winner.root].update(chosen | added)
        if len(matches) > 1:
            row = {
                "source_id": source.source_id, "selected_worker": str(winner.root),
                "skipped_workers": [str(other.root) for other, _ in matches[1:]],
                "resolution": "standard_geometry_then_first_wins" if matches != original_matches else "first_wins", "annotation_difference_detected": different,
                "protected_negative_merge_count": len(added),
            }
            row["original_scenes"] = [{"worker": str(owner.root), "source_id": s.source_id,
                "scene": s.scene, "group_id": s.group_id,
                "tile_size": s.tile_size, "overlap": s.overlap, "stride": s.stride,
                "tile_count": sum(t.source_id == s.source_id for t in owner.tiles)}
                for owner, s in original_matches]
            row["final_scene"] = source.scene
            row["final_group_id"] = source.group_id
            resolutions.append(row)
    if compatibility_issues:
        raise MergeValidationError("重复 Source metadata 或 Tile 几何不兼容", compatibility_issues)
    filtered = []
    for snapshot in snapshots:
        ids = {source.source_id for source in selected[snapshot.root]}
        tiles = [tile for tile in snapshot.tiles if tile.source_id in ids]
        filtered.append(replace(snapshot, sources=selected[snapshot.root], tiles=tiles,
            scenes=sorted({source.scene for source in selected[snapshot.root]}),
            protected_negatives=protected[snapshot.root],
            statistics=_build_statistics({tile.tile_id: annotations[snapshot.root][tile.tile_id] for tile in tiles})))
    return filtered, resolutions


def merge_worker_packages(
    package_roots: Iterable[str | Path],
    master_output_root: str | Path,
    progress_callback: MergeProgressCallback | None = None,
    *,
    first_wins: bool = False,
    resolve_source_variants: bool = False,
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

    resolutions = []
    input_source_count = sum(len(snapshot.sources) for snapshot in snapshots)
    if len(snapshots) == len(roots):
        if first_wins:
            try:
                snapshots, resolutions = _first_wins(snapshots, resolve_source_variants)
            except MergeValidationError as exc:
                validation_issues.extend(exc.issues)
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
            staging, snapshots, timestamp, warnings, resolutions, input_source_count
        )
        validate_master_dataset(staging)
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

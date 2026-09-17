"""Strict Stage 8 annotations.json initialization, loading, and persistence."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from annotation_model import (
    AnnotationValidationError,
    BBoxAnnotation,
    TileAnnotation,
    derive_trainable,
    validate_annotation,
    validate_tile_annotation,
    validate_tile_completion,
    validate_tile_status,
)
from source_manager import utc_now_iso8601
from tile_manager import (
    SourceMetadata,
    TileInitializationError,
    TileRecord,
    load_tile_records,
    validate_stage3_sources,
)


ANNOTATION_SCHEMA_VERSION = "1.0"
TOP_LEVEL_FIELDS = (
    "schema_version",
    "source_id",
    "scene",
    "annotator_id",
    "created_by",
    "created_at",
    "updated_at",
    "tiles",
)
TILE_FIELDS = (
    "tile_id",
    "x",
    "y",
    "width",
    "height",
    "status",
    "trainable",
    "objects",
    "annotator",
    "created_by",
    "review_status",
    "reviewed_by",
    "notes",
    "completed",
    "completed_at",
    "created_at",
    "updated_at",
)
OBJECT_FIELDS = ("class_name", "bbox")

NowProvider = Callable[[], str]


class AnnotationStorageError(Exception):
    """A user-actionable annotation initialization or persistence failure."""

    def __init__(self, message: str, issues: Iterable[str] | None = None):
        super().__init__(message)
        self.message = message
        self.issues = list(issues or [])

    def __str__(self) -> str:
        if not self.issues:
            return self.message
        details = "\n\n".join(
            f"{index}. {issue}"
            for index, issue in enumerate(self.issues, start=1)
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
            suffix = f"\n\n仅显示前 {limit} 个问题。"
        return (
            f"{self.message}，共发现 {len(self.issues)} 个问题：\n\n"
            f"{details}{suffix}"
        )


@dataclass
class StoredTileAnnotation:
    manifest: TileRecord
    annotation: TileAnnotation
    annotator: str
    created_by: str
    review_status: None
    reviewed_by: None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "tile_id": self.manifest.tile_id,
            "x": self.manifest.x,
            "y": self.manifest.y,
            "width": self.manifest.width,
            "height": self.manifest.height,
            "status": self.annotation.status,
            "trainable": self.annotation.trainable,
            "objects": [
                {
                    "class_name": item.class_name,
                    "bbox": list(item.bbox),
                }
                for item in self.annotation.objects
            ],
            "annotator": self.annotator,
            "created_by": self.created_by,
            "review_status": self.review_status,
            "reviewed_by": self.reviewed_by,
            "notes": self.annotation.notes,
            "completed": self.annotation.completed,
            "completed_at": self.annotation.completed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class SourceAnnotationDocument:
    source: SourceMetadata
    annotator_id: str
    created_by: str
    created_at: str
    updated_at: str
    tiles: list[StoredTileAnnotation]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ANNOTATION_SCHEMA_VERSION,
            "source_id": self.source.source_id,
            "scene": self.source.scene,
            "annotator_id": self.annotator_id,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tiles": [tile.to_dict() for tile in self.tiles],
        }


def validate_utc_timestamp(value: object, field_name: str) -> datetime:
    """Validate a timezone-aware UTC ISO8601 string with the canonical Z suffix."""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AnnotationStorageError(
            f"{field_name} 必须是以 Z 结尾的 UTC ISO8601 时间字符串。"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise AnnotationStorageError(
            f"{field_name} 不是合法 UTC ISO8601 时间：{value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise AnnotationStorageError(f"{field_name} 必须使用 UTC 时区。")
    return parsed


def atomic_write_annotation_json(target: str | Path, data: dict[str, object]) -> None:
    """Atomically replace one UTF-8 annotation JSON using a same-directory temp."""
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            dir=target_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(data, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target_path)
    except Exception as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise AnnotationStorageError(
            f"annotations.json 原子写入失败：{target_path}\n{exc}"
        ) from exc


class AnnotationStorage:
    """Manage strict per-Source annotation documents and dirty autosave state."""

    def __init__(
        self,
        output_root: str | Path,
        annotator_id: str,
        sources: list[SourceMetadata],
        tile_records: list[TileRecord],
        now_provider: NowProvider = utc_now_iso8601,
        enable_logging: bool = True,
    ):
        self.output_root = Path(output_root).resolve()
        self.annotator_id = annotator_id
        self.sources = list(sources)
        self.tile_records = list(tile_records)
        self._now_provider = now_provider
        self._sources_by_id = {item.source_id: item for item in self.sources}
        self._tiles_by_id = {item.tile_id: item for item in self.tile_records}
        self._tiles_by_source: dict[str, list[TileRecord]] = {
            source.source_id: [] for source in self.sources
        }
        for record in self.tile_records:
            self._tiles_by_source.setdefault(record.source_id, []).append(record)

        self._documents: dict[str, SourceAnnotationDocument] = {}
        self.annotations: dict[str, TileAnnotation] = {}
        self.dirty_sources: set[str] = set()
        self._pending_change: dict[str, tuple[str, str, str]] = {}
        self._logger: logging.Logger | None = None
        self._log_handler: logging.FileHandler | None = None
        if enable_logging:
            self._logger, self._log_handler = self._create_logger()

    @classmethod
    def open_or_initialize(
        cls,
        output_root: str | Path,
        annotator_id: str,
        tile_records: list[TileRecord] | None = None,
        now_provider: NowProvider = utc_now_iso8601,
    ) -> "AnnotationStorage":
        """Validate Stage 4, then initialize all or strictly load all files."""
        root = Path(output_root).resolve()
        normalized_annotator = annotator_id.strip()
        if not normalized_annotator:
            raise AnnotationStorageError("当前 annotator_id 不能为空。")
        try:
            sources = validate_stage3_sources(root)
            records = list(tile_records) if tile_records is not None else load_tile_records(root)
        except TileInitializationError as exc:
            raise AnnotationStorageError(str(exc)) from exc

        storage = cls(
            root,
            normalized_annotator,
            sources,
            records,
            now_provider=now_provider,
        )
        try:
            storage._validate_project_metadata()
            paths = {
                source.source_id: storage.annotation_path(source.source_id)
                for source in sources
            }
            existing = [source_id for source_id, path in paths.items() if path.exists()]
            missing = [source_id for source_id, path in paths.items() if not path.exists()]
            if existing and missing:
                raise AnnotationStorageError(
                    "检测到标注数据仅部分初始化。\n\n"
                    "为了避免覆盖或丢失已有标注，程序不会自动补齐缺失 "
                    "annotations.json。\n\n"
                    f"已有：{', '.join(existing)}\n"
                    f"缺失：{', '.join(missing)}"
                )
            if not existing:
                storage._log(logging.INFO, "Annotation 初始化开始")
                storage._initialize_all()
                storage._log(logging.INFO, "Annotation 初始化成功")
            else:
                storage._load_all()
                storage._log(logging.INFO, "加载已有 Annotation 成功")
            return storage
        except Exception as exc:
            try:
                storage._log(logging.ERROR, f"Annotation schema 校验失败：{exc}")
            except AnnotationStorageError:
                pass
            storage.close()
            if isinstance(exc, AnnotationStorageError):
                raise
            raise AnnotationStorageError(f"Annotation 数据处理失败：{exc}") from exc

    @classmethod
    def load_existing_read_only(
        cls,
        output_root: str | Path,
        annotator_id: str,
        sources: list[SourceMetadata] | None = None,
        tile_records: list[TileRecord] | None = None,
    ) -> "AnnotationStorage":
        """Strictly load existing annotations without creating files or logs."""
        root = Path(output_root).resolve()
        normalized_annotator = annotator_id.strip()
        if not normalized_annotator:
            raise AnnotationStorageError("当前 annotator_id 不能为空。")
        try:
            loaded_sources = (
                list(sources) if sources is not None else validate_stage3_sources(root)
            )
            records = (
                list(tile_records)
                if tile_records is not None
                else load_tile_records(root)
            )
        except TileInitializationError as exc:
            raise AnnotationStorageError(str(exc)) from exc

        storage = cls(
            root,
            normalized_annotator,
            loaded_sources,
            records,
            enable_logging=False,
        )
        try:
            storage._validate_project_metadata()
            missing = [
                source.source_id
                for source in loaded_sources
                if not storage.annotation_path(source.source_id).is_file()
            ]
            if missing:
                raise AnnotationStorageError(
                    "缺少 annotations.json：" + ", ".join(missing)
                )
            storage._load_all()
            return storage
        except Exception as exc:
            storage.close()
            if isinstance(exc, AnnotationStorageError):
                raise
            raise AnnotationStorageError(
                f"Annotation 只读加载失败：{exc}"
            ) from exc

    @property
    def has_dirty_data(self) -> bool:
        return bool(self.dirty_sources)

    def annotation_path(self, source_id: str) -> Path:
        return self.output_root / "sources" / source_id / "annotations.json"

    def source_id_for_tile(self, tile_id: str) -> str:
        try:
            return self._tiles_by_id[tile_id].source_id
        except KeyError as exc:
            raise AnnotationStorageError(f"未知 TileID：{tile_id}") from exc

    def get_tile_annotation(self, tile_id: str) -> TileAnnotation:
        try:
            annotation = self.annotations[tile_id]
        except KeyError as exc:
            raise AnnotationStorageError(f"未知 TileID：{tile_id}") from exc
        return self._clone_annotation(annotation)

    def stage_tile_change(
        self, tile_id: str, annotation: TileAnnotation, change_type: str
    ) -> bool:
        """Update memory/timestamps and mark only the owning Source dirty."""
        if not isinstance(change_type, str) or not change_type.strip():
            raise AnnotationStorageError("change_type 不能为空。")
        record = self._tiles_by_id.get(tile_id)
        if record is None:
            raise AnnotationStorageError(f"未知 TileID：{tile_id}")
        current = self.annotations[tile_id]
        candidate = self._clone_annotation(annotation)

        semantic_changed = (
            current.status != candidate.status
            or current.objects != candidate.objects
            or current.notes != candidate.notes
        )
        if semantic_changed and current.completed and candidate.completed:
            candidate = candidate.mark_incomplete()
        self._validate_annotation_for_tile(candidate, record)
        if candidate == current:
            return False

        timestamp = self._now()
        source_id = record.source_id
        document = self._documents[source_id]
        stored_tile = self._stored_tile(tile_id)
        stored_tile.annotation = candidate
        stored_tile.updated_at = timestamp
        document.updated_at = timestamp
        self.annotations[tile_id] = candidate
        self.dirty_sources.add(source_id)
        self._pending_change[source_id] = (tile_id, change_type, timestamp)
        return True

    def save_tile_annotation(
        self, tile_id: str, annotation: TileAnnotation, change_type: str
    ) -> bool:
        """Stage one Tile change and atomically save only its owning Source."""
        changed = self.stage_tile_change(tile_id, annotation, change_type)
        source_id = self.source_id_for_tile(tile_id)
        if source_id in self.dirty_sources:
            self.save_source(source_id)
        return changed

    def save_source(self, source_id: str) -> None:
        """Validate and atomically write one dirty Source document."""
        if source_id not in self._documents:
            raise AnnotationStorageError(f"未知 SourceID：{source_id}")
        if source_id not in self.dirty_sources:
            return
        document = self._documents[source_id]
        data = document.to_dict()
        path = self.annotation_path(source_id)
        try:
            self._parse_document(
                data,
                self._sources_by_id[source_id],
                self._tiles_by_source[source_id],
                path,
            )
            atomic_write_annotation_json(path, data)
            tile_id, change_type, timestamp = self._pending_change[source_id]
            self._log(
                logging.INFO,
                "Annotation 自动保存成功 | "
                f"source_id={source_id} | tile_id={tile_id} | "
                f"change_type={change_type} | timestamp={timestamp}",
            )
        except Exception as exc:
            pending = self._pending_change.get(source_id)
            pending_details = ""
            if pending is not None:
                tile_id, change_type, timestamp = pending
                pending_details = (
                    f" | tile_id={tile_id} | change_type={change_type} | "
                    f"timestamp={timestamp}"
                )
            try:
                self._log(
                    logging.ERROR,
                    "Annotation 保存失败 | "
                    f"source_id={source_id}{pending_details} | error={exc}",
                )
            except AnnotationStorageError:
                pass
            if isinstance(exc, AnnotationStorageError):
                raise
            raise AnnotationStorageError(
                f"Annotation 保存失败：{path}\n{exc}"
            ) from exc
        self.dirty_sources.discard(source_id)
        self._pending_change.pop(source_id, None)

    def flush_all(self) -> None:
        """Retry every dirty Source, preserving dirty markers on any failure."""
        for source_id in list(self.dirty_sources):
            self.save_source(source_id)

    def close(self) -> None:
        if getattr(self, "_log_handler", None) is not None:
            self._log_handler.close()
            if self._logger is not None:
                self._logger.removeHandler(self._log_handler)
            self._log_handler = None

    def _now(self) -> str:
        value = self._now_provider()
        validate_utc_timestamp(value, "当前时间")
        return value

    @staticmethod
    def _clone_annotation(annotation: TileAnnotation) -> TileAnnotation:
        validate_tile_annotation(annotation)
        return TileAnnotation(
            status=annotation.status,
            objects=list(annotation.objects),
            notes=annotation.notes,
            completed=annotation.completed,
            completed_at=annotation.completed_at,
        )

    def _validate_project_metadata(self) -> None:
        issues: list[str] = []
        source_ids = set(self._sources_by_id)
        tile_source_ids = {record.source_id for record in self.tile_records}
        if source_ids != tile_source_ids:
            missing = sorted(source_ids - tile_source_ids)
            extra = sorted(tile_source_ids - source_ids)
            if missing:
                issues.append("以下 Source 在 tiles.csv 中没有 Tile：" + ", ".join(missing))
            if extra:
                issues.append("tiles.csv 包含未知 Source：" + ", ".join(extra))

        for source in self.sources:
            if source.annotator_id != self.annotator_id:
                issues.append(
                    f"Source {source.source_id}：当前 annotator_id "
                    f"{self.annotator_id!r} 与 Source metadata "
                    f"{source.annotator_id!r} 不一致。"
                )
        for record in self.tile_records:
            source = self._sources_by_id.get(record.source_id)
            if source is None:
                continue
            if record.scene != source.scene:
                issues.append(
                    f"Tile {record.tile_id}：scene={record.scene!r} 与 Source "
                    f"scene={source.scene!r} 不一致。"
                )
            if record.annotator != source.annotator_id:
                issues.append(
                    f"Tile {record.tile_id}：annotator={record.annotator!r} 与 "
                    f"Source annotator_id={source.annotator_id!r} 不一致。"
                )
        if issues:
            raise AnnotationStorageError("Annotation 项目 metadata 校验失败", issues)

    def _initialize_all(self) -> None:
        timestamp = self._now()
        documents: dict[str, SourceAnnotationDocument] = {}
        for source in self.sources:
            stored_tiles = [
                StoredTileAnnotation(
                    manifest=record,
                    annotation=TileAnnotation(),
                    annotator=self.annotator_id,
                    created_by=self.annotator_id,
                    review_status=None,
                    reviewed_by=None,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                for record in self._tiles_by_source[source.source_id]
            ]
            documents[source.source_id] = SourceAnnotationDocument(
                source=source,
                annotator_id=self.annotator_id,
                created_by=self.annotator_id,
                created_at=timestamp,
                updated_at=timestamp,
                tiles=stored_tiles,
            )

        staging_root = Path(
            tempfile.mkdtemp(prefix=".tmp_annotation_init_", dir=self.output_root)
        )
        committed: list[Path] = []
        try:
            for source in self.sources:
                source_id = source.source_id
                staged_path = staging_root / source_id / "annotations.json"
                data = documents[source_id].to_dict()
                self._parse_document(
                    data, source, self._tiles_by_source[source_id], staged_path
                )
                atomic_write_annotation_json(staged_path, data)

            for source in self.sources:
                source_id = source.source_id
                target = self.annotation_path(source_id)
                if target.exists():
                    raise AnnotationStorageError(
                        f"提交时发现 annotations.json 已存在，拒绝覆盖：{target}"
                    )
                staged_path = staging_root / source_id / "annotations.json"
                os.rename(staged_path, target)
                committed.append(target)
        except Exception as exc:
            rollback_issues: list[str] = []
            for target in reversed(committed):
                try:
                    target.unlink(missing_ok=True)
                except OSError as rollback_exc:
                    rollback_issues.append(f"无法撤回 {target}：{rollback_exc}")
            message = str(exc)
            if rollback_issues:
                message += "\n\n回滚未完全成功：\n" + "\n".join(rollback_issues)
            raise AnnotationStorageError(message) from exc
        finally:
            self._cleanup_staging(staging_root)

        self._documents = documents
        self._rebuild_annotations_index()

    def _load_all(self) -> None:
        documents: dict[str, SourceAnnotationDocument] = {}
        issues: list[str] = []
        for source in self.sources:
            path = self.annotation_path(source.source_id)
            try:
                data = self._read_json(path)
                documents[source.source_id] = self._parse_document(
                    data,
                    source,
                    self._tiles_by_source[source.source_id],
                    path,
                )
            except AnnotationStorageError as exc:
                issues.append(
                    f"Source：{source.source_id}\n文件：{path}\n问题：{exc}"
                )
        if issues:
            raise AnnotationStorageError("Annotation 数据严格校验失败", issues)
        self._documents = documents
        self._rebuild_annotations_index()

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            with path.open("r", encoding="utf-8") as file_handle:
                data = json.load(file_handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AnnotationStorageError(
                f"annotations.json 无法读取或 JSON 损坏：{exc}"
            ) from exc
        if not isinstance(data, dict):
            raise AnnotationStorageError("annotations.json 顶层必须是 JSON object。")
        return data

    def _parse_document(
        self,
        data: dict[str, object],
        source: SourceMetadata,
        expected_tiles: list[TileRecord],
        path: Path,
    ) -> SourceAnnotationDocument:
        self._require_exact_fields(data, TOP_LEVEL_FIELDS, "annotations.json 顶层")
        if data["schema_version"] != ANNOTATION_SCHEMA_VERSION:
            raise AnnotationStorageError(
                f"不支持的 schema_version：{data['schema_version']!r}，"
                f"期望 {ANNOTATION_SCHEMA_VERSION!r}。"
            )
        expected_top_values = {
            "source_id": source.source_id,
            "scene": source.scene,
            "annotator_id": source.annotator_id,
            "created_by": source.annotator_id,
        }
        for field_name, expected in expected_top_values.items():
            if data[field_name] != expected:
                raise AnnotationStorageError(
                    f"顶层字段 {field_name} 不一致："
                    f"annotations={data[field_name]!r}，期望={expected!r}。"
                )
        if data["annotator_id"] != self.annotator_id:
            raise AnnotationStorageError(
                "当前 annotator_id 与已有 Worker Annotation 不一致："
                f"当前={self.annotator_id!r}，"
                f"annotations={data['annotator_id']!r}。"
            )
        created_at = self._require_timestamp(data["created_at"], "created_at")
        updated_at = self._require_timestamp(data["updated_at"], "updated_at")
        if updated_at[1] < created_at[1]:
            raise AnnotationStorageError("顶层 updated_at 不能早于 created_at。")

        raw_tiles = data["tiles"]
        if not isinstance(raw_tiles, list):
            raise AnnotationStorageError("顶层 tiles 必须是 list。")
        actual_ids: list[object] = [
            item.get("tile_id") if isinstance(item, dict) else None
            for item in raw_tiles
        ]
        expected_ids = [item.tile_id for item in expected_tiles]
        string_ids = [item for item in actual_ids if isinstance(item, str)]
        duplicates = sorted(
            item for item in set(string_ids) if string_ids.count(item) > 1
        )
        if duplicates:
            raise AnnotationStorageError(
                "annotations.json 包含重复 TileID：" + ", ".join(duplicates)
            )
        actual_id_set = set(string_ids)
        expected_id_set = set(expected_ids)
        if actual_id_set != expected_id_set or len(raw_tiles) != len(expected_tiles):
            missing = sorted(expected_id_set - actual_id_set)
            extra = sorted(actual_id_set - expected_id_set)
            details = [
                f"期望 Tile 数量={len(expected_tiles)}，实际={len(raw_tiles)}"
            ]
            if missing:
                details.append("缺少 Tile：" + ", ".join(missing))
            if extra:
                details.append("多余 Tile：" + ", ".join(extra))
            raise AnnotationStorageError("Tile 集合与 tiles.csv 不一致：\n" + "\n".join(details))
        if actual_ids != expected_ids:
            raise AnnotationStorageError(
                "annotations.json Tile 顺序与 tiles.csv 不一致。"
            )

        stored_tiles: list[StoredTileAnnotation] = []
        latest_tile_updated = created_at[1]
        for index, (raw_tile, manifest) in enumerate(
            zip(raw_tiles, expected_tiles), start=1
        ):
            try:
                stored = self._parse_tile(raw_tile, manifest)
            except AnnotationStorageError as exc:
                raise AnnotationStorageError(
                    f"Tile {manifest.tile_id}（tiles[{index - 1}]）：{exc}"
                ) from exc
            stored_tiles.append(stored)
            latest_tile_updated = max(
                latest_tile_updated,
                validate_utc_timestamp(stored.updated_at, "tile.updated_at"),
            )
        if updated_at[1] < latest_tile_updated:
            raise AnnotationStorageError(
                "顶层 updated_at 早于某个 Tile 的 updated_at。"
            )
        return SourceAnnotationDocument(
            source=source,
            annotator_id=str(data["annotator_id"]),
            created_by=str(data["created_by"]),
            created_at=created_at[0],
            updated_at=updated_at[0],
            tiles=stored_tiles,
        )

    def _parse_tile(
        self, raw_tile: object, manifest: TileRecord
    ) -> StoredTileAnnotation:
        if not isinstance(raw_tile, dict):
            raise AnnotationStorageError("Tile 记录必须是 JSON object。")
        self._require_exact_fields(raw_tile, TILE_FIELDS, "Tile 记录")
        expected_geometry = {
            "tile_id": manifest.tile_id,
            "x": manifest.x,
            "y": manifest.y,
            "width": manifest.width,
            "height": manifest.height,
        }
        for field_name, expected in expected_geometry.items():
            actual = raw_tile[field_name]
            if field_name != "tile_id" and (
                not isinstance(actual, int) or isinstance(actual, bool)
            ):
                raise AnnotationStorageError(f"字段 {field_name} 必须是整数。")
            if actual != expected:
                raise AnnotationStorageError(
                    f"字段 {field_name} 与 tiles.csv 不一致："
                    f"annotations={actual!r}，tiles.csv={expected!r}。"
                )

        status = raw_tile["status"]
        if status is not None and not isinstance(status, str):
            raise AnnotationStorageError("status 必须是字符串或 null。")
        try:
            validate_tile_status(status)
        except AnnotationValidationError as exc:
            raise AnnotationStorageError(str(exc)) from exc
        trainable = raw_tile["trainable"]
        if not isinstance(trainable, bool):
            raise AnnotationStorageError("trainable 必须是布尔值。")
        expected_trainable = derive_trainable(status)
        if trainable is not expected_trainable:
            raise AnnotationStorageError(
                f"trainable 与 status 不一致：status={status!r}，"
                f"trainable={trainable!r}，期望={expected_trainable!r}。"
            )

        raw_objects = raw_tile["objects"]
        if not isinstance(raw_objects, list):
            raise AnnotationStorageError("objects 必须是 list。")
        objects: list[BBoxAnnotation] = []
        for object_index, raw_object in enumerate(raw_objects):
            try:
                objects.append(self._parse_object(raw_object, manifest))
            except AnnotationStorageError as exc:
                raise AnnotationStorageError(
                    f"objects[{object_index}]：{exc}"
                ) from exc

        for field_name in ("annotator", "created_by"):
            if raw_tile[field_name] != self.annotator_id:
                raise AnnotationStorageError(
                    f"{field_name}={raw_tile[field_name]!r} 与当前 annotator_id "
                    f"{self.annotator_id!r} 不一致。"
                )
        if raw_tile["review_status"] is not None:
            raise AnnotationStorageError("Stage 8 review_status 必须为 null。")
        if raw_tile["reviewed_by"] is not None:
            raise AnnotationStorageError("Stage 8 reviewed_by 必须为 null。")
        notes = raw_tile["notes"]
        if not isinstance(notes, str):
            raise AnnotationStorageError("notes 必须是字符串。")
        completed = raw_tile["completed"]
        if not isinstance(completed, bool):
            raise AnnotationStorageError("completed 必须是布尔值。")
        completed_at = raw_tile["completed_at"]
        if completed:
            if completed_at is None:
                raise AnnotationStorageError(
                    "completed=true 时 completed_at 不能为 null。"
                )
            self._require_timestamp(completed_at, "completed_at")
        elif completed_at is not None:
            raise AnnotationStorageError(
                "completed=false 时 completed_at 必须为 null。"
            )

        try:
            annotation = TileAnnotation(
                status=status,
                objects=objects,
                notes=notes,
                completed=completed,
                completed_at=completed_at,
            )
            validate_tile_annotation(annotation)
            if completed:
                validate_tile_completion(annotation)
        except AnnotationValidationError as exc:
            raise AnnotationStorageError(str(exc)) from exc

        created_at = self._require_timestamp(
            raw_tile["created_at"], "tile.created_at"
        )
        updated_at = self._require_timestamp(
            raw_tile["updated_at"], "tile.updated_at"
        )
        if updated_at[1] < created_at[1]:
            raise AnnotationStorageError(
                "Tile updated_at 不能早于 created_at。"
            )
        return StoredTileAnnotation(
            manifest=manifest,
            annotation=annotation,
            annotator=str(raw_tile["annotator"]),
            created_by=str(raw_tile["created_by"]),
            review_status=None,
            reviewed_by=None,
            created_at=created_at[0],
            updated_at=updated_at[0],
        )

    @staticmethod
    def _parse_object(raw_object: object, manifest: TileRecord) -> BBoxAnnotation:
        if not isinstance(raw_object, dict):
            raise AnnotationStorageError("bbox object 必须是 JSON object。")
        AnnotationStorage._require_exact_fields(
            raw_object, OBJECT_FIELDS, "bbox object"
        )
        class_name = raw_object["class_name"]
        bbox = raw_object["bbox"]
        if not isinstance(class_name, str):
            raise AnnotationStorageError("class_name 必须是字符串。")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise AnnotationStorageError("bbox 必须是包含 4 个整数的 list。")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in bbox):
            raise AnnotationStorageError("bbox 坐标必须全部是整数。")
        try:
            annotation = BBoxAnnotation(class_name, tuple(bbox))
            validate_annotation(annotation, manifest.width, manifest.height)
        except AnnotationValidationError as exc:
            raise AnnotationStorageError(str(exc)) from exc
        return annotation

    def _validate_annotation_for_tile(
        self, annotation: TileAnnotation, manifest: TileRecord
    ) -> None:
        try:
            validate_tile_annotation(annotation)
            for item in annotation.objects:
                validate_annotation(item, manifest.width, manifest.height)
            if annotation.completed:
                validate_tile_completion(annotation)
                validate_utc_timestamp(annotation.completed_at, "completed_at")
        except (AnnotationValidationError, AnnotationStorageError) as exc:
            raise AnnotationStorageError(
                f"Tile {manifest.tile_id} annotation 校验失败：{exc}"
            ) from exc

    def _stored_tile(self, tile_id: str) -> StoredTileAnnotation:
        source_id = self.source_id_for_tile(tile_id)
        for item in self._documents[source_id].tiles:
            if item.manifest.tile_id == tile_id:
                return item
        raise AnnotationStorageError(f"Source 文档中缺少 Tile：{tile_id}")

    def _rebuild_annotations_index(self) -> None:
        self.annotations = {
            item.manifest.tile_id: item.annotation
            for source in self.sources
            for item in self._documents[source.source_id].tiles
        }

    @staticmethod
    def _require_exact_fields(
        data: dict[str, object], fields: Iterable[str], context: str
    ) -> None:
        expected = set(fields)
        actual = set(data)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append("缺少字段：" + ", ".join(missing))
            if extra:
                details.append("未知字段：" + ", ".join(extra))
            raise AnnotationStorageError(f"{context} schema 不匹配：" + "；".join(details))

    @staticmethod
    def _require_timestamp(value: object, field_name: str) -> tuple[str, datetime]:
        parsed = validate_utc_timestamp(value, field_name)
        return str(value), parsed

    def _create_logger(self) -> tuple[logging.Logger, logging.FileHandler]:
        log_path = self.output_root / "logs" / "operations.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logger = logging.getLogger(
                f"fod_annotation.{id(self)}.{os.getpid()}"
            )
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = logging.FileHandler(log_path, encoding="utf-8")
            formatter = logging.Formatter(
                "%(asctime)sZ | %(levelname)s | %(message)s"
            )
            formatter.converter = time.gmtime
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            return logger, handler
        except OSError as exc:
            raise AnnotationStorageError(
                f"Annotation 操作日志创建失败：{log_path}\n{exc}"
            ) from exc

    def _log(self, level: int, message: str) -> None:
        if self._logger is None or self._log_handler is None:
            return
        try:
            self._logger.log(level, message)
            self._log_handler.flush()
            if self._log_handler.stream is not None:
                os.fsync(self._log_handler.stream.fileno())
        except OSError as exc:
            raise AnnotationStorageError(f"Annotation 操作日志写入失败：{exc}") from exc

    def _cleanup_staging(self, staging_root: Path) -> None:
        try:
            resolved = staging_root.resolve()
            resolved.relative_to(self.output_root)
            if not resolved.name.startswith(".tmp_annotation_init_"):
                return
        except (OSError, ValueError):
            return
        if not staging_root.exists():
            return
        for path in sorted(
            staging_root.rglob("*"), key=lambda item: len(item.parts), reverse=True
        ):
            try:
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            staging_root.rmdir()
        except OSError:
            pass

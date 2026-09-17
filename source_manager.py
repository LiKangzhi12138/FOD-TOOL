"""Source image validation and metadata initialization for Stage 3."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, UnidentifiedImageError


SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
EXPECTED_WIDTH = 8192
EXPECTED_HEIGHT = 5460
SHA256_CHUNK_SIZE = 4 * 1024 * 1024
SOURCE_SCHEMA_VERSION = "1.0"
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

ProgressCallback = Callable[[int, int, str], None]


class SourceInitializationError(Exception):
    """A user-actionable failure during Source initialization."""

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
        """Return a bounded message while retaining the total issue count."""
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
class SourceRecord:
    schema_version: str
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

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True)
class SourceInitializationResult:
    scene: str
    source_count: int
    tile_size: int
    overlap: int
    stride: int
    manifest_path: Path
    log_warning: str | None = None


def utc_now_iso8601() -> str:
    """Return a timezone-aware UTC timestamp using the canonical Z suffix."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def calculate_stride(tile_size: int, overlap: int) -> int:
    """Validate tiling settings and derive stride from them."""
    if tile_size <= 0:
        raise SourceInitializationError("Tile Size 必须大于 0。")
    if overlap < 0:
        raise SourceInitializationError("Overlap 不能小于 0。")
    if overlap >= tile_size:
        raise SourceInitializationError("Overlap 必须小于 Tile Size。")
    return tile_size - overlap


def generate_source_id(scene: str, sha256_hex: str) -> str:
    """Build the globally deterministic SourceID."""
    if scene not in {"S01", "S02", "S03"}:
        raise SourceInitializationError(f"不支持的场景代码：{scene}")
    normalized_hash = sha256_hex.lower()
    if len(normalized_hash) != 64 or any(
        character not in "0123456789abcdef" for character in normalized_hash
    ):
        raise SourceInitializationError("SHA256 必须是 64 位十六进制字符串。")
    return f"{scene}_{normalized_hash[:16].upper()}"


def validate_path_relationship(
    input_dir: str | Path, output_root: str | Path
) -> tuple[Path, Path]:
    """Resolve paths and reject equal or nested input/output directories."""
    resolved_input = Path(input_dir).expanduser().resolve()
    resolved_output = Path(output_root).expanduser().resolve()

    if resolved_input == resolved_output:
        raise SourceInitializationError("原图输入目录与成果输出目录不能相同。")

    try:
        resolved_output.relative_to(resolved_input)
    except ValueError:
        pass
    else:
        raise SourceInitializationError("成果输出目录不能位于原图输入目录内部。")

    try:
        resolved_input.relative_to(resolved_output)
    except ValueError:
        pass
    else:
        raise SourceInitializationError("原图输入目录不能位于成果输出目录内部。")

    return resolved_input, resolved_output


def find_image_files(input_dir: str | Path) -> list[Path]:
    """Recursively find supported images in stable relative POSIX path order."""
    root = Path(input_dir)
    images = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
    ]
    return sorted(images, key=lambda path: path.relative_to(root).as_posix())


def relative_posix_path(file_path: str | Path, input_dir: str | Path) -> str:
    """Return a metadata-safe relative path with forward slashes."""
    return Path(file_path).relative_to(Path(input_dir)).as_posix()


def calculate_sha256(file_path: str | Path) -> str:
    """Calculate a full lowercase SHA256 without loading the file into memory."""
    digest = hashlib.sha256()
    with Path(file_path).open("rb") as file_handle:
        while chunk := file_handle.read(SHA256_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_image(file_path: str | Path) -> tuple[int, int]:
    """Read image dimensions and ask Pillow to verify basic file integrity."""
    path = Path(file_path)
    try:
        with Image.open(path) as image:
            width, height = image.size
            image.verify()
    except UnidentifiedImageError as exc:
        raise SourceInitializationError(
            f"文件：\n{path}\n\nPillow 无法识别该图片。"
        ) from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise SourceInitializationError(
            f"文件：\n{path}\n\n图片损坏或无法读取：{exc}"
        ) from exc

    if width != EXPECTED_WIDTH or height != EXPECTED_HEIGHT:
        raise SourceInitializationError(
            f"文件：\n{path}\n\n"
            f"期望尺寸：\n{EXPECTED_WIDTH} × {EXPECTED_HEIGHT}\n\n"
            f"实际尺寸：\n{width} × {height}"
        )
    return width, height


def build_source_records(
    image_files: Iterable[Path],
    input_dir: Path,
    scene: str,
    annotator_id: str,
    tile_size: int,
    overlap: int,
    created_at: str,
    progress_callback: ProgressCallback | None = None,
) -> list[SourceRecord]:
    """Inspect and hash all images, collecting per-image preflight problems."""
    files = list(image_files)
    stride = calculate_stride(tile_size, overlap)
    records: list[SourceRecord] = []
    issues: list[str] = []

    for current, image_path in enumerate(files, start=1):
        relative_path = relative_posix_path(image_path, input_dir)
        try:
            width, height = inspect_image(image_path)
            sha256_hex = calculate_sha256(image_path)
            source_id = generate_source_id(scene, sha256_hex)
            records.append(
                SourceRecord(
                    schema_version=SOURCE_SCHEMA_VERSION,
                    source_id=source_id,
                    scene=scene,
                    group_id=source_id,
                    original_filename=image_path.name,
                    original_relative_path=relative_path,
                    original_sha256=sha256_hex,
                    original_width=width,
                    original_height=height,
                    tile_size=tile_size,
                    overlap=overlap,
                    stride=stride,
                    annotator_id=annotator_id,
                    created_at=created_at,
                )
            )
        except SourceInitializationError as exc:
            issues.append(f"{relative_path}\n{exc}")
        except OSError as exc:
            issues.append(f"{relative_path}\n文件读取或 SHA256 计算失败：{exc}")
        finally:
            if progress_callback is not None:
                progress_callback(current, len(files), relative_path)

    if issues:
        raise SourceInitializationError("Source 预检失败", issues)
    return records


def validate_source_records(records: Iterable[SourceRecord]) -> None:
    """Reject duplicate full hashes and SourceID prefix collisions."""
    records_list = list(records)
    by_hash: dict[str, list[SourceRecord]] = {}
    by_source_id: dict[str, list[SourceRecord]] = {}

    for record in records_list:
        by_hash.setdefault(record.original_sha256, []).append(record)
        by_source_id.setdefault(record.source_id, []).append(record)

    issues: list[str] = []
    for sha256_hex, matches in by_hash.items():
        if len(matches) > 1:
            paths = "\n".join(match.original_relative_path for match in matches)
            issues.append(
                "检测到内容完全相同的重复原图：\n\n"
                f"{paths}\n\nSHA256：\n{sha256_hex}"
            )

    for source_id, matches in by_source_id.items():
        distinct_hashes = {match.original_sha256 for match in matches}
        if len(distinct_hashes) > 1:
            details = "\n".join(
                f"{match.original_relative_path}\nSHA256：{match.original_sha256}"
                for match in matches
            )
            issues.append(
                f"检测到 SourceID 前缀碰撞：\n\nSourceID：{source_id}\n\n{details}"
            )

    if issues:
        raise SourceInitializationError("Source 唯一性校验失败", issues)


def atomic_write_json(target: str | Path, data: dict[str, object]) -> None:
    """Atomically write an indented UTF-8 JSON file in its target directory."""
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
            temporary_path.unlink(missing_ok=True)
        raise SourceInitializationError(f"JSON 写入失败：{target_path}\n{exc}") from exc


def write_sources_manifest(target: str | Path, records: Iterable[SourceRecord]) -> None:
    """Atomically write the fixed-schema sources.csv manifest."""
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
            writer = csv.DictWriter(temporary_file, fieldnames=SOURCE_CSV_FIELDS)
            writer.writeheader()
            for record in records:
                row = record.to_dict()
                row.pop("schema_version")
                writer.writerow(row)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target_path)
    except Exception as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise SourceInitializationError(f"CSV 写入失败：{target_path}\n{exc}") from exc


def _ensure_no_existing_source_data(output_root: Path) -> None:
    manifest_path = output_root / "manifests" / "sources.csv"
    sources_root = output_root / "sources"
    has_source_directories = sources_root.is_dir() and any(
        child.is_dir() for child in sources_root.iterdir()
    )
    if manifest_path.exists() or has_source_directories:
        raise SourceInitializationError(
            "当前成果目录已经存在 Source 数据。\n\n"
            "阶段 3 初始化不会自动覆盖、删除或合并已有数据。"
        )


def _create_operation_logger(output_root: Path) -> tuple[logging.Logger, logging.FileHandler]:
    log_path = output_root / "logs" / "operations.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"fod_source_init.{id(output_root)}.{os.getpid()}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)sZ | %(levelname)s | %(message)s")
        )
        handler.formatter.converter = time.gmtime
        logger.addHandler(handler)
        return logger, handler
    except OSError as exc:
        raise SourceInitializationError(f"操作日志创建失败：{log_path}\n{exc}") from exc


def _flush_operation_log(handler: logging.FileHandler) -> None:
    try:
        handler.flush()
        if handler.stream is not None:
            os.fsync(handler.stream.fileno())
    except OSError as exc:
        raise SourceInitializationError(f"操作日志写入失败：{exc}") from exc


def _cleanup_tree(staging_root: Path, output_root: Path) -> list[str]:
    """Best-effort cleanup restricted to this initialization's staging tree."""
    problems: list[str] = []
    try:
        resolved_staging = staging_root.resolve()
        resolved_output = output_root.resolve()
        resolved_staging.relative_to(resolved_output)
        if not resolved_staging.name.startswith(".tmp_source_init_"):
            return [f"拒绝清理非 staging 目录：{resolved_staging}"]
    except (OSError, ValueError) as exc:
        return [f"无法确认 staging 清理范围：{exc}"]

    if not staging_root.exists():
        return problems
    for path in sorted(staging_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            problems.append(f"无法清理 {path}：{exc}")
    try:
        staging_root.rmdir()
    except OSError as exc:
        problems.append(f"无法清理 {staging_root}：{exc}")
    return problems


def _write_staging_data(staging_root: Path, records: list[SourceRecord]) -> None:
    for record in records:
        target = staging_root / "sources" / record.source_id / "source.json"
        atomic_write_json(target, record.to_dict())
    write_sources_manifest(staging_root / "manifests" / "sources.csv", records)


def _commit_staging(
    staging_root: Path, output_root: Path, records: list[SourceRecord]
) -> None:
    sources_root = output_root / "sources"
    manifests_root = output_root / "manifests"
    sources_root_existed = sources_root.exists()
    manifests_root_existed = manifests_root.exists()
    committed_source_dirs: list[Path] = []
    manifest_path = manifests_root / "sources.csv"

    try:
        sources_root.mkdir(parents=True, exist_ok=True)
        manifests_root.mkdir(parents=True, exist_ok=True)
        for record in records:
            staged_source_dir = staging_root / "sources" / record.source_id
            final_source_dir = sources_root / record.source_id
            if final_source_dir.exists():
                raise SourceInitializationError(
                    f"提交时发现 Source 目录已存在，拒绝覆盖：{final_source_dir}"
                )
            os.replace(staged_source_dir, final_source_dir)
            committed_source_dirs.append(final_source_dir)

        if manifest_path.exists():
            raise SourceInitializationError(
                f"提交时发现 Source Manifest 已存在，拒绝覆盖：{manifest_path}"
            )
        os.replace(staging_root / "manifests" / "sources.csv", manifest_path)
    except Exception as exc:
        rollback_problems: list[str] = []
        try:
            manifest_path.unlink(missing_ok=True)
        except OSError as rollback_exc:
            rollback_problems.append(f"无法撤回 {manifest_path}：{rollback_exc}")
        for source_dir in reversed(committed_source_dirs):
            try:
                (source_dir / "source.json").unlink(missing_ok=True)
                source_dir.rmdir()
            except OSError as rollback_exc:
                rollback_problems.append(f"无法撤回 {source_dir}：{rollback_exc}")
        for directory, existed in (
            (sources_root, sources_root_existed),
            (manifests_root, manifests_root_existed),
        ):
            if not existed:
                try:
                    directory.rmdir()
                except OSError:
                    pass
        if isinstance(exc, SourceInitializationError):
            message = str(exc)
        else:
            message = f"Source 数据提交失败：{exc}"
        if rollback_problems:
            message += "\n\n回滚未完全成功：\n" + "\n".join(rollback_problems)
        raise SourceInitializationError(message) from exc


def initialize_sources(
    input_dir: str | Path,
    output_root: str | Path,
    scene: str,
    annotator_id: str,
    tile_size: int,
    overlap: int,
    progress_callback: ProgressCallback | None = None,
) -> SourceInitializationResult:
    """Preflight all images, stage complete Source data, then commit it."""
    if scene not in {"S01", "S02", "S03"}:
        raise SourceInitializationError(f"不支持的场景代码：{scene}")
    normalized_annotator_id = annotator_id.strip()
    if not normalized_annotator_id:
        raise SourceInitializationError("标注人员 ID 不能为空。")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", normalized_annotator_id):
        raise SourceInitializationError(
            "标注人员 ID 只能包含字母、数字、下划线和短横线。"
        )
    stride = calculate_stride(tile_size, overlap)
    resolved_input, resolved_output = validate_path_relationship(input_dir, output_root)
    if not resolved_input.is_dir():
        raise SourceInitializationError(f"原图输入目录不存在或不是文件夹：{resolved_input}")
    if not resolved_output.is_dir():
        raise SourceInitializationError(f"成果输出目录不存在或不是文件夹：{resolved_output}")

    logger, log_handler = _create_operation_logger(resolved_output)
    staging_root: Path | None = None
    try:
        logger.info("Source 初始化开始")
        logger.info("input_dir=%s", resolved_input)
        logger.info("output_dir=%s", resolved_output)
        logger.info("scene=%s", scene)
        logger.info("annotator_id=%s", normalized_annotator_id)
        logger.info("tile_size=%s", tile_size)
        logger.info("overlap=%s", overlap)
        logger.info("stride=%s", stride)

        _ensure_no_existing_source_data(resolved_output)
        image_files = find_image_files(resolved_input)
        logger.info("发现的图片数量=%s", len(image_files))
        if not image_files:
            raise SourceInitializationError(
                "原图输入目录及其子目录中没有找到支持的图片。\n"
                "支持格式：.jpg、.jpeg、.png、.tif、.tiff、.bmp"
            )

        created_at = utc_now_iso8601()
        records = build_source_records(
            image_files=image_files,
            input_dir=resolved_input,
            scene=scene,
            annotator_id=normalized_annotator_id,
            tile_size=tile_size,
            overlap=overlap,
            created_at=created_at,
            progress_callback=progress_callback,
        )
        validate_source_records(records)
        for record in records:
            logger.info(
                "SourceID=%s | original_relative_path=%s",
                record.source_id,
                record.original_relative_path,
            )

        _flush_operation_log(log_handler)
        staging_root = Path(
            tempfile.mkdtemp(prefix=".tmp_source_init_", dir=resolved_output)
        )
        _write_staging_data(staging_root, records)
        _commit_staging(staging_root, resolved_output, records)

        log_warning = None
        try:
            logger.info("Source 初始化成功 | Source 数量=%s", len(records))
            _flush_operation_log(log_handler)
        except SourceInitializationError as exc:
            # Formal data is already committed. Report a warning instead of
            # falsely claiming that Source initialization itself failed.
            log_warning = str(exc)
        return SourceInitializationResult(
            scene=scene,
            source_count=len(records),
            tile_size=tile_size,
            overlap=overlap,
            stride=stride,
            manifest_path=resolved_output / "manifests" / "sources.csv",
            log_warning=log_warning,
        )
    except SourceInitializationError as exc:
        logger.error("Source 初始化失败：%s", exc)
        try:
            _flush_operation_log(log_handler)
        except SourceInitializationError:
            pass
        raise
    except Exception as exc:
        logger.exception("Source 初始化失败（意外错误）：%s", exc)
        try:
            _flush_operation_log(log_handler)
        except SourceInitializationError:
            pass
        raise SourceInitializationError(f"Source 初始化发生意外错误：{exc}") from exc
    finally:
        if staging_root is not None:
            cleanup_problems = _cleanup_tree(staging_root, resolved_output)
            for problem in cleanup_problems:
                logger.error(problem)
        log_handler.close()
        logger.removeHandler(log_handler)

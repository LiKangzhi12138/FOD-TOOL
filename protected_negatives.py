"""重点负样本的唯一持久化清单与严格一致性校验。"""

import csv
import os
import tempfile
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from annotation_model import TileAnnotation
from tile_manager import TileRecord

MANIFEST_PATH = Path("manifests/protected_negatives.csv")
FIELDS = ("scene", "source_id", "tile_id")


class ProtectedNegativeError(ValueError):
    pass


def load_protected_negatives(
    root: str | Path,
    records: Iterable[TileRecord],
    annotations: Mapping[str, TileAnnotation],
    *,
    require_completed: bool = False,
) -> set[str]:
    """旧数据缺清单表示空集，存在的非法记录必须报错以免丢失人工选择。"""
    path = Path(root) / MANIFEST_PATH
    if not path.exists():
        package_path = Path(root) / "package.json"
        if package_path.is_file():
            with package_path.open(encoding="utf-8") as handle:
                package = json.load(handle)
            if isinstance(package, dict) and package.get("schema_version") == "1.1":
                raise ProtectedNegativeError(f"{path}：1.1 Worker 必须保留重点负样本清单")
        return set()
    by_id = {record.tile_id: record for record in records}
    selected = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(FIELDS):
            raise ProtectedNegativeError(f"{path} 清单列必须为 {FIELDS}")
        for row in reader:
            tile_id = row.get("tile_id")
            source_id = row.get("source_id")
            context = f"{path} SourceID={source_id} TileID={tile_id}"
            record = by_id.get(tile_id)
            if record is None or record.source_id != source_id or record.scene != row.get("scene"):
                raise ProtectedNegativeError(f"{context}：Tile 不存在或 Source/scene 不匹配")
            if None in row or tile_id in selected:
                raise ProtectedNegativeError(f"{context}：重复记录或多余列")
            annotation = annotations[tile_id]
            # 仅 Negative 的无目标背景才有保留语义。草稿可以未完成，交付必须完成。
            if annotation.status != "negative" or annotation.objects or not annotation.trainable:
                raise ProtectedNegativeError(f"{context}：必须为 negative、trainable=true、objects=[]")
            if require_completed and not annotation.completed:
                raise ProtectedNegativeError(f"{context}：重点负样本尚未 completed")
            selected.add(tile_id)
    return selected


def write_protected_negatives(
    path: str | Path, records: Iterable[TileRecord], selected: set[str]
) -> None:
    """按 manifest 顺序写唯一记录，BOM UTF-8、CRLF，空集仍保留表头。"""
    path = Path(path)
    records = list(records)
    if not set(selected).issubset({record.tile_id for record in records}):
        raise ProtectedNegativeError("重点负样本包含不存在的 TileID")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", dir=path.parent,
            prefix=".protected_", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\r\n")
            writer.writeheader()
            for record in records:
                if record.tile_id in selected:
                    writer.writerow({field: getattr(record, field) for field in FIELDS})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

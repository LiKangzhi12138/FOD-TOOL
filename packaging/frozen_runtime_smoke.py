"""Frozen-runtime release smoke test (never included in the user ZIP)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from PySide6.QtWidgets import QApplication

from annotation_canvas import AnnotationGraphicsView
from annotation_model import BBoxAnnotation, TileAnnotation
from annotation_storage import AnnotationStorage
from main import MainWindow
from merge_manager import merge_worker_packages
from package_manager import finalize_worker_package
from source_manager import initialize_sources, utc_now_iso8601
from tile_manager import initialize_tiles, load_tile_records


def _check_pillow_codecs(root: Path) -> None:
    for suffix, image_format in (
        (".jpg", "JPEG"),
        (".png", "PNG"),
        (".tif", "TIFF"),
        (".bmp", "BMP"),
    ):
        path = root / f"codec{suffix}"
        Image.new("RGB", (32, 24), (12, 34, 56)).save(path, image_format)
        with Image.open(path) as opened:
            if opened.size != (32, 24):
                raise RuntimeError(f"Unexpected {image_format} dimensions")
            opened.verify()


def _check_qt(root: Path) -> QApplication:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    if not window.windowTitle():
        raise RuntimeError("MainWindow has no title")

    image_path = root / "canvas.jpg"
    Image.new("RGB", (64, 64), (80, 90, 100)).save(
        image_path, "JPEG", quality=95, subsampling=0
    )
    canvas = AnnotationGraphicsView()
    canvas.set_image(image_path, 64, 64)
    annotation = BBoxAnnotation("metal", (1, 2, 12, 14))
    canvas.add_annotation(annotation)
    if canvas.get_annotations() != [annotation]:
        raise RuntimeError("Annotation canvas bbox round-trip failed")
    if canvas.sceneRect().width() != 64 or canvas.sceneRect().height() != 64:
        raise RuntimeError("Annotation canvas Scene coordinates are incorrect")
    canvas.close()
    window.close()
    return app


def _create_completed_worker(
    root: Path,
    annotator: str,
    color: tuple[int, int, int],
) -> dict[str, object]:
    input_dir = root / "input"
    output_root = root / "worker_output"
    input_dir.mkdir(parents=True)
    output_root.mkdir(parents=True)

    original = input_dir / "source.jpg"
    image = Image.new("RGB", (8192, 5460), color)
    image.save(original, "JPEG", quality=95, subsampling=0)
    image.close()

    source_result = initialize_sources(
        input_dir,
        output_root,
        scene="S01",
        annotator_id=annotator,
        tile_size=1024,
        overlap=256,
    )
    if source_result.source_count != 1 or source_result.stride != 768:
        raise RuntimeError("Source initialization result is incorrect")

    tile_result = initialize_tiles(input_dir, output_root)
    if tile_result.source_count != 1 or tile_result.tile_count != 77:
        raise RuntimeError("Default tiling did not produce 77 tiles")

    records = load_tile_records(output_root)
    storage = AnnotationStorage.open_or_initialize(
        output_root, annotator, tile_records=records
    )
    completed_at = utc_now_iso8601()
    first = records[0]
    for index, record in enumerate(records):
        if index == 0:
            annotation = TileAnnotation(
                status="positive",
                objects=[BBoxAnnotation("metal", (10, 20, 30, 42))],
                notes="疑似金属异物，需要复核。",
            ).mark_completed(completed_at)
        else:
            annotation = TileAnnotation(status="negative").mark_completed(
                completed_at
            )
        storage.save_tile_annotation(
            record.tile_id, annotation, "frozen_smoke"
        )
    storage.set_protected_negative(records[1].tile_id, True)
    storage.close()

    reopened = AnnotationStorage.open_or_initialize(
        output_root, annotator, tile_records=records
    )
    restored = reopened.get_tile_annotation(first.tile_id)
    if reopened.protected_negatives != {records[1].tile_id}:
        raise RuntimeError("Protected Negative did not survive storage reload")
    if (
        restored.status != "positive"
        or len(restored.objects) != 1
        or restored.notes != "疑似金属异物，需要复核。"
        or not restored.completed
    ):
        raise RuntimeError("Annotation autosave/reload did not restore data")
    reopened.close()

    package_result = finalize_worker_package(
        input_dir, output_root, annotator_id=annotator
    )
    if not package_result.package_path.is_file():
        raise RuntimeError("Worker package finalization did not create package.json")
    return {
        "input_dir": input_dir,
        "output_root": output_root,
        "source_count": source_result.source_count,
        "tile_count": tile_result.tile_count,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fod_frozen_smoke_") as temporary:
        root = Path(temporary)
        _check_pillow_codecs(root)
        app = _check_qt(root)
        first = _create_completed_worker(root / "worker_a", "A01", (10, 20, 30))
        second = _create_completed_worker(root / "worker_b", "A02", (90, 80, 70))
        master_root = root / "master_dataset"
        merge_result = merge_worker_packages(
            [first["output_root"], second["output_root"]], master_root
        )
        if merge_result.source_count != 2 or merge_result.tile_count != 154:
            raise RuntimeError("Merged Master Dataset counts are incorrect")
        if not (master_root / "master.json").is_file():
            raise RuntimeError("Merge did not create master.json")
        if not (master_root / "merge_report.json").is_file():
            raise RuntimeError("Merge did not create merge_report.json")
        master = json.loads((master_root / "master.json").read_text(encoding="utf-8"))
        if master["protected_negatives"]["count"] != 2:
            raise RuntimeError("Merge lost Protected Negative records")
        app.processEvents()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "pillow_formats": ["JPEG", "PNG", "TIFF", "BMP"],
                    "source_scan": True,
                    "tiles_per_source": 77,
                    "annotation_reload": True,
                    "finalize": True,
                    "merge_sources": merge_result.source_count,
                    "merge_tiles": merge_result.tile_count,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

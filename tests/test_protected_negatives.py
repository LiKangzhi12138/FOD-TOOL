import csv
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from annotation_model import BBoxAnnotation
from annotation_storage import AnnotationStorage, AnnotationStorageError
from main import MainWindow
from merge_manager import MergeValidationError, merge_worker_packages
from package_manager import (
    PackageValidationError, finalize_worker_package, load_finalized_worker_package,
)
from protected_negatives import (
    FIELDS, MANIFEST_PATH,
    write_protected_negatives,
)
from source_manager import calculate_sha256
from tile_manager import load_tile_records


def make_negative(worker_factory, root, **kwargs):
    return worker_factory(root, status="negative", with_bbox=False, **kwargs)


def protect(project):
    storage = AnnotationStorage.open_or_initialize(project["output_root"], "A01")
    storage.set_protected_negative(project["tile_id"], True)
    storage.close()


def fail_replace(*args):
    raise OSError("disk")


def test_mark_unmark_restart_and_preserve_annotations(tmp_path, worker_factory):
    project = make_negative(worker_factory, tmp_path)
    original = project["annotation_path"].read_bytes()
    protect(project)
    storage = AnnotationStorage.open_or_initialize(project["output_root"], "A01")
    assert storage.protected_negatives == {project["tile_id"]}
    storage.set_protected_negative(project["tile_id"], False)
    storage.close()
    reopened = AnnotationStorage.open_or_initialize(project["output_root"], "A01")
    assert reopened.protected_negatives == set()
    assert original == project["annotation_path"].read_bytes()
    reopened.close()


@pytest.mark.parametrize("status", ["exclude", "uncertain", None, "positive"])
def test_status_transition_clears_protection(tmp_path, worker_factory, status):
    project = make_negative(worker_factory, tmp_path)
    protect(project)
    storage = AnnotationStorage.open_or_initialize(project["output_root"], "A01")
    annotation = storage.get_tile_annotation(project["tile_id"])
    if status == "positive":
        # 原有状态规则要求先清除 Negative，才允许添加检测框。
        annotation = annotation.with_status(None).with_objects([
            BBoxAnnotation("metal", (1, 1, 10, 10))
        ])
    storage.save_tile_annotation(project["tile_id"], annotation.with_status(status), "status")
    assert not storage.protected_negatives
    storage.close()
    reopened = AnnotationStorage.open_or_initialize(project["output_root"], "A01")
    assert not reopened.protected_negatives
    reopened.close()


def test_positive_cannot_be_protected(tmp_path, worker_factory):
    project = worker_factory(tmp_path)
    storage = AnnotationStorage.open_or_initialize(project["output_root"], "A01")
    with pytest.raises(AnnotationStorageError, match="Negative"):
        storage.set_protected_negative(project["tile_id"], True)
    storage.close()


def test_legacy_annotations_and_package(tmp_path, worker_factory):
    project = make_negative(worker_factory, tmp_path)
    storage = AnnotationStorage.open_or_initialize(project["output_root"], "A01")
    assert not storage.protected_negatives
    storage.close()
    finalize_worker_package(project["input_dir"], project["output_root"])
    package_path = project["output_root"] / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    assert package["schema_version"] == "1.0"
    # 与历史工具版本一致的声明仍可加载，指纹算法保持 1.0。
    package["tool_version"] = "0.1.0"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    assert not load_finalized_worker_package(project["output_root"]).protected_negatives


@pytest.mark.parametrize("tamper", ["remove", "empty", "unknown", "duplicate", "wrong_source", "wrong_scene"])
def test_finalized_package_detects_tampering(tmp_path, worker_factory, tamper):
    project = make_negative(worker_factory, tmp_path)
    protect(project)
    finalize_worker_package(project["input_dir"], project["output_root"])
    path = project["output_root"] / MANIFEST_PATH
    if tamper == "remove":
        path.unlink()
    else:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if tamper == "empty":
            rows = []
        elif tamper == "duplicate":
            rows *= 2
        else:
            field = {"unknown": "tile_id", "wrong_source": "source_id", "wrong_scene": "scene"}[tamper]
            rows[0][field] = "invalid"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    with pytest.raises(PackageValidationError):
        load_finalized_worker_package(project["output_root"])
    with pytest.raises(MergeValidationError):
        merge_worker_packages([project["output_root"]], tmp_path / "master")


@pytest.mark.parametrize("invalid", ["positive", "unknown", "duplicate", "source", "scene", "incomplete"])
def test_finalize_rejects_invalid_records(tmp_path, worker_factory, invalid):
    project = make_negative(worker_factory, tmp_path)
    records = load_tile_records(project["output_root"])
    path = project["output_root"] / MANIFEST_PATH
    write_protected_negatives(path, records, {project["tile_id"]})
    if invalid in {"positive", "incomplete"}:
        data = json.loads(project["annotation_path"].read_text(encoding="utf-8"))
        tile = data["tiles"][0]
        if invalid == "positive":
            tile["status"] = "positive"
            tile["objects"] = [{"class_name": "metal", "bbox": [1, 1, 10, 10]}]
        else:
            tile["completed"] = False
            tile["completed_at"] = None
        project["annotation_path"].write_text(json.dumps(data), encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8-sig")
        if invalid == "unknown":
            text = text.replace(project["tile_id"], "missing_tile")
        elif invalid == "duplicate":
            text += text.splitlines()[1] + "\n"
        elif invalid == "source":
            text = text.replace("," + project["source_id"] + ",", ",bad_source,")
        else:
            text = text.replace("S01,", "S02,")
        path.write_text(text, encoding="utf-8-sig")
    with pytest.raises(PackageValidationError):
        finalize_worker_package(project["input_dir"], project["output_root"])
    assert not (project["output_root"] / "package.json").exists()


def test_merge_multiple_workers_and_master_hash(tmp_path, worker_factory):
    a = make_negative(worker_factory, tmp_path / "a", color=(1, 2, 3))
    b = make_negative(worker_factory, tmp_path / "b", color=(9, 8, 7))
    for project in (a, b):
        protect(project)
        finalize_worker_package(project["input_dir"], project["output_root"])
        snapshot = load_finalized_worker_package(project["output_root"])
        assert snapshot.package_data["schema_version"] == "1.1"
        assert snapshot.protected_negatives == {project["tile_id"]}
    master = tmp_path / "master"
    merge_worker_packages([a["output_root"], b["output_root"]], master)
    with (master / MANIFEST_PATH).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["tile_id"] for row in rows} == {a["tile_id"], b["tile_id"]}
    data = json.loads((master / "master.json").read_text(encoding="utf-8"))
    assert data["protected_negatives"]["sha256"] == calculate_sha256(master / MANIFEST_PATH)
    assert data["protected_negatives"]["count"] == 2
    for project in (a, b):
        relative = project["annotation_path"].relative_to(project["output_root"])
        assert (master / relative).read_bytes() == project["annotation_path"].read_bytes()


def test_empty_manifest_and_legacy_merge(tmp_path, worker_factory):
    a = worker_factory(tmp_path / "a")
    b = make_negative(worker_factory, tmp_path / "b", color=(9, 8, 7))
    records = load_tile_records(b["output_root"])
    write_protected_negatives(b["output_root"] / MANIFEST_PATH, records, set())
    for project in (a, b):
        finalize_worker_package(project["input_dir"], project["output_root"])
    master = tmp_path / "master"
    merge_worker_packages([a["output_root"], b["output_root"]], master)
    assert (master / MANIFEST_PATH).read_bytes() == b"\xef\xbb\xbfscene,source_id,tile_id\r\n"


def test_save_failure_retains_dirty_and_flush_retries(tmp_path, worker_factory, monkeypatch):
    project = make_negative(worker_factory, tmp_path)
    storage = AnnotationStorage.open_or_initialize(project["output_root"], "A01")
    with monkeypatch.context() as patch:
        patch.setattr("protected_negatives.os.replace", fail_replace)
        with pytest.raises(AnnotationStorageError):
            storage.set_protected_negative(project["tile_id"], True)
        assert storage.has_dirty_data
    storage.flush_all()
    assert not storage.has_dirty_data
    assert storage.protected_negatives == {project["tile_id"]}
    storage.close()


def test_checkbox_navigation_restart_and_status(tmp_path):
    from test_annotation_canvas import make_persistent_project

    app = QApplication.instance() or QApplication([])
    root, records = make_persistent_project(tmp_path)
    def open_window():
        window = MainWindow()
        window.annotator_edit.setText("A01")
        window._browser_output_root = root.resolve()
        window.open_tile_browser()
        return window
    window = open_window()
    assert not window.protected_negative_checkbox.isEnabled()
    window.set_current_tile_status("negative")
    assert window.protected_negative_checkbox.isEnabled()
    window.protected_negative_checkbox.setChecked(True)
    assert window._display_tile(1)
    assert not window.protected_negative_checkbox.isChecked()
    assert window._display_tile(0)
    assert window.protected_negative_checkbox.isChecked()
    window.close()
    reopened = open_window()
    assert reopened.protected_negative_checkbox.isChecked()
    reopened.set_current_tile_status("uncertain")
    assert not reopened.protected_negative_checkbox.isEnabled()
    assert not reopened.protected_negative_checkbox.isChecked()
    reopened.close()
    app.processEvents()


def test_refinalize_cannot_downgrade_missing_manifest(tmp_path, worker_factory):
    project = make_negative(worker_factory, tmp_path)
    protect(project)
    finalize_worker_package(project["input_dir"], project["output_root"])
    package = (project["output_root"] / "package.json").read_bytes()
    (project["output_root"] / MANIFEST_PATH).unlink()
    with pytest.raises(PackageValidationError, match="丢失"):
        finalize_worker_package(project["input_dir"], project["output_root"])
    assert (project["output_root"] / "package.json").read_bytes() == package


def test_legacy_package_cannot_silently_carry_new_manifest(tmp_path, worker_factory):
    project = make_negative(worker_factory, tmp_path)
    finalize_worker_package(project["input_dir"], project["output_root"])
    protect(project)
    with pytest.raises(PackageValidationError, match="未声明"):
        load_finalized_worker_package(project["output_root"])


@pytest.mark.parametrize("status", ["positive", "exclude", "uncertain", None])
def test_checkbox_disabled_for_other_statuses(tmp_path, worker_factory, status):
    project = worker_factory(
        tmp_path, status=status, with_bbox=status == "positive", completed=status is not None
    )
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.annotator_edit.setText("A01")
    window._browser_output_root = project["output_root"]
    window.open_tile_browser()
    assert not window.protected_negative_checkbox.isEnabled()
    assert not window.protected_negative_checkbox.isChecked()
    window.close()
    app.processEvents()


def test_marker_save_failure_blocks_gui_navigation(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from test_annotation_canvas import make_persistent_project

    app = QApplication.instance() or QApplication([])
    root, _ = make_persistent_project(tmp_path)
    window = MainWindow()
    window.annotator_edit.setText("A01")
    window._browser_output_root = root
    window.open_tile_browser()
    window.set_current_tile_status("negative")
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    with monkeypatch.context() as patch:
        patch.setattr("protected_negatives.os.replace", fail_replace)
        window.protected_negative_checkbox.setChecked(True)
        assert window._annotation_storage.has_dirty_data
        assert not window._display_tile(1)
        assert window._current_tile_index == 0
        assert "保存失败" in window.autosave_status_label.text()
    assert window.flush_pending_changes()
    assert window._display_tile(1)
    window.close()
    app.processEvents()


def test_missing_finalized_manifest_rejected_on_open(tmp_path, worker_factory):
    project = make_negative(worker_factory, tmp_path)
    protect(project)
    finalize_worker_package(project["input_dir"], project["output_root"])
    (project["output_root"] / MANIFEST_PATH).unlink()
    with pytest.raises(AnnotationStorageError, match="重点负样本清单"):
        AnnotationStorage.open_or_initialize(project["output_root"], "A01")


def test_uncompleted_negative_draft_retains_marker_until_completion(tmp_path, worker_factory):
    project = make_negative(worker_factory, tmp_path, completed=False)
    protect(project)
    storage = AnnotationStorage.open_or_initialize(project["output_root"], "A01")
    annotation = storage.get_tile_annotation(project["tile_id"])
    assert not annotation.completed
    assert storage.protected_negatives == {project["tile_id"]}
    from source_manager import utc_now_iso8601
    storage.save_tile_annotation(
        project["tile_id"], annotation.mark_completed(utc_now_iso8601()), "completed"
    )
    storage.close()
    finalize_worker_package(project["input_dir"], project["output_root"])
    assert load_finalized_worker_package(project["output_root"]).protected_negatives == {project["tile_id"]}

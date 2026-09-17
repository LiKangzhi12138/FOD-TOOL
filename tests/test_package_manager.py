import csv
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from package_manager import (
    PackageValidationError,
    _commit_finalize_files,
    compute_content_fingerprint,
    finalize_worker_package,
    load_finalized_worker_package,
)
from source_manager import atomic_write_json


def read_json(path):
    with path.open("r", encoding="utf-8") as file_handle:
        return json.load(file_handle)


def write_json(path, data):
    atomic_write_json(path, data)


def test_finalize_valid_worker_builds_package_statistics_and_canonical_manifest(
    tmp_path, worker_factory
):
    project = worker_factory(tmp_path / "worker")
    result = finalize_worker_package(project["input_dir"], project["output_root"])

    package = read_json(result.package_path)
    assert package["schema_version"] == "1.0"
    assert package["package_type"] == "worker"
    assert package["tool_version"] == "0.1.0"
    assert package["source_count"] == 1
    assert package["tile_count"] == 1
    assert package["annotation_statistics"] == {
        "positive": 1,
        "negative": 0,
        "exclude": 0,
        "uncertain": 0,
        "completed": 1,
        "trainable": 1,
        "object_count": 1,
        "objects_by_class": {
            "metal": 1,
            "stone": 0,
            "rubber": 0,
            "other": 0,
        },
    }
    with (project["output_root"] / "manifests" / "tiles.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as file_handle:
        row = next(csv.DictReader(file_handle))
    assert row["status"] == "positive"
    assert row["trainable"] == "true"
    assert row["object_count"] == "1"
    assert row["annotator"] == "A01"
    assert result.validation_report_path.is_file()
    snapshot = load_finalized_worker_package(project["output_root"])
    assert len(snapshot.sources) == 1
    assert len(snapshot.tiles) == 1


@pytest.mark.parametrize(
    ("status", "completed", "with_bbox"),
    [
        ("positive", False, True),
        (None, False, False),
    ],
)
def test_finalize_rejects_incomplete_or_unannotated_tile(
    tmp_path, worker_factory, status, completed, with_bbox
):
    project = worker_factory(
        tmp_path / "worker",
        status=status,
        completed=completed,
        with_bbox=with_bbox,
    )
    with pytest.raises(PackageValidationError) as caught:
        finalize_worker_package(project["input_dir"], project["output_root"])
    assert any(issue.code == "INCOMPLETE_TILE" for issue in caught.value.issues)
    assert caught.value.report_path is not None
    assert caught.value.report_path.is_file()
    assert not (project["output_root"] / "package.json").exists()


@pytest.mark.parametrize(
    ("status", "objects"),
    [
        ("positive", []),
        ("negative", [{"class_name": "metal", "bbox": [1, 1, 5, 5]}]),
        ("exclude", [{"class_name": "metal", "bbox": [1, 1, 5, 5]}]),
    ],
)
def test_finalize_rejects_status_object_inconsistency(
    tmp_path, worker_factory, status, objects
):
    project = worker_factory(tmp_path / "worker")
    data = read_json(project["annotation_path"])
    tile = data["tiles"][0]
    tile["status"] = status
    tile["trainable"] = status in {"positive", "negative"}
    tile["objects"] = objects
    write_json(project["annotation_path"], data)
    with pytest.raises(PackageValidationError) as caught:
        finalize_worker_package(project["input_dir"], project["output_root"])
    assert any(issue.code == "ANNOTATION_INCONSISTENT" for issue in caught.value.issues)


def test_finalize_rejects_unknown_class(tmp_path, worker_factory):
    project = worker_factory(tmp_path / "worker")
    data = read_json(project["annotation_path"])
    data["tiles"][0]["objects"][0]["class_name"] = "unknown"
    write_json(project["annotation_path"], data)
    with pytest.raises(PackageValidationError) as caught:
        finalize_worker_package(project["input_dir"], project["output_root"])
    assert any(issue.code == "UNKNOWN_CLASS" for issue in caught.value.issues)
    assert "非法 bbox 类别" in str(caught.value)


def test_finalize_rejects_missing_tile_image(tmp_path, worker_factory):
    project = worker_factory(tmp_path / "worker")
    project["tile_path"].unlink()
    with pytest.raises(PackageValidationError) as caught:
        finalize_worker_package(project["input_dir"], project["output_root"])
    assert any(issue.code == "MISSING_FILE" for issue in caught.value.issues)


def test_finalize_rejects_wrong_tile_dimensions(tmp_path, worker_factory):
    project = worker_factory(tmp_path / "worker")
    Image.new("RGB", (32, 64), (1, 2, 3)).save(project["tile_path"], "JPEG")
    with pytest.raises(PackageValidationError) as caught:
        finalize_worker_package(project["input_dir"], project["output_root"])
    assert any(issue.code == "CORRUPT_IMAGE" for issue in caught.value.issues)


def test_finalize_rejects_changed_original(tmp_path, worker_factory):
    project = worker_factory(tmp_path / "worker")
    Image.new("RGB", (64, 64), (200, 1, 1)).save(project["original_path"], "JPEG")
    with pytest.raises(PackageValidationError) as caught:
        finalize_worker_package(project["input_dir"], project["output_root"])
    assert any(issue.code == "SOURCE_ORIGINAL_MISMATCH" for issue in caught.value.issues)


def test_finalize_rejects_source_id_sha_mismatch(tmp_path, worker_factory):
    project = worker_factory(tmp_path / "worker")
    source_path = (
        project["output_root"] / "sources" / project["source_id"] / "source.json"
    )
    data = read_json(source_path)
    data["original_sha256"] = "0" * 64
    write_json(source_path, data)
    with pytest.raises(PackageValidationError) as caught:
        finalize_worker_package(project["input_dir"], project["output_root"])
    assert any(issue.code == "MANIFEST_MISMATCH" for issue in caught.value.issues)


def test_content_fingerprint_is_stable_and_changes_with_annotations(
    tmp_path, worker_factory
):
    project = worker_factory(tmp_path / "worker")
    result = finalize_worker_package(project["input_dir"], project["output_root"])
    first = compute_content_fingerprint(project["output_root"])
    second = compute_content_fingerprint(project["output_root"])
    assert first == second == result.content_fingerprint

    data = read_json(project["annotation_path"])
    data["tiles"][0]["notes"] = "Finalize 后修改"
    write_json(project["annotation_path"], data)
    assert compute_content_fingerprint(project["output_root"]) != first
    with pytest.raises(PackageValidationError) as caught:
        load_finalized_worker_package(project["output_root"])
    assert any(
        issue.code == "PACKAGE_FINGERPRINT_MISMATCH"
        for issue in caught.value.issues
    )


def test_explicit_refinalize_updates_fingerprint_and_preserves_created_at(
    tmp_path, worker_factory
):
    project = worker_factory(tmp_path / "worker")
    first = finalize_worker_package(project["input_dir"], project["output_root"])
    first_package = read_json(first.package_path)
    annotation = read_json(project["annotation_path"])
    annotation["tiles"][0]["notes"] = "重新封装"
    write_json(project["annotation_path"], annotation)

    second = finalize_worker_package(project["input_dir"], project["output_root"])
    second_package = read_json(second.package_path)
    assert second_package["created_at"] == first_package["created_at"]
    assert second_package["content_fingerprint"] != first_package["content_fingerprint"]
    assert load_finalized_worker_package(project["output_root"]).content_fingerprint == second.content_fingerprint


def test_finalize_commit_failure_restores_existing_files(tmp_path, monkeypatch):
    root = tmp_path / "worker"
    staging = root / ".tmp_package_finalize_test"
    report_relative = Path("logs") / "package_validation_test.json"
    targets = [
        (staging / "manifests" / "sources.csv", root / "manifests" / "sources.csv"),
        (staging / "manifests" / "tiles.csv", root / "manifests" / "tiles.csv"),
        (staging / "package.json", root / "package.json"),
        (staging / report_relative, root / report_relative),
    ]
    for staged, target in targets:
        staged.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("new", encoding="utf-8")
        target.write_text("old", encoding="utf-8")

    original_replace = os.replace
    commit_calls = 0

    def fail_second_commit(source, target):
        nonlocal commit_calls
        source_path = Path(source)
        if staging in source_path.parents and "backups" not in source_path.parts:
            commit_calls += 1
            if commit_calls == 2:
                raise OSError("simulated commit failure")
        return original_replace(source, target)

    monkeypatch.setattr("package_manager.os.replace", fail_second_commit)
    with pytest.raises(PackageValidationError):
        _commit_finalize_files(staging, root, report_relative)
    for _staged, target in targets:
        assert target.read_text(encoding="utf-8") == "old"

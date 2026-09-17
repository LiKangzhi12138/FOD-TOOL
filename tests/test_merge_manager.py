import csv
import json
import shutil

import pytest

import merge_manager
from merge_manager import MergeValidationError, merge_worker_packages
from package_manager import finalize_worker_package
from source_manager import atomic_write_json


def read_json(path):
    with path.open("r", encoding="utf-8") as file_handle:
        return json.load(file_handle)


def write_json(path, data):
    atomic_write_json(path, data)


def finalized_worker(base, worker_factory, **kwargs):
    project = worker_factory(base, **kwargs)
    finalize_worker_package(project["input_dir"], project["output_root"])
    return project


def test_merge_two_valid_workers_builds_master_and_preserves_provenance(
    tmp_path, worker_factory
):
    first = finalized_worker(
        tmp_path / "a", worker_factory, annotator="A01", color=(10, 20, 30)
    )
    second = finalized_worker(
        tmp_path / "b", worker_factory, annotator="A02", color=(90, 80, 70)
    )
    target = tmp_path / "master_dataset"
    result = merge_worker_packages(
        [first["output_root"], second["output_root"]], target
    )

    assert result.source_count == 2
    assert result.tile_count == 2
    assert (target / "master.json").is_file()
    assert (target / "merge_report.json").is_file()
    assert (target / "manifests" / "sources.csv").is_file()
    assert (target / "manifests" / "tiles.csv").is_file()
    assert (target / "logs" / "merge.log").is_file()
    master = read_json(target / "master.json")
    assert master["worker_package_count"] == 2
    assert {item["annotator_id"] for item in master["worker_packages"]} == {
        "A01",
        "A02",
    }
    copied = read_json(
        target / "sources" / first["source_id"] / "annotations.json"
    )
    assert copied["created_by"] == "A01"
    assert copied["tiles"][0]["annotator"] == "A01"
    with (target / "manifests" / "tiles.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as file_handle:
        rows = list(csv.DictReader(file_handle))
    assert len(rows) == 2
    assert [row["source_id"] for row in rows] == sorted(
        [first["source_id"], second["source_id"]]
    )


def test_merge_rejects_duplicate_source_and_tile_ids(tmp_path, worker_factory):
    first = finalized_worker(tmp_path / "a", worker_factory)
    duplicate_root = tmp_path / "b" / "worker_output"
    shutil.copytree(first["output_root"], duplicate_root)
    target = tmp_path / "master_dataset"
    with pytest.raises(MergeValidationError) as caught:
        merge_worker_packages([first["output_root"], duplicate_root], target)
    codes = {issue.code for issue in caught.value.issues}
    assert "DUPLICATE_SOURCE_ID" in codes
    assert "DUPLICATE_TILE_ID" in codes
    assert not target.exists()
    assert caught.value.report_path.is_file()
    assert read_json(caught.value.report_path)["status"] == "failed"


def test_merge_rejects_duplicate_original_sha_with_different_source_ids(
    tmp_path, worker_factory
):
    first = finalized_worker(
        tmp_path / "a", worker_factory, annotator="A01", scene="S01", color=(1, 2, 3)
    )
    second = finalized_worker(
        tmp_path / "b", worker_factory, annotator="A02", scene="S02", color=(1, 2, 3)
    )
    with pytest.raises(MergeValidationError) as caught:
        merge_worker_packages(
            [first["output_root"], second["output_root"]],
            tmp_path / "master_dataset",
        )
    assert any(
        issue.code == "DUPLICATE_SOURCE_SHA256" for issue in caught.value.issues
    )


def test_merge_rejects_package_schema_mismatch(tmp_path, worker_factory):
    first = finalized_worker(tmp_path / "a", worker_factory)
    package_path = first["output_root"] / "package.json"
    package = read_json(package_path)
    package["schema_version"] = "2.0"
    write_json(package_path, package)
    with pytest.raises(MergeValidationError) as caught:
        merge_worker_packages([first["output_root"]], tmp_path / "master_dataset")
    assert any(
        issue.code == "PACKAGE_SCHEMA_MISMATCH" for issue in caught.value.issues
    )


def test_merge_rejects_fingerprint_mismatch(tmp_path, worker_factory):
    first = finalized_worker(tmp_path / "a", worker_factory)
    annotation = read_json(first["annotation_path"])
    annotation["tiles"][0]["notes"] = "Finalize 后修改"
    write_json(first["annotation_path"], annotation)
    with pytest.raises(MergeValidationError) as caught:
        merge_worker_packages([first["output_root"]], tmp_path / "master_dataset")
    assert any(
        issue.code == "PACKAGE_FINGERPRINT_MISMATCH"
        for issue in caught.value.issues
    )


def test_merge_rejects_package_statistics_mismatch(tmp_path, worker_factory):
    first = finalized_worker(tmp_path / "a", worker_factory)
    package_path = first["output_root"] / "package.json"
    package = read_json(package_path)
    package["source_count"] = 99
    write_json(package_path, package)
    with pytest.raises(MergeValidationError) as caught:
        merge_worker_packages([first["output_root"]], tmp_path / "master_dataset")
    assert any(
        issue.code == "PACKAGE_STATISTICS_MISMATCH"
        for issue in caught.value.issues
    )


def test_merge_rejects_missing_tile_image(tmp_path, worker_factory):
    first = finalized_worker(tmp_path / "a", worker_factory)
    first["tile_path"].unlink()
    with pytest.raises(MergeValidationError) as caught:
        merge_worker_packages([first["output_root"]], tmp_path / "master_dataset")
    assert any(issue.code == "MISSING_FILE" for issue in caught.value.issues)


def test_merge_rejects_nonempty_output_directory(tmp_path, worker_factory):
    first = finalized_worker(tmp_path / "a", worker_factory)
    target = tmp_path / "master_dataset"
    target.mkdir()
    (target / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(MergeValidationError) as caught:
        merge_worker_packages([first["output_root"]], target)
    assert any(issue.code == "OUTPUT_NOT_EMPTY" for issue in caught.value.issues)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "do not overwrite"


def test_merge_copy_failure_leaves_no_master_and_writes_failure_report(
    tmp_path, worker_factory, monkeypatch
):
    first = finalized_worker(tmp_path / "a", worker_factory)
    target = tmp_path / "master_dataset"

    def fail_copy(*_args, **_kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(merge_manager.shutil, "copy2", fail_copy)
    with pytest.raises(MergeValidationError) as caught:
        merge_worker_packages([first["output_root"]], target)
    assert not target.exists()
    assert caught.value.report_path.is_file()
    report = read_json(caught.value.report_path)
    assert report["status"] == "failed"
    assert any(item["code"] == "MERGE_BUILD_FAILED" for item in report["errors"])

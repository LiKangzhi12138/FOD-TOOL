import csv
import json
import shutil
from dataclasses import replace

import pytest

from annotation_storage import AnnotationStorage
from merge_manager import (
    MergeValidationError, _first_wins, merge_worker_packages, validate_master_dataset,
)
from package_manager import finalize_worker_package, load_finalized_worker_package
from package_manager import _source_record
from source_manager import calculate_sha256, atomic_write_json, write_sources_manifest
from tile_manager import validate_stage3_sources, load_tile_records, write_tiles_manifest


def finalize(project):
    finalize_worker_package(project["input_dir"], project["output_root"])
    return project["output_root"]


def hashes(root):
    return {str(p.relative_to(root)): calculate_sha256(p) for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("conflict", ["status", "bbox"])
def test_first_order_conflicts_unique_and_read_only(tmp_path, worker_factory, reverse, conflict):
    a = worker_factory(tmp_path / "a")
    b = worker_factory(tmp_path / "b", annotator="A02",
        status="negative" if conflict == "status" else "positive", with_bbox=conflict != "status")
    if conflict == "bbox":
        raw = json.loads(b["annotation_path"].read_text(encoding="utf-8"))
        raw["tiles"][0]["objects"][0]["bbox"] = [3, 4, 20, 30]
        b["annotation_path"].write_text(json.dumps(raw), encoding="utf-8")
    c = worker_factory(tmp_path / "c", color=(9, 8, 7))
    roots = [finalize(a), finalize(b), finalize(c)]
    before = [hashes(root) for root in roots]
    if reverse:
        roots = [roots[1], roots[0], roots[2]]
    master = tmp_path / "master"
    result = merge_worker_packages(roots, master, first_wins=True)
    assert result.source_count == 2 and result.tile_count == 2
    winner = b if reverse else a
    assert (master / "sources" / a["source_id"] / "annotations.json").read_bytes() == winner["annotation_path"].read_bytes()
    assert (master / "sources" / c["source_id"]).exists()
    data = validate_master_dataset(master)
    assert data["duplicate_resolution"]["skipped_source_record_count"] == 1
    assert data["duplicate_resolution"]["annotation_difference_source_count"] == 1
    assert [hashes(p["output_root"]) for p in (a, b, c)] == before
    rows = list(csv.DictReader((master / "duplicate_resolution_report.csv").open(encoding="utf-8-sig")))
    assert rows[0]["selected_worker"] == str(winner["output_root"].resolve())


def test_protected_union_and_three_duplicates(tmp_path, worker_factory):
    projects = [worker_factory(tmp_path / str(i), status="negative", with_bbox=False) for i in range(3)]
    storage = AnnotationStorage.open_or_initialize(projects[1]["output_root"], "A01")
    storage.set_protected_negative(projects[1]["tile_id"], True)
    storage.close()
    roots = [finalize(p) for p in projects]
    master = tmp_path / "master"
    merge_worker_packages(roots, master, first_wins=True)
    data = validate_master_dataset(master)
    assert data["source_count"] == 1 and data["tile_count"] == 1
    assert data["protected_negatives"]["count"] == 1
    assert data["duplicate_resolution"]["protected_negative_merge_count"] == 1
    assert data["duplicate_resolution"]["skipped_source_record_count"] == 2


def test_conflict_does_not_apply_other_protected_flag(tmp_path, worker_factory):
    a = worker_factory(tmp_path / "a")
    b = worker_factory(tmp_path / "b", status="negative", with_bbox=False)
    storage = AnnotationStorage.open_or_initialize(b["output_root"], "A01")
    storage.set_protected_negative(b["tile_id"], True)
    storage.close()
    master = tmp_path / "master"
    merge_worker_packages([finalize(a), finalize(b)], master, first_wins=True)
    assert validate_master_dataset(master)["protected_negatives"]["count"] == 0


def test_invalid_skipped_worker_still_rejected(tmp_path, worker_factory):
    a = worker_factory(tmp_path / "a")
    b = worker_factory(tmp_path / "b")
    roots = [finalize(a), finalize(b)]
    path = b["output_root"] / "manifests" / "protected_negatives.csv"
    path.write_text("scene,source_id,tile_id\nS01,missing,missing\n", encoding="utf-8-sig")
    with pytest.raises(MergeValidationError):
        merge_worker_packages(roots, tmp_path / "master", first_wins=True)


@pytest.mark.parametrize("field,value", [("original_sha256", "0" * 64), ("tile_size", 32), ("group_id", "different")])
def test_incompatible_identity_rejected(tmp_path, worker_factory, field, value):
    project = worker_factory(tmp_path)
    snapshot = load_finalized_worker_package(finalize(project))
    altered = replace(snapshot, sources=[replace(snapshot.sources[0], **{field: value})])
    with pytest.raises(MergeValidationError):
        _first_wins([snapshot, altered])


def test_strict_mode_and_master_tamper(tmp_path, worker_factory):
    roots = [finalize(worker_factory(tmp_path / str(i))) for i in range(2)]
    with pytest.raises(MergeValidationError):
        merge_worker_packages(roots, tmp_path / "strict")
    master = tmp_path / "master"
    merge_worker_packages(roots, master, first_wins=True)
    (master / "duplicate_resolution_report.csv").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        validate_master_dataset(master)


def test_skipped_duplicate_worker_retains_its_other_source(tmp_path, worker_factory):
    a = worker_factory(tmp_path / "a")
    b = worker_factory(tmp_path / "b")
    c = worker_factory(tmp_path / "c", color=(99, 88, 77))
    b_root = b["output_root"]
    sources = validate_stage3_sources(b_root)
    extra = replace(validate_stage3_sources(c["output_root"])[0],
                    original_filename="extra.jpg", original_relative_path="extra.jpg")
    shutil.copy2(c["original_path"], b["input_dir"] / "extra.jpg")
    target = b_root / "sources" / extra.source_id
    shutil.copytree(c["output_root"] / "sources" / extra.source_id, target)
    atomic_write_json(target / "source.json", _source_record(extra).to_dict())
    write_sources_manifest(b_root / "manifests" / "sources.csv",
                           [_source_record(s) for s in sources + [extra]])
    write_tiles_manifest(b_root / "manifests" / "tiles.csv",
                         load_tile_records(b_root) + load_tile_records(c["output_root"]))
    master = tmp_path / "master"
    merge_worker_packages([finalize(a), finalize(b)], master, first_wins=True)
    data = validate_master_dataset(master)
    assert data["source_count"] == 2 and data["tile_count"] == 2
    assert (master / "sources" / extra.source_id / "annotations.json").read_bytes() == c["annotation_path"].read_bytes()


def test_dialog_passes_explicit_mode(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication, QMessageBox
    import merge_dialog

    app = QApplication.instance() or QApplication([])
    dialog = merge_dialog.MergeDialog()
    modes = []
    def capture(*args, **kwargs):
        modes.append(kwargs["first_wins"])
        raise MergeValidationError("test")
    monkeypatch.setattr(merge_dialog, "merge_worker_packages", capture)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    dialog.package_list.addItem(str(tmp_path / "worker"))
    dialog.output_edit.setText(str(tmp_path / "master"))
    assert dialog.first_wins_checkbox.isChecked()
    dialog.run_merge()
    dialog.first_wins_checkbox.setChecked(False)
    dialog.run_merge()
    assert modes == [True, False]
    dialog.close()
    app.processEvents()

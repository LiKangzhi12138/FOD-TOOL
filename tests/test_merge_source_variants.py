from dataclasses import replace
from types import SimpleNamespace

import pytest

from annotation_model import TileAnnotation
from annotation_storage import AnnotationStorage
from merge_manager import _first_wins, merge_worker_packages, validate_master_dataset, MergeValidationError
from package_manager import load_finalized_worker_package
from tile_manager import build_tile_plan
from test_merge_first_wins import finalize, hashes


@pytest.mark.parametrize("reverse", [False, True])
def test_scene_variants_keep_complete_first_source(tmp_path, worker_factory, reverse):
    a = worker_factory(tmp_path / "a", scene="S01", status="negative", with_bbox=False)
    b = worker_factory(tmp_path / "b", scene="S02", status="negative", with_bbox=False)
    storage = AnnotationStorage.open_or_initialize(b["output_root"], "A01")
    storage.set_protected_negative(b["tile_id"], True)
    storage.close()
    roots = [finalize(a), finalize(b)]
    before = [hashes(root) for root in roots]
    if reverse:
        roots.reverse()
    master = tmp_path / "master"
    merge_worker_packages(roots, master, first_wins=True, resolve_source_variants=True)
    result = validate_master_dataset(master)
    assert result["source_count"] == result["tile_count"] == 1
    assert result["protected_negatives"]["count"] == 1
    winner = b if reverse else a
    assert result["scenes"] == ["S02" if reverse else "S01"]
    assert (master / "sources" / winner["source_id"] / "annotations.json").read_bytes() == winner["annotation_path"].read_bytes()
    assert [hashes(p["output_root"]) for p in (a, b)] == before


@pytest.mark.parametrize("standard_first", [False, True])
def test_standard77_selected_and_only_matching_tiles_union(tmp_path, worker_factory, monkeypatch, standard_first):
    a = worker_factory(tmp_path / "a")
    base = load_finalized_worker_package(finalize(a))
    source = replace(base.sources[0], original_width=8192, original_height=5460,
                     tile_size=1024, overlap=256, stride=768)
    standard = replace(base, sources=[source], tiles=build_tile_plan(source), protected_negatives=set())
    alternative_source = replace(source, overlap=224, stride=800)
    alternative_tiles = build_tile_plan(alternative_source)
    assert len(standard.tiles) == 77 and len(alternative_tiles) == 70
    alternative = replace(base, root=tmp_path / "ys", sources=[alternative_source],
                          tiles=alternative_tiles, protected_negatives={alternative_tiles[0].tile_id, alternative_tiles[1].tile_id})
    snapshots = [standard, alternative] if standard_first else [alternative, standard]
    annotations = {t.tile_id: TileAnnotation(status="negative").mark_completed("2026-11-17T00:00:00Z")
                   for snapshot in snapshots for t in snapshot.tiles}
    monkeypatch.setattr(AnnotationStorage, "load_existing_read_only",
                        lambda *args: SimpleNamespace(annotations=annotations, close=lambda: None))
    selected, rows = _first_wins(snapshots, resolve_source_variants=True)
    kept = [snapshot for snapshot in selected if snapshot.sources]
    assert len(kept) == 1 and kept[0].root == standard.root
    assert kept[0].sources == [source] and len(kept[0].tiles) == 77
    assert kept[0].protected_negatives == {standard.tiles[0].tile_id}
    assert rows[0]["selected_worker"] == str(standard.root)
    assert rows[0]["protected_negative_merge_count"] == 1


def test_variants_do_not_allow_sha_collision(tmp_path, worker_factory):
    project = worker_factory(tmp_path)
    snapshot = load_finalized_worker_package(finalize(project))
    altered = replace(snapshot, sources=[replace(snapshot.sources[0], original_sha256="0" * 64)])
    with pytest.raises(MergeValidationError, match="SHA256"):
        _first_wins([snapshot, altered], resolve_source_variants=True)


def test_unmatched_nonstandard_variants_still_block(tmp_path, worker_factory, monkeypatch):
    project = worker_factory(tmp_path)
    snapshot = load_finalized_worker_package(finalize(project))
    alternative = replace(snapshot, root=tmp_path / "other", sources=[replace(snapshot.sources[0], stride=32)])
    monkeypatch.setattr(AnnotationStorage, "load_existing_read_only",
                        lambda *args: SimpleNamespace(annotations={}, close=lambda: None))
    with pytest.raises(MergeValidationError, match="77"):
        _first_wins([snapshot, alternative], resolve_source_variants=True)


def test_master_preserves_unique_sources_with_distinct_tiling(tmp_path, worker_factory):
    from tile_manager import validate_stage3_sources, initialize_tiles, TileInitializationError
    from package_manager import _source_record
    from source_manager import atomic_write_json, write_sources_manifest

    a = worker_factory(tmp_path / "a")
    b = worker_factory(tmp_path / "b", color=(91, 82, 73))
    root = b["output_root"]
    source = replace(validate_stage3_sources(root)[0], tile_size=32, stride=32)
    atomic_write_json(root / "sources" / source.source_id / "source.json", _source_record(source).to_dict())
    write_sources_manifest(root / "manifests" / "sources.csv", [_source_record(source)])
    b["tile_path"].unlink()
    b["tile_path"].parent.rmdir()
    (root / "manifests" / "tiles.csv").unlink()
    b["annotation_path"].unlink()
    initialize_tiles(b["input_dir"], root)
    storage = AnnotationStorage.open_or_initialize(root, "A01")
    for tile in storage.tile_records:
        storage.save_tile_annotation(tile.tile_id,
            TileAnnotation(status="negative").mark_completed("2026-11-17T00:00:00Z"), "completed")
    storage.close()
    master = tmp_path / "master"
    merge_worker_packages([finalize(a), finalize(b)], master, first_wins=True, resolve_source_variants=True)
    assert validate_master_dataset(master)["tile_count"] == 5
    with pytest.raises(TileInitializationError, match="不一致"):
        validate_stage3_sources(master)

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QApplication, QGridLayout, QMessageBox, QScrollArea

from annotation_canvas import AnnotationGraphicsView
from annotation_storage import AnnotationStorageError
from annotation_model import BBoxAnnotation
from main import MainWindow
from source_manager import SourceRecord, atomic_write_json, write_sources_manifest
from tile_manager import TileRecord, write_tiles_manifest


SOURCE_ID = "S01_A84F39BC21D7F238"


def get_application():
    return QApplication.instance() or QApplication([])


def test_annotation_panel_uses_scrollable_grouped_status_grid():
    app = get_application()
    window = MainWindow()

    assert isinstance(window.annotation_scroll_area, QScrollArea)
    assert window.annotation_scroll_area.widgetResizable()
    assert (
        window.annotation_scroll_area.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert (
        window.annotation_scroll_area.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    assert window.annotation_scroll_area.minimumWidth() >= 300
    assert (
        window.workspace_splitter.widget(0).sizePolicy().horizontalStretch()
        == 1
    )
    assert (
        window.workspace_splitter.widget(1).sizePolicy().horizontalStretch()
        == 0
    )

    status_grid = window.tile_status_group.layout().itemAt(0).layout()
    assert isinstance(status_grid, QGridLayout)
    expected_positions = {
        "positive": (0, 0, 1, 1),
        "negative": (0, 1, 1, 1),
        "exclude": (1, 0, 1, 1),
        "uncertain": (1, 1, 1, 1),
    }
    for status, expected in expected_positions.items():
        button = window.status_buttons[status]
        assert button.minimumHeight() >= 36
        assert status_grid.getItemPosition(status_grid.indexOf(button)) == expected
    assert status_grid.getItemPosition(
        status_grid.indexOf(window.clear_status_button)
    ) == (2, 0, 1, 2)
    assert window.bbox_list.minimumHeight() >= 120
    assert window.notes_edit.minimumHeight() >= 80
    assert window.validate_tile_button.minimumHeight() >= 38

    window.close()
    app.processEvents()


def make_tile_record(output_root, x: int) -> TileRecord:
    tile_id = f"{SOURCE_ID}_X{x:06d}_Y000000"
    relative_path = f"sources/{SOURCE_ID}/tiles/{tile_id}.jpg"
    image_path = output_root.joinpath(*relative_path.split("/"))
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (30, 60, 90)).save(image_path, format="JPEG")
    return TileRecord(
        tile_id=tile_id,
        source_id=SOURCE_ID,
        scene="S01",
        x=x,
        y=0,
        width=64,
        height=64,
        tile_relative_path=relative_path,
        annotator="A01",
    )


def make_persistent_project(tmp_path):
    output_root = tmp_path / "persistent_output"
    records = [
        make_tile_record(output_root, 0),
        make_tile_record(output_root, 64),
    ]
    source = SourceRecord(
        schema_version="1.0",
        source_id=SOURCE_ID,
        scene="S01",
        group_id=SOURCE_ID,
        original_filename="source.jpg",
        original_relative_path="source.jpg",
        original_sha256="a84f39bc21d7f238" + "0" * 48,
        original_width=128,
        original_height=64,
        tile_size=64,
        overlap=0,
        stride=64,
        annotator_id="A01",
        created_at="2026-09-16T06:30:00Z",
    )
    atomic_write_json(
        output_root / "sources" / SOURCE_ID / "source.json", source.to_dict()
    )
    write_sources_manifest(
        output_root / "manifests" / "sources.csv", [source]
    )
    write_tiles_manifest(output_root / "manifests" / "tiles.csv", records)
    return output_root, records


def test_annotation_canvas_add_change_delete_and_pixel_scene(tmp_path):
    app = get_application()
    image_path = tmp_path / "tile.jpg"
    Image.new("RGB", (1024, 1024), (25, 50, 75)).save(
        image_path, format="JPEG"
    )
    canvas = AnnotationGraphicsView()
    canvas.resize(640, 480)
    canvas.set_image(image_path, expected_width=1024, expected_height=1024)

    assert canvas.pixmap_item.pos() == QPointF(0, 0)
    assert canvas.sceneRect().getRect() == (0.0, 0.0, 1024.0, 1024.0)

    original = BBoxAnnotation("metal", (100, 200, 160, 260))
    canvas.add_annotation(original)
    assert canvas.get_annotations() == [original]
    assert canvas.selected_annotation() == original

    canvas.zoom_in()
    assert canvas.get_annotations() == [original]
    assert canvas.change_selected_class("stone")
    assert canvas.get_annotations() == [
        BBoxAnnotation("stone", (100, 200, 160, 260))
    ]
    assert canvas.delete_selected_annotation()
    assert canvas.get_annotations() == []
    canvas.close()
    app.processEvents()


def test_main_window_restores_tile_status_and_objects_in_memory(tmp_path):
    app = get_application()
    output_root = tmp_path / "output"
    records = [
        make_tile_record(output_root, 0),
        make_tile_record(output_root, 64),
        make_tile_record(output_root, 128),
    ]
    window = MainWindow()
    window._browser_output_root = output_root.resolve()
    window._tile_records = records
    window._source_local_positions = [1, 2, 3]
    window._source_tile_counts = {SOURCE_ID: 3}

    assert window._display_tile(0)
    window.image_viewer.add_annotation(BBoxAnnotation("metal", (1, 1, 10, 10)))
    window.image_viewer.add_annotation(BBoxAnnotation("stone", (20, 20, 30, 30)))
    assert window.set_current_tile_status("positive")
    assert window._display_tile(1)
    assert window.set_current_tile_status("negative")
    assert "Negative" in window.current_status_label.text()
    assert window.trainable_label.text() == "Trainable：是"
    assert not window.image_viewer.drawing_enabled
    assert window.validate_current_tile_completion()
    assert "已完成" in window.completion_label.text()

    assert window._display_tile(2)
    window.image_viewer.add_annotation(BBoxAnnotation("rubber", (5, 5, 15, 15)))
    assert window.set_current_tile_status("uncertain")
    assert window._display_tile(0)

    assert window.image_viewer.get_annotations() == [
        BBoxAnnotation("metal", (1, 1, 10, 10)),
        BBoxAnnotation("stone", (20, 20, 30, 30)),
    ]
    assert window._annotation_store[records[0].tile_id].status == "positive"
    assert len(window._annotation_store[records[0].tile_id].objects) == 2
    assert window._annotation_store[records[1].tile_id].status == "negative"
    assert window._annotation_store[records[1].tile_id].objects == []
    assert window._annotation_store[records[2].tile_id].status == "uncertain"
    assert len(window._annotation_store[records[2].tile_id].objects) == 1
    assert window.status_buttons["positive"].isChecked()
    assert window.image_viewer.drawing_enabled
    window.close()
    app.processEvents()


def test_main_window_rejects_invalid_status_and_last_positive_delete(
    tmp_path, monkeypatch
):
    app = get_application()
    output_root = tmp_path / "output"
    record = make_tile_record(output_root, 0)
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    window = MainWindow()
    window._browser_output_root = output_root.resolve()
    window._tile_records = [record]
    window._source_local_positions = [1]
    window._source_tile_counts = {SOURCE_ID: 1}
    assert set(window.status_buttons) == {
        "positive",
        "negative",
        "exclude",
        "uncertain",
    }

    assert window._display_tile(0)
    assert not window.set_current_tile_status("positive")
    assert window._current_tile_annotation().status is None

    bbox = BBoxAnnotation("metal", (1, 1, 10, 10))
    window.image_viewer.add_annotation(bbox)
    assert not window.set_current_tile_status("negative")
    assert window._current_tile_annotation().status is None
    assert window.set_current_tile_status("positive")

    window.delete_selected_bbox()
    assert window.image_viewer.get_annotations() == [bbox]
    assert window._current_tile_annotation().status == "positive"
    assert any("最后一个 bbox" in message for _title, message in warnings)

    window.clear_current_tile_status()
    window.delete_selected_bbox()
    assert window.image_viewer.get_annotations() == []
    assert window._current_tile_annotation().status is None
    window.close()
    app.processEvents()


def test_main_window_persists_and_restores_annotations_after_restart(tmp_path):
    app = get_application()
    output_root, records = make_persistent_project(tmp_path)
    first = MainWindow()
    first.annotator_edit.setText("A01")
    first._browser_output_root = output_root.resolve()
    first.open_tile_browser()

    first.image_viewer.add_annotation(
        BBoxAnnotation("metal", (1, 1, 10, 10))
    )
    first.image_viewer.add_annotation(
        BBoxAnnotation("other", (20, 20, 30, 30))
    )
    assert first.set_current_tile_status("positive")
    first.notes_edit.setPlainText("疑似小金属片，建议复核")
    assert first.validate_current_tile_completion()
    assert first._display_tile(1)
    assert first.set_current_tile_status("negative")
    assert first.validate_current_tile_completion()
    first.close()
    app.processEvents()

    reopened = MainWindow()
    reopened.annotator_edit.setText("A01")
    reopened._browser_output_root = output_root.resolve()
    reopened.open_tile_browser()
    assert reopened._current_tile_index == 0
    assert reopened.image_viewer.get_annotations() == [
        BBoxAnnotation("metal", (1, 1, 10, 10)),
        BBoxAnnotation("other", (20, 20, 30, 30)),
    ]
    assert reopened._current_tile_annotation().status == "positive"
    assert reopened._current_tile_annotation().completed
    assert reopened.notes_edit.toPlainText() == "疑似小金属片，建议复核"

    assert reopened._display_tile(1)
    assert reopened._current_tile_annotation().status == "negative"
    assert reopened._current_tile_annotation().completed
    assert reopened.image_viewer.get_annotations() == []
    assert reopened._tile_records == records
    reopened.close()
    app.processEvents()


def test_save_failure_keeps_dirty_and_blocks_tile_navigation(
    tmp_path, monkeypatch
):
    app = get_application()
    output_root, _records = make_persistent_project(tmp_path)
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, title, message: messages.append((title, message)),
    )
    window = MainWindow()
    window.annotator_edit.setText("A01")
    window._browser_output_root = output_root.resolve()
    window.open_tile_browser()
    window.notes_edit.setPlainText("尚未落盘")
    assert window._annotation_storage.has_dirty_data

    original_save_source = window._annotation_storage.save_source

    def fail_save(_source_id):
        raise AnnotationStorageError("simulated save failure")

    monkeypatch.setattr(window._annotation_storage, "save_source", fail_save)
    window.show_next_tile()
    assert window._current_tile_index == 0
    assert window._annotation_storage.has_dirty_data
    assert window.autosave_status_label.text() == "自动保存：保存失败"
    assert messages

    monkeypatch.setattr(
        window._annotation_storage, "save_source", original_save_source
    )
    window.retry_save()
    assert not window._annotation_storage.has_dirty_data
    assert window.autosave_status_label.text() == "自动保存：已保存"
    window.show_next_tile()
    assert window._current_tile_index == 1
    window.close()
    app.processEvents()

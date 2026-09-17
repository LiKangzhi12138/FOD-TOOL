import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

from image_viewer import ImageGraphicsView, ImageViewerError


def get_application():
    return QApplication.instance() or QApplication([])


def test_image_graphics_view_places_pixels_at_scene_origin(tmp_path):
    app = get_application()
    image_path = tmp_path / "tile.jpg"
    Image.new("RGB", (512, 512), (50, 100, 150)).save(image_path, format="JPEG")
    viewer = ImageGraphicsView()
    viewer.resize(640, 480)

    viewer.set_image(image_path, expected_width=512, expected_height=512)
    app.processEvents()

    assert viewer.has_image
    assert viewer.pixmap_item.pos() == QPointF(0, 0)
    assert viewer.pixmap_item.pixmap().size().width() == 512
    assert viewer.pixmap_item.pixmap().size().height() == 512
    assert viewer.sceneRect().x() == 0
    assert viewer.sceneRect().y() == 0
    assert viewer.sceneRect().width() == 512
    assert viewer.sceneRect().height() == 512
    assert viewer.fit_mode
    viewer.close()


def test_image_graphics_view_zoom_is_bounded_and_100_percent_is_one_to_one(
    tmp_path,
):
    app = get_application()
    image_path = tmp_path / "tile.jpg"
    Image.new("RGB", (128, 128), (20, 40, 60)).save(image_path, format="JPEG")
    viewer = ImageGraphicsView()
    viewer.set_image(image_path, expected_width=128, expected_height=128)

    viewer.reset_to_100_percent()
    assert viewer.transform().m11() == pytest.approx(1.0)
    assert not viewer.fit_mode
    viewer.zoom_in()
    assert viewer.transform().m11() == pytest.approx(1.25)
    for _ in range(100):
        viewer.zoom_in()
    assert viewer.transform().m11() <= viewer.MAX_SCALE
    for _ in range(200):
        viewer.zoom_out()
    assert viewer.transform().m11() >= viewer.MIN_SCALE
    viewer.fit_image()
    assert viewer.fit_mode
    viewer.close()
    app.processEvents()


def test_image_graphics_view_rejects_manifest_size_mismatch(tmp_path):
    app = get_application()
    image_path = tmp_path / "tile.jpg"
    Image.new("RGB", (64, 32), (0, 0, 0)).save(image_path, format="JPEG")
    viewer = ImageGraphicsView()

    try:
        viewer.set_image(image_path, expected_width=64, expected_height=64)
    except ImageViewerError as exc:
        assert "高度与 Manifest 不一致" in str(exc)
    else:
        raise AssertionError("Expected ImageViewerError")
    finally:
        viewer.close()
        app.processEvents()

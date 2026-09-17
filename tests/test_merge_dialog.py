import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from main import MainWindow
from merge_dialog import MergeDialog


def get_application():
    return QApplication.instance() or QApplication([])


def test_finalize_and_merge_gui_entries_construct_offscreen():
    app = get_application()
    window = MainWindow()
    dialog = MergeDialog(window)

    assert window.finalize_package_button.text() == "校验并封装 Worker Package"
    assert window.merge_packages_button.text() == "Merge Worker Packages"
    assert dialog.add_package_button.text() == "添加 Package"
    assert dialog.merge_button.text() == "严格校验并合并"
    assert dialog.package_list.count() == 0

    dialog.close()
    window.close()
    app.processEvents()

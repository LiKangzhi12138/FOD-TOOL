"""PySide6 dialog for strict Worker Package to Master Dataset merge."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from merge_manager import MergeValidationError, merge_worker_packages


class MergeDialog(QDialog):
    """Collect Worker Package roots and run one strict, staging-based merge."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Merge Worker Packages")
        self.resize(760, 600)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel("Worker Packages")
        title_font = title.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        package_buttons = QHBoxLayout()
        self.add_package_button = QPushButton("添加 Package")
        self.remove_package_button = QPushButton("移除选中")
        self.clear_packages_button = QPushButton("清空")
        package_buttons.addWidget(self.add_package_button)
        package_buttons.addWidget(self.remove_package_button)
        package_buttons.addWidget(self.clear_packages_button)
        package_buttons.addStretch()
        layout.addLayout(package_buttons)

        self.package_list = QListWidget()
        self.package_list.setMinimumHeight(180)
        layout.addWidget(self.package_list)

        output_label = QLabel("Master Dataset 输出目录：")
        layout.addWidget(output_label)
        output_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("请选择新的空目录或尚不存在的目标目录")
        self.choose_output_button = QPushButton("选择...")
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.choose_output_button)
        layout.addLayout(output_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_label = QLabel("等待选择 Worker Package")
        self.progress_label.setWordWrap(True)
        self.progress_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)

        self.merge_button = QPushButton("严格校验并合并")
        self.merge_button.setMinimumHeight(42)
        layout.addWidget(self.merge_button)

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.result_label)
        layout.addStretch()

        self.add_package_button.clicked.connect(self.add_package)
        self.remove_package_button.clicked.connect(self.remove_selected_package)
        self.clear_packages_button.clicked.connect(self.package_list.clear)
        self.choose_output_button.clicked.connect(self.choose_output)
        self.merge_button.clicked.connect(self.run_merge)

    def add_package(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择 Worker Package 根目录"
        )
        if not directory:
            return
        resolved = str(Path(directory).resolve())
        existing = {
            self.package_list.item(index).text()
            for index in range(self.package_list.count())
        }
        if resolved not in existing:
            self.package_list.addItem(resolved)

    def remove_selected_package(self) -> None:
        for item in self.package_list.selectedItems():
            self.package_list.takeItem(self.package_list.row(item))

    def choose_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择 Master Dataset 输出目录"
        )
        if directory:
            self.output_edit.setText(directory)

    def _set_processing(self, processing: bool) -> None:
        for widget in (
            self.add_package_button,
            self.remove_package_button,
            self.clear_packages_button,
            self.choose_output_button,
            self.output_edit,
            self.package_list,
            self.merge_button,
        ):
            widget.setEnabled(not processing)

    def _update_progress(
        self,
        package_index: int,
        package_total: int,
        tile_index: int,
        tile_total: int,
        source_id: str,
        tile_id: str,
    ) -> None:
        self.progress_bar.setRange(0, max(tile_total, 1))
        self.progress_bar.setValue(tile_index)
        self.progress_label.setText(
            f"正在验证 Package：{package_index} / {package_total}\n"
            f"Source：{source_id}\n"
            f"Tile：{tile_index} / {tile_total}\n"
            f"TileID：{tile_id}"
        )
        QApplication.processEvents()

    def run_merge(self) -> None:
        package_roots = [
            self.package_list.item(index).text()
            for index in range(self.package_list.count())
        ]
        output_text = self.output_edit.text().strip()
        if not package_roots:
            QMessageBox.warning(self, "无法 Merge", "请至少添加一个 Worker Package。")
            return
        if not output_text:
            QMessageBox.warning(self, "无法 Merge", "请选择 Master Dataset 输出目录。")
            return

        self._set_processing(True)
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("正在执行全量预检，尚未复制任何数据...")
        try:
            result = merge_worker_packages(
                package_roots,
                output_text,
                progress_callback=self._update_progress,
            )
        except MergeValidationError as exc:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Merge 失败，未生成 Master Dataset")
            self.result_label.setText(exc.format_for_gui())
            QMessageBox.critical(self, "Merge 失败", exc.format_for_gui())
        except Exception as exc:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Merge 发生意外错误")
            self.result_label.setText(f"{type(exc).__name__}: {exc}")
            QMessageBox.critical(
                self,
                "Merge 意外错误",
                f"Master Dataset 未生成。\n\n{type(exc).__name__}: {exc}",
            )
        else:
            stats = result.statistics
            scene_lines = "\n".join(
                f"{scene}：{result.scene_source_counts.get(scene, 0)} Sources"
                for scene in result.scenes
            )
            message = (
                "Merge 成功\n\n"
                f"Worker Packages：{result.worker_package_count}\n"
                f"Sources：{result.source_count}\n"
                f"Tiles：{result.tile_count}\n\n"
                f"{scene_lines}\n\n"
                f"Positive：{stats['positive']}\n"
                f"Negative：{stats['negative']}\n"
                f"Exclude：{stats['exclude']}\n"
                f"Uncertain：{stats['uncertain']}\n"
                f"Objects：{stats['object_count']}\n\n"
                f"Master Dataset：\n{result.master_root}\n\n"
                f"Merge Report：\n{result.report_path}"
            )
            self.progress_bar.setRange(0, result.tile_count)
            self.progress_bar.setValue(result.tile_count)
            self.progress_label.setText("Merge 完成")
            self.result_label.setText(message)
            QMessageBox.information(self, "Merge 成功", message)
        finally:
            self._set_processing(False)

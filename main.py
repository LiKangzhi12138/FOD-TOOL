import re
import sys
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QTimer, Qt
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from annotation_canvas import AnnotationGraphicsView
from annotation_model import (
    AnnotationValidationError,
    BBoxAnnotation,
    FOD_CLASSES,
    FOD_CLASS_LABELS,
    TILE_STATUS_LABELS,
    VALID_TILE_STATUSES,
    TileAnnotation,
    derive_trainable,
    drawing_enabled_for_status,
    validate_annotation,
    validate_bbox_deletion,
    validate_tile_annotation,
    validate_tile_completion,
)
from annotation_storage import AnnotationStorage, AnnotationStorageError
from image_viewer import ImageViewerError
from merge_dialog import MergeDialog
from package_manager import PackageValidationError, finalize_worker_package
from source_manager import (
    SourceInitializationError,
    initialize_sources,
    utc_now_iso8601,
    validate_path_relationship,
)
from tile_manager import (
    TileInitializationError,
    TileRecord,
    initialize_tiles,
    load_tile_records,
    resolve_tile_image_path,
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._settings_valid = False
        self._validated_settings: dict[str, object] | None = None
        self._stage3_completed_settings: dict[str, object] | None = None
        self._browser_output_root: Path | None = None
        self._tile_records: list[TileRecord] = []
        self._source_local_positions: list[int] = []
        self._source_tile_counts: dict[str, int] = {}
        self._current_tile_index = -1
        self._annotation_store: dict[str, TileAnnotation] = {}
        self._annotation_store_root: Path | None = None
        self._annotation_storage: AnnotationStorage | None = None
        self._loading_notes = False
        self._notes_save_timer = QTimer(self)
        self._notes_save_timer.setSingleShot(True)
        self._notes_save_timer.setInterval(400)
        self._notes_save_timer.timeout.connect(self._flush_notes_timer)
        self._browser_shortcuts: list[QShortcut] = []
        self.setWindowTitle("机场跑道 FOD 数据标注工具")
        self.resize(1200, 800)
        self.init_ui()

    def init_ui(self):
        title_label = QLabel("FOD Annotation Tool")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = title_label.font()
        title_font.setPointSize(22)
        title_font.setBold(True)
        title_label.setFont(title_font)

        subtitle_label = QLabel("机场跑道 FOD 数据切片、标注与数据集管理工具")
        subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        settings_group = QGroupBox("项目设置")
        form_layout = QFormLayout()

        self.input_dir_edit = QLineEdit()
        self.input_dir_edit.setPlaceholderText("请选择原始图片所在目录")
        self.input_browse_button = QPushButton("选择...")
        self.input_browse_button.clicked.connect(self.select_input_directory)
        input_layout = QHBoxLayout()
        input_layout.addWidget(self.input_dir_edit)
        input_layout.addWidget(self.input_browse_button)
        input_widget = QWidget()
        input_widget.setLayout(input_layout)
        form_layout.addRow("原图输入目录：", input_widget)

        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("请选择标注成果输出根目录")
        self.output_browse_button = QPushButton("选择...")
        self.output_browse_button.clicked.connect(self.select_output_directory)
        output_layout = QHBoxLayout()
        output_layout.addWidget(self.output_dir_edit)
        output_layout.addWidget(self.output_browse_button)
        output_widget = QWidget()
        output_widget.setLayout(output_layout)
        form_layout.addRow("成果输出目录：", output_widget)

        self.scene_combo = QComboBox()
        self.scene_combo.addItem("场景一 S01", "S01")
        self.scene_combo.addItem("场景二 S02", "S02")
        self.scene_combo.addItem("场景三 S03", "S03")
        form_layout.addRow("场景：", self.scene_combo)

        self.annotator_edit = QLineEdit()
        self.annotator_edit.setPlaceholderText("例如：A01")
        form_layout.addRow("标注人员 ID：", self.annotator_edit)

        self.tile_size_spin = QSpinBox()
        self.tile_size_spin.setRange(128, 8192)
        self.tile_size_spin.setSingleStep(128)
        self.tile_size_spin.setValue(1024)
        form_layout.addRow("Tile Size：", self.tile_size_spin)

        self.overlap_spin = QSpinBox()
        self.overlap_spin.setRange(0, 8191)
        self.overlap_spin.setSingleStep(32)
        self.overlap_spin.setValue(256)
        form_layout.addRow("Overlap：", self.overlap_spin)

        self.stride_label = QLabel()
        form_layout.addRow("Stride：", self.stride_label)
        settings_group.setLayout(form_layout)

        self.validate_button = QPushButton("验证项目设置")
        self.validate_button.setMinimumHeight(40)
        self.validate_button.clicked.connect(self.validate_settings)

        self.scan_button = QPushButton("扫描原图并建立 Source 清单")
        self.scan_button.setMinimumHeight(40)
        self.scan_button.setEnabled(False)
        self.scan_button.clicked.connect(self.initialize_source_data)

        self.tile_button = QPushButton("生成切片与 Tile 清单")
        self.tile_button.setMinimumHeight(40)
        self.tile_button.setEnabled(False)
        self.tile_button.clicked.connect(self.initialize_tile_data)

        self.open_browser_button = QPushButton("进入 Tile 浏览器")
        self.open_browser_button.setMinimumHeight(40)
        self.open_browser_button.setEnabled(False)
        self.open_browser_button.clicked.connect(self.open_tile_browser)

        self.finalize_package_button = QPushButton("校验并封装 Worker Package")
        self.finalize_package_button.setMinimumHeight(40)
        self.finalize_package_button.setEnabled(False)
        self.finalize_package_button.clicked.connect(self.finalize_worker_data)

        self.merge_packages_button = QPushButton("Merge Worker Packages")
        self.merge_packages_button.setMinimumHeight(40)
        self.merge_packages_button.clicked.connect(self.open_merge_dialog)

        progress_group = QGroupBox("扫描 / 切片进度")
        progress_layout = QVBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_label = QLabel("等待验证项目设置")
        self.progress_label.setWordWrap(True)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.progress_label)
        progress_group.setLayout(progress_layout)

        result_group = QGroupBox("当前配置 / 处理结果")
        result_layout = QVBoxLayout()
        self.result_label = QLabel("填写项目设置后，点击“验证项目设置”。")
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        result_layout.addWidget(self.result_label)
        result_group.setLayout(result_layout)

        main_layout = QVBoxLayout()
        main_layout.addWidget(title_label)
        main_layout.addWidget(subtitle_label)
        main_layout.addSpacing(20)
        main_layout.addWidget(settings_group)
        main_layout.addSpacing(10)
        main_layout.addWidget(self.validate_button)
        main_layout.addWidget(self.scan_button)
        main_layout.addWidget(self.tile_button)
        main_layout.addWidget(self.open_browser_button)
        main_layout.addWidget(self.finalize_package_button)
        main_layout.addWidget(self.merge_packages_button)
        main_layout.addWidget(progress_group)
        main_layout.addWidget(result_group)
        main_layout.addStretch()

        self.settings_page = QWidget()
        self.settings_page.setLayout(main_layout)
        self.browser_page = self._build_browser_page()
        self.stacked_widget = QStackedWidget()
        self.stacked_widget.addWidget(self.settings_page)
        self.stacked_widget.addWidget(self.browser_page)
        self.stacked_widget.currentChanged.connect(
            self._sync_stacked_page_size_policy
        )
        self.setCentralWidget(self.stacked_widget)
        self._sync_stacked_page_size_policy(self.stacked_widget.currentIndex())

        self._configuration_controls = [
            self.input_dir_edit,
            self.output_dir_edit,
            self.scene_combo,
            self.annotator_edit,
            self.tile_size_spin,
            self.overlap_spin,
            self.input_browse_button,
            self.output_browse_button,
        ]
        self.input_dir_edit.textChanged.connect(self.invalidate_validation)
        self.output_dir_edit.textChanged.connect(self.invalidate_validation)
        self.scene_combo.currentIndexChanged.connect(self.invalidate_validation)
        self.annotator_edit.textChanged.connect(self.invalidate_validation)
        self.tile_size_spin.valueChanged.connect(self.update_stride)
        self.tile_size_spin.valueChanged.connect(self.invalidate_validation)
        self.overlap_spin.valueChanged.connect(self.update_stride)
        self.overlap_spin.valueChanged.connect(self.invalidate_validation)

        self.update_stride()
        self.statusBar().showMessage("阶段 3：请配置并验证 Source 初始化参数")

    def _sync_stacked_page_size_policy(self, current_index: int) -> None:
        """Only let the visible page constrain the main window's minimum size."""
        for index in range(self.stacked_widget.count()):
            policy = (
                QSizePolicy.Policy.Preferred
                if index == current_index
                else QSizePolicy.Policy.Ignored
            )
            self.stacked_widget.widget(index).setSizePolicy(policy, policy)
        self.stacked_widget.updateGeometry()

    def _build_browser_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)

        header_layout = QHBoxLayout()
        title_label = QLabel("Tile 浏览 / bbox 标注工作区")
        title_font = title_label.font()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title_label.setFont(title_font)
        self.return_settings_button = QPushButton("返回项目设置")
        self.return_settings_button.clicked.connect(self.return_to_settings)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(self.return_settings_button)
        page_layout.addLayout(header_layout)

        metadata_group = QGroupBox("当前 Tile")
        metadata_layout = QGridLayout(metadata_group)
        self.browser_tile_id_label = QLabel("-")
        self.browser_source_id_label = QLabel("-")
        self.browser_scene_label = QLabel("-")
        self.browser_coordinates_label = QLabel("-")
        self.browser_size_label = QLabel("-")
        self.browser_source_position_label = QLabel("-")
        self.browser_global_position_label = QLabel("-")
        for label in (
            self.browser_tile_id_label,
            self.browser_source_id_label,
            self.browser_scene_label,
            self.browser_coordinates_label,
            self.browser_size_label,
            self.browser_source_position_label,
            self.browser_global_position_label,
        ):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        metadata_layout.addWidget(QLabel("TileID："), 0, 0)
        metadata_layout.addWidget(self.browser_tile_id_label, 0, 1, 1, 3)
        metadata_layout.addWidget(QLabel("SourceID："), 1, 0)
        metadata_layout.addWidget(self.browser_source_id_label, 1, 1)
        metadata_layout.addWidget(QLabel("Scene："), 1, 2)
        metadata_layout.addWidget(self.browser_scene_label, 1, 3)
        metadata_layout.addWidget(QLabel("原图坐标："), 2, 0)
        metadata_layout.addWidget(self.browser_coordinates_label, 2, 1)
        metadata_layout.addWidget(QLabel("Tile 尺寸："), 2, 2)
        metadata_layout.addWidget(self.browser_size_label, 2, 3)
        metadata_layout.addWidget(QLabel("当前 Source："), 3, 0)
        metadata_layout.addWidget(self.browser_source_position_label, 3, 1)
        metadata_layout.addWidget(QLabel("全部 Tile："), 3, 2)
        metadata_layout.addWidget(self.browser_global_position_label, 3, 3)
        page_layout.addWidget(metadata_group)

        self.image_viewer = AnnotationGraphicsView()
        self.image_viewer.setMinimumSize(420, 240)
        self.image_viewer.annotations_changed.connect(self._on_annotations_changed)
        self.image_viewer.selection_changed.connect(
            self._on_canvas_selection_changed
        )
        self.image_viewer.drawing_blocked.connect(self._on_drawing_blocked)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.addWidget(self.image_viewer)
        self.workspace_splitter.addWidget(self._build_annotation_panel())
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 0)
        self.workspace_splitter.setSizes([840, 360])
        page_layout.addWidget(self.workspace_splitter, 1)

        controls_layout = QHBoxLayout()
        self.previous_tile_button = QPushButton("上一张")
        self.next_tile_button = QPushButton("下一张")
        self.previous_tile_button.setEnabled(False)
        self.next_tile_button.setEnabled(False)
        zoom_in_button = QPushButton("放大")
        zoom_out_button = QPushButton("缩小")
        reset_zoom_button = QPushButton("100%")
        fit_button = QPushButton("适应窗口")
        self.previous_tile_button.clicked.connect(self.show_previous_tile)
        self.next_tile_button.clicked.connect(self.show_next_tile)
        zoom_in_button.clicked.connect(self.image_viewer.zoom_in)
        zoom_out_button.clicked.connect(self.image_viewer.zoom_out)
        reset_zoom_button.clicked.connect(self.image_viewer.reset_to_100_percent)
        fit_button.clicked.connect(self.image_viewer.fit_image)
        controls_layout.addWidget(self.previous_tile_button)
        controls_layout.addWidget(self.next_tile_button)
        controls_layout.addSpacing(24)
        controls_layout.addWidget(zoom_in_button)
        controls_layout.addWidget(zoom_out_button)
        controls_layout.addWidget(reset_zoom_button)
        controls_layout.addWidget(fit_button)
        controls_layout.addStretch()
        page_layout.addLayout(controls_layout)

        help_label = QLabel(
            "左键拖动：画框　中键拖动：平移　滚轮：缩放　"
            "A / ←：上一张　D / →：下一张　F：适应窗口　"
            "1/2/3/4：选择类别　N/X/U：Tile 状态　"
            "Delete：删除选中框　Space：验证完成"
        )
        help_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        help_label.setWordWrap(True)
        page_layout.addWidget(help_label)

        memory_warning = QLabel(
            "标注数据已启用 annotations.json 自动保存；"
            "切换 Tile、返回设置和关闭程序前会强制保存。"
        )
        memory_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        memory_warning.setWordWrap(True)
        memory_warning.setStyleSheet("color: #176b2c; font-weight: bold;")
        page_layout.addWidget(memory_warning)

        shortcut_actions = [
            ("A", self.show_previous_tile),
            ("Left", self.show_previous_tile),
            ("D", self.show_next_tile),
            ("Right", self.show_next_tile),
            ("F", self.image_viewer.fit_image),
            ("-", self.image_viewer.zoom_in),
            ("*", self.image_viewer.zoom_out),
            ("1", lambda: self.set_active_annotation_class("metal")),
            ("2", lambda: self.set_active_annotation_class("stone")),
            ("3", lambda: self.set_active_annotation_class("rubber")),
            ("4", lambda: self.set_active_annotation_class("other")),
            ("N", lambda: self.set_current_tile_status("negative")),
            ("X", lambda: self.set_current_tile_status("exclude")),
            ("U", lambda: self.set_current_tile_status("uncertain")),
            ("Space", self.validate_current_tile_completion),
            ("Delete", self.delete_selected_bbox),
        ]
        for key_sequence, action in shortcut_actions:
            shortcut = QShortcut(QKeySequence(key_sequence), page)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)
            self._browser_shortcuts.append(shortcut)
        return page

    def _build_annotation_panel(self) -> QWidget:
        self.annotation_scroll_area = QScrollArea()
        self.annotation_scroll_area.setObjectName("annotationScrollArea")
        self.annotation_scroll_area.setMinimumWidth(320)
        self.annotation_scroll_area.setWidgetResizable(True)
        self.annotation_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.annotation_scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.annotation_scroll_area.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )

        panel = QWidget()
        panel.setObjectName("annotationPanelContent")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        self.class_group = QGroupBox("FOD 类别")
        class_layout = QVBoxLayout(self.class_group)
        class_layout.setContentsMargins(8, 8, 8, 8)
        class_layout.setSpacing(6)
        self.active_class_label = QLabel()
        self.active_class_label.setWordWrap(True)
        class_layout.addWidget(self.active_class_label)
        self.active_class_combo = QComboBox()
        for index, class_name in enumerate(FOD_CLASSES, start=1):
            self.active_class_combo.addItem(
                f"{index} - {FOD_CLASS_LABELS[class_name]} ({class_name})",
                class_name,
            )
        self.active_class_combo.setMinimumHeight(30)
        class_layout.addWidget(self.active_class_combo)
        layout.addWidget(self.class_group)

        self.tile_status_group = QGroupBox("Tile 状态")
        status_group_layout = QVBoxLayout(self.tile_status_group)
        status_group_layout.setContentsMargins(8, 8, 8, 8)
        status_group_layout.setSpacing(8)
        self.status_button_group = QButtonGroup(self.tile_status_group)
        self.status_button_group.setExclusive(True)
        self.status_buttons: dict[str, QPushButton] = {}
        status_layout = QGridLayout()
        status_layout.setHorizontalSpacing(6)
        status_layout.setVerticalSpacing(6)
        status_layout.setColumnStretch(0, 1)
        status_layout.setColumnStretch(1, 1)
        status_button_text = {
            "positive": "Positive（有 FOD）",
            "negative": "Negative（无 FOD）",
            "exclude": "Exclude（排除）",
            "uncertain": "Uncertain（待确认）",
        }
        status_positions = {
            "positive": (0, 0),
            "negative": (0, 1),
            "exclude": (1, 0),
            "uncertain": (1, 1),
        }
        for status in VALID_TILE_STATUSES:
            button = QPushButton(status_button_text[status])
            button.setCheckable(True)
            button.setMinimumHeight(38)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            button.clicked.connect(
                lambda checked, value=status: (
                    self.set_current_tile_status(value) if checked else None
                )
            )
            self.status_button_group.addButton(button)
            self.status_buttons[status] = button
            row, column = status_positions[status]
            status_layout.addWidget(button, row, column)

        self.clear_status_button = QPushButton("清除状态 / 设为未标注")
        self.clear_status_button.setMinimumHeight(34)
        self.clear_status_button.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.clear_status_button.clicked.connect(self.clear_current_tile_status)
        status_layout.addWidget(self.clear_status_button, 2, 0, 1, 2)
        status_group_layout.addLayout(status_layout)

        self.current_status_label = QLabel("当前 Tile 状态：未标注")
        self.trainable_label = QLabel("Trainable：否")
        self.completion_label = QLabel("完成状态：未完成")
        self.current_status_label.setWordWrap(True)
        status_info_layout = QVBoxLayout()
        status_info_layout.setSpacing(4)
        status_info_layout.addWidget(self.current_status_label)
        status_info_layout.addWidget(self.trainable_label)
        status_info_layout.addWidget(self.completion_label)
        status_group_layout.addLayout(status_info_layout)
        layout.addWidget(self.tile_status_group)

        self.bbox_group = QGroupBox("BBox 标注")
        bbox_layout = QVBoxLayout(self.bbox_group)
        bbox_layout.setContentsMargins(8, 8, 8, 8)
        bbox_layout.setSpacing(6)
        self.bbox_count_label = QLabel("当前 Tile bbox 数量：0")
        bbox_layout.addWidget(self.bbox_count_label)
        self.bbox_list = QListWidget()
        self.bbox_list.setMinimumHeight(120)
        self.bbox_list.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.bbox_list.currentRowChanged.connect(self._on_bbox_list_row_changed)
        bbox_layout.addWidget(self.bbox_list)

        self.selected_bbox_label = QLabel("选中框：未选择 bbox")
        self.selected_bbox_label.setWordWrap(True)
        self.selected_bbox_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        bbox_layout.addWidget(self.selected_bbox_label)

        self.change_bbox_class_button = QPushButton("修改选中框类别")
        self.delete_bbox_button = QPushButton("删除选中框")
        for button in (
            self.change_bbox_class_button,
            self.delete_bbox_button,
        ):
            button.setMinimumHeight(34)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
        self.change_bbox_class_button.setEnabled(False)
        self.delete_bbox_button.setEnabled(False)
        self.change_bbox_class_button.clicked.connect(
            self.change_selected_bbox_class
        )
        self.delete_bbox_button.clicked.connect(self.delete_selected_bbox)
        bbox_layout.addWidget(self.change_bbox_class_button)
        bbox_layout.addWidget(self.delete_bbox_button)
        layout.addWidget(self.bbox_group)

        self.notes_group = QGroupBox("备注 / Notes")
        notes_layout = QVBoxLayout(self.notes_group)
        notes_layout.setContentsMargins(8, 8, 8, 8)
        notes_layout.setSpacing(6)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("可输入中文备注；停止输入后自动保存")
        self.notes_edit.setMinimumHeight(90)
        self.notes_edit.setMaximumHeight(150)
        self.notes_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.notes_edit.textChanged.connect(self._on_notes_changed)
        notes_layout.addWidget(self.notes_edit)
        layout.addWidget(self.notes_group)

        self.save_group = QGroupBox("保存与完成")
        save_layout = QVBoxLayout(self.save_group)
        save_layout.setContentsMargins(8, 8, 8, 8)
        save_layout.setSpacing(6)
        self.autosave_status_label = QLabel("自动保存：未加载")
        self.retry_save_button = QPushButton("重试保存")
        self.retry_save_button.setMinimumHeight(34)
        self.retry_save_button.setEnabled(False)
        self.retry_save_button.clicked.connect(self.retry_save)
        save_layout.addWidget(self.autosave_status_label)
        save_layout.addWidget(self.retry_save_button)

        self.validate_tile_button = QPushButton("验证当前 Tile")
        self.validate_tile_button.setMinimumHeight(40)
        self.validate_tile_button.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.validate_tile_button.clicked.connect(
            self.validate_current_tile_completion
        )
        save_layout.addWidget(self.validate_tile_button)
        completion_hint = QLabel("Space = 验证当前 Tile")
        completion_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        save_layout.addWidget(completion_hint)
        layout.addWidget(self.save_group)
        layout.addStretch()

        self.active_class_combo.currentIndexChanged.connect(
            self._on_active_class_changed
        )
        self._on_active_class_changed()
        self._refresh_status_panel()
        self.annotation_scroll_area.setWidget(panel)
        return self.annotation_scroll_area

    def select_input_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "选择原图输入目录")
        if directory:
            self.input_dir_edit.setText(directory)

    def select_output_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "选择成果输出根目录")
        if directory:
            self.output_dir_edit.setText(directory)

    def update_stride(self):
        stride = self.tile_size_spin.value() - self.overlap_spin.value()
        if stride > 0:
            self.stride_label.setText(str(stride))
        else:
            self.stride_label.setText("无效：Overlap 必须小于 Tile Size")

    def invalidate_validation(self, *_args):
        had_active_state = (
            self._settings_valid or self._stage3_completed_settings is not None
        )
        self._settings_valid = False
        self._validated_settings = None
        self._stage3_completed_settings = None
        self._browser_output_root = None
        self.scan_button.setEnabled(False)
        self.tile_button.setEnabled(False)
        self.open_browser_button.setEnabled(False)
        self.finalize_package_button.setEnabled(False)
        if had_active_state:
            self.progress_label.setText("项目设置已修改，请重新验证")
            self.statusBar().showMessage("项目设置已修改，请重新验证")

    def _read_settings(self) -> dict[str, object]:
        return {
            "input_dir": self.input_dir_edit.text().strip(),
            "output_root": self.output_dir_edit.text().strip(),
            "scene": self.scene_combo.currentData(),
            "annotator_id": self.annotator_edit.text().strip(),
            "tile_size": self.tile_size_spin.value(),
            "overlap": self.overlap_spin.value(),
        }

    def validate_settings(self):
        settings = self._read_settings()
        input_dir_text = str(settings["input_dir"])
        output_dir_text = str(settings["output_root"])
        annotator_id = str(settings["annotator_id"])
        tile_size = int(settings["tile_size"])
        overlap = int(settings["overlap"])
        stride = tile_size - overlap

        if not input_dir_text:
            self.show_error("请选择原图输入目录。")
            return
        input_dir = Path(input_dir_text)
        if not input_dir.exists():
            self.show_error("原图输入目录不存在。")
            return
        if not input_dir.is_dir():
            self.show_error("原图输入路径不是文件夹。")
            return

        if not output_dir_text:
            self.show_error("请选择成果输出目录。")
            return
        output_dir = Path(output_dir_text)
        if not output_dir.exists():
            self.show_error("成果输出目录不存在。")
            return
        if not output_dir.is_dir():
            self.show_error("成果输出路径不是文件夹。")
            return

        if not annotator_id:
            self.show_error("请输入标注人员 ID。")
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]+", annotator_id):
            self.show_error("标注人员 ID 只能包含字母、数字、下划线和短横线。")
            return
        if overlap >= tile_size:
            self.show_error("Overlap 必须小于 Tile Size。")
            return
        if stride <= 0:
            self.show_error("Stride 必须大于 0。")
            return
        if tile_size > 5460:
            self.show_error(
                "Tile Size 不能大于正式原图的最短边 5460；"
                "否则无法生成固定尺寸正方形 Tile。"
            )
            return

        try:
            resolved_input, resolved_output = validate_path_relationship(
                input_dir, output_dir
            )
        except SourceInitializationError as exc:
            self.show_error(str(exc))
            return

        settings["input_dir"] = resolved_input
        settings["output_root"] = resolved_output
        self._validated_settings = settings
        self._settings_valid = True
        tiles_manifest = resolved_output / "manifests" / "tiles.csv"
        self._browser_output_root = resolved_output if tiles_manifest.is_file() else None
        self.scan_button.setEnabled(
            self._stage3_completed_settings is None
            and self._browser_output_root is None
        )
        self.open_browser_button.setEnabled(self._browser_output_root is not None)
        self._refresh_finalize_button()
        if self._browser_output_root is not None:
            self.progress_label.setText(
                "项目设置已验证，检测到已有 tiles.csv，可进入 Tile 浏览器"
            )
        else:
            self.progress_label.setText("项目设置已验证，可以开始扫描")
        self.result_label.setText(
            "项目设置验证通过。\n\n"
            f"原图输入目录：{resolved_input}\n"
            f"成果输出目录：{resolved_output}\n"
            f"场景：{settings['scene']}\n"
            f"标注人员 ID：{annotator_id}\n"
            f"Tile Size：{tile_size}\n"
            f"Overlap：{overlap}\n"
            f"Stride：{stride}"
        )
        self.statusBar().showMessage("项目设置验证通过")
        QMessageBox.information(self, "验证成功", "项目设置验证通过。")

    def _set_processing_state(self, processing: bool):
        for control in self._configuration_controls:
            control.setEnabled(not processing)
        self.validate_button.setEnabled(not processing)
        self.scan_button.setEnabled(
            not processing
            and self._settings_valid
            and self._stage3_completed_settings is None
            and self._browser_output_root is None
        )
        self.tile_button.setEnabled(
            not processing and self._stage3_completed_settings is not None
        )
        self.open_browser_button.setEnabled(
            not processing and self._browser_output_root is not None
        )
        self.finalize_package_button.setEnabled(
            not processing
            and self._settings_valid
            and self._browser_output_root is not None
            and self._has_complete_annotation_file_set(self._browser_output_root)
        )
        self.merge_packages_button.setEnabled(not processing)

    @staticmethod
    def _has_complete_annotation_file_set(output_root: Path | None) -> bool:
        if output_root is None:
            return False
        sources_root = output_root / "sources"
        if not sources_root.is_dir():
            return False
        source_directories = [
            path
            for path in sources_root.iterdir()
            if path.is_dir() and (path / "source.json").is_file()
        ]
        return bool(source_directories) and all(
            (path / "annotations.json").is_file()
            for path in source_directories
        )

    def _refresh_finalize_button(self) -> None:
        self.finalize_package_button.setEnabled(
            self._settings_valid
            and self._browser_output_root is not None
            and self._has_complete_annotation_file_set(self._browser_output_root)
        )

    def _update_package_progress(
        self, current: int, total: int, source_id: str, tile_id: str
    ) -> None:
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(current)
        self.progress_label.setText(
            "正在校验并封装 Worker Package\n\n"
            f"Source：{source_id}\n"
            f"Tile：{current} / {total}\n"
            f"TileID：{tile_id}"
        )
        QApplication.processEvents()

    def finalize_worker_data(self) -> None:
        if (
            not self._settings_valid
            or self._validated_settings is None
            or self._browser_output_root is None
        ):
            QMessageBox.warning(
                self,
                "无法封装 Worker Package",
                "请先验证包含完整 Stage 8 Annotation 数据的项目设置。",
            )
            return
        self._set_processing_state(True)
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("正在执行 Worker Package 全量预检...")
        self.statusBar().showMessage("正在校验并封装 Worker Package")
        try:
            result = finalize_worker_package(
                input_dir=self._validated_settings["input_dir"],
                output_root=self._browser_output_root,
                annotator_id=str(self._validated_settings["annotator_id"]),
                progress_callback=self._update_package_progress,
            )
        except PackageValidationError as exc:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Worker Package 封装失败")
            self.result_label.setText(exc.format_for_gui())
            self.statusBar().showMessage("Worker Package 封装失败")
            QMessageBox.critical(
                self, "Worker Package 封装失败", exc.format_for_gui()
            )
        except Exception as exc:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Worker Package 封装发生意外错误")
            self.statusBar().showMessage("Worker Package 封装发生意外错误")
            QMessageBox.critical(
                self,
                "意外错误",
                f"Worker Package 未成功封装：\n\n{type(exc).__name__}: {exc}",
            )
        else:
            stats = result.statistics
            message = (
                "Worker Package 封装成功\n\n"
                f"Annotator：{result.annotator_id}\n"
                f"Scenes：{', '.join(result.scenes)}\n"
                f"Sources：{result.source_count}\n"
                f"Tiles：{result.tile_count}\n\n"
                f"Positive：{stats['positive']}\n"
                f"Negative：{stats['negative']}\n"
                f"Exclude：{stats['exclude']}\n"
                f"Uncertain：{stats['uncertain']}\n"
                f"Objects：{stats['object_count']}\n\n"
                f"Package：\n{result.package_path}\n\n"
                f"Validation Report：\n{result.validation_report_path}"
            )
            if result.log_warning:
                message += f"\n\n警告：{result.log_warning}"
            self.progress_bar.setRange(0, result.tile_count)
            self.progress_bar.setValue(result.tile_count)
            self.progress_label.setText("Worker Package 封装完成")
            self.result_label.setText(message)
            if result.log_warning:
                self.statusBar().showMessage("Worker Package 封装成功，但日志写入异常")
                QMessageBox.warning(
                    self, "Worker Package 封装成功（日志警告）", message
                )
            else:
                self.statusBar().showMessage("Worker Package 封装成功")
                QMessageBox.information(self, "Worker Package 封装成功", message)
        finally:
            self._set_processing_state(False)

    def open_merge_dialog(self) -> None:
        dialog = MergeDialog(self)
        dialog.exec()

    def _update_progress(self, current: int, total: int, relative_path: str):
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)
        self.progress_label.setText(f"正在处理：{current} / {total}\n{relative_path}")
        QApplication.processEvents()

    def initialize_source_data(self):
        if not self._settings_valid or self._validated_settings is None:
            self.show_error("项目设置尚未验证，或验证结果已经失效。请重新验证。")
            return

        self._set_processing_state(True)
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("正在扫描原图...")
        self.statusBar().showMessage("正在初始化 Source 数据")
        try:
            result = initialize_sources(
                **self._validated_settings,
                progress_callback=self._update_progress,
            )
        except SourceInitializationError as exc:
            self.statusBar().showMessage("Source 初始化失败")
            QMessageBox.critical(self, "Source 初始化失败", exc.format_for_gui())
        except Exception as exc:
            self.statusBar().showMessage("Source 初始化发生意外错误")
            QMessageBox.critical(
                self,
                "意外错误",
                f"Source 初始化没有成功：\n\n{type(exc).__name__}: {exc}",
            )
        else:
            self._stage3_completed_settings = dict(self._validated_settings)
            self.progress_bar.setRange(0, result.source_count)
            self.progress_bar.setValue(result.source_count)
            self.progress_label.setText(
                f"处理完成：{result.source_count} / {result.source_count}"
            )
            self.result_label.setText(
                "Source 初始化成功\n\n"
                f"场景：{result.scene}\n"
                f"原图数量：{result.source_count}\n"
                f"Source 数量：{result.source_count}\n\n"
                f"Tile Size：{result.tile_size}\n"
                f"Overlap：{result.overlap}\n"
                f"Stride：{result.stride}\n\n"
                f"Source Manifest：\n{result.manifest_path}"
            )
            if result.log_warning:
                self.result_label.setText(
                    self.result_label.text()
                    + f"\n\n警告：Source 数据已成功提交，但日志写入异常：\n"
                    f"{result.log_warning}"
                )
                self.statusBar().showMessage("Source 初始化成功，但操作日志写入异常")
                QMessageBox.warning(
                    self,
                    "Source 初始化完成（日志警告）",
                    "Source 数据已经成功提交，但操作日志写入异常：\n\n"
                    f"{result.log_warning}",
                )
            else:
                self.statusBar().showMessage("Source 初始化成功")
                QMessageBox.information(
                    self, "Source 初始化完成", "Source 初始化完成。"
                )
            self._settings_valid = False
            self._validated_settings = None
        finally:
            self._set_processing_state(False)

    def _update_tile_progress(
        self,
        source_index: int,
        source_total: int,
        tile_index: int,
        tile_total: int,
        overall_current: int,
        overall_total: int,
        source_id: str,
    ):
        self.progress_bar.setRange(0, overall_total)
        self.progress_bar.setValue(overall_current)
        self.progress_label.setText(
            "正在生成 Tile\n\n"
            f"Source：{source_id}\n"
            f"Source：{source_index} / {source_total}\n"
            f"Tile：{tile_index} / {tile_total}\n"
            f"总进度：{overall_current} / {overall_total}"
        )
        QApplication.processEvents()

    def initialize_tile_data(self):
        if self._stage3_completed_settings is None:
            QMessageBox.warning(
                self,
                "无法生成 Tile",
                "当前程序本次会话尚未成功完成 Source 初始化。",
            )
            return

        input_dir = self._stage3_completed_settings["input_dir"]
        output_root = self._stage3_completed_settings["output_root"]
        self._set_processing_state(True)
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("正在重新校验 Stage 3 数据和原始图片...")
        self.statusBar().showMessage("正在初始化 Tile 数据")
        try:
            result = initialize_tiles(
                input_dir=input_dir,
                output_root=output_root,
                progress_callback=self._update_tile_progress,
            )
        except TileInitializationError as exc:
            self.statusBar().showMessage("Tile 初始化失败")
            QMessageBox.critical(self, "Tile 初始化失败", exc.format_for_gui())
        except Exception as exc:
            self.statusBar().showMessage("Tile 初始化发生意外错误")
            QMessageBox.critical(
                self,
                "意外错误",
                f"Tile 初始化没有成功：\n\n{type(exc).__name__}: {exc}",
            )
        else:
            self.progress_bar.setRange(0, result.tile_count)
            self.progress_bar.setValue(result.tile_count)
            self.progress_label.setText(
                f"Tile 生成完成：{result.tile_count} / {result.tile_count}"
            )
            self.result_label.setText(
                "Tile 初始化成功\n\n"
                f"Source 数量：{result.source_count}\n"
                f"Tile 总数：{result.tile_count}\n\n"
                f"Tile Size：{result.tile_size}\n"
                f"Overlap：{result.overlap}\n"
                f"Stride：{result.stride}\n\n"
                f"Tiles Manifest：\n{result.manifest_path}"
            )
            if result.log_warning:
                self.result_label.setText(
                    self.result_label.text()
                    + "\n\n警告：Tile 数据已成功提交，但日志写入异常：\n"
                    + result.log_warning
                )
                self.statusBar().showMessage("Tile 初始化成功，但操作日志写入异常")
                QMessageBox.warning(
                    self,
                    "Tile 初始化完成（日志警告）",
                    "Tile 数据已经成功提交，但操作日志写入异常：\n\n"
                    + result.log_warning,
                )
            else:
                self.statusBar().showMessage("Tile 初始化成功")
                QMessageBox.information(self, "Tile 初始化完成", "Tile 初始化完成。")
            self._browser_output_root = Path(output_root).resolve()
            self._stage3_completed_settings = None
            self._settings_valid = False
            self._validated_settings = None
        finally:
            self._set_processing_state(False)

    def open_tile_browser(self):
        if self._browser_output_root is None:
            QMessageBox.warning(
                self,
                "无法进入 Tile 浏览器",
                "请先验证包含合法 Stage 4 tiles.csv 的成果输出目录，"
                "或在本次会话完成 Tile 初始化。",
            )
            return

        try:
            records = load_tile_records(self._browser_output_root)
            storage = AnnotationStorage.open_or_initialize(
                self._browser_output_root,
                self.annotator_edit.text().strip(),
                tile_records=records,
            )
        except (TileInitializationError, AnnotationStorageError) as exc:
            self.statusBar().showMessage("Annotation 初始化或加载失败")
            QMessageBox.critical(
                self,
                "无法进入 Tile 浏览器",
                exc.format_for_gui(),
            )
            return

        if self._annotation_storage is not None:
            self._annotation_storage.close()
        self._annotation_storage = storage
        self._annotation_store = storage.annotations
        self._annotation_store_root = self._browser_output_root
        self._set_autosave_state("saved")

        source_counts: dict[str, int] = {}
        source_local_positions: list[int] = []
        for record in records:
            source_counts[record.source_id] = (
                source_counts.get(record.source_id, 0) + 1
            )
            source_local_positions.append(source_counts[record.source_id])

        self._tile_records = records
        self._source_tile_counts = source_counts
        self._source_local_positions = source_local_positions
        self._current_tile_index = -1
        if not self._display_tile(0):
            self._clear_browser_session()
            return

        self.stacked_widget.setCurrentWidget(self.browser_page)
        current = self._tile_records[self._current_tile_index]
        self.statusBar().showMessage(
            f"已加载 {len(records)} 个 Tile | 当前 {current.tile_id}"
        )

    def _display_tile(self, index: int) -> bool:
        if not 0 <= index < len(self._tile_records):
            return False
        if (
            self._current_tile_index >= 0
            and index != self._current_tile_index
            and not self.flush_pending_changes()
        ):
            return False
        record = self._tile_records[index]
        if self._browser_output_root is None:
            return False
        try:
            self._sync_current_annotations_to_store()
            target_annotation = self._annotation_store.get(
                record.tile_id, TileAnnotation()
            )
            validate_tile_annotation(target_annotation)
            for annotation in target_annotation.objects:
                validate_annotation(annotation, record.width, record.height)
            image_path = resolve_tile_image_path(
                self._browser_output_root, record.tile_relative_path
            )
            if not image_path.is_file():
                raise TileInitializationError(f"Tile 图片不存在：{image_path}")
            self.image_viewer.set_image(
                image_path,
                expected_width=record.width,
                expected_height=record.height,
            )
            self.image_viewer.set_annotations(target_annotation.objects)
        except (
            AnnotationValidationError,
            TileInitializationError,
            ImageViewerError,
        ) as exc:
            self.statusBar().showMessage("Tile 图片加载失败")
            QMessageBox.critical(
                self,
                "Tile 图片加载失败",
                f"当前索引保持不变，未跳过损坏数据。\n\n{exc}",
            )
            return False

        self._current_tile_index = index
        source_position = self._source_local_positions[index]
        source_total = self._source_tile_counts[record.source_id]
        self.browser_tile_id_label.setText(record.tile_id)
        self.browser_source_id_label.setText(record.source_id)
        self.browser_scene_label.setText(record.scene)
        self.browser_coordinates_label.setText(f"x={record.x}，y={record.y}")
        self.browser_size_label.setText(f"{record.width} × {record.height}")
        self.browser_source_position_label.setText(
            f"{source_position} / {source_total}"
        )
        self.browser_global_position_label.setText(
            f"{index + 1} / {len(self._tile_records)}"
        )
        self.previous_tile_button.setEnabled(index > 0)
        self.next_tile_button.setEnabled(index < len(self._tile_records) - 1)
        self._load_current_notes()
        self._refresh_bbox_panel()
        self._refresh_status_panel()
        self.statusBar().showMessage(
            f"Tile {index + 1} / {len(self._tile_records)} | "
            f"Source {record.source_id} | X={record.x} Y={record.y}"
        )
        return True

    def show_previous_tile(self):
        if self._current_tile_index > 0:
            self._display_tile(self._current_tile_index - 1)

    def show_next_tile(self):
        if 0 <= self._current_tile_index < len(self._tile_records) - 1:
            self._display_tile(self._current_tile_index + 1)

    def return_to_settings(self):
        if not self.flush_pending_changes():
            return
        self.stacked_widget.setCurrentWidget(self.settings_page)
        self._clear_browser_session()
        self._refresh_finalize_button()
        self.statusBar().showMessage("已退出 Tile 浏览器")

    def _clear_browser_session(self):
        self._notes_save_timer.stop()
        self.image_viewer.clear_image()
        self._tile_records = []
        self._source_local_positions = []
        self._source_tile_counts = {}
        self._current_tile_index = -1
        self.previous_tile_button.setEnabled(False)
        self.next_tile_button.setEnabled(False)
        self._loading_notes = True
        self.notes_edit.clear()
        self._loading_notes = False
        self._refresh_bbox_panel()
        self._refresh_status_panel()
        if self._annotation_storage is not None:
            self._annotation_storage.close()
        self._annotation_storage = None
        self._annotation_store = {}
        self._annotation_store_root = None
        self._set_autosave_state("idle")

    def _sync_current_annotations_to_store(self) -> None:
        if 0 <= self._current_tile_index < len(self._tile_records):
            tile_id = self._tile_records[self._current_tile_index].tile_id
            current = self._annotation_store.get(tile_id, TileAnnotation())
            updated = current.with_objects(self.image_viewer.get_annotations())
            if updated != current:
                self._persist_tile_change(updated, "bbox_sync", immediate=True)

    def _on_annotations_changed(self, change_type: str) -> None:
        current = self._current_tile_annotation(use_canvas=False)
        if current is None:
            return
        updated = current.with_objects(self.image_viewer.get_annotations())
        self._persist_tile_change(updated, change_type, immediate=True)
        self._refresh_bbox_panel()
        self._refresh_status_panel()

    def _current_tile_id(self) -> str | None:
        if 0 <= self._current_tile_index < len(self._tile_records):
            return self._tile_records[self._current_tile_index].tile_id
        return None

    def _current_tile_annotation(
        self, use_canvas: bool = True
    ) -> TileAnnotation | None:
        tile_id = self._current_tile_id()
        if tile_id is None:
            return None
        stored = self._annotation_store.get(tile_id, TileAnnotation())
        if not use_canvas:
            return stored
        return stored.with_objects(self.image_viewer.get_annotations())

    def _refresh_status_panel(self) -> None:
        tile_id = self._current_tile_id()
        annotation = self._current_tile_annotation()
        has_tile = tile_id is not None and annotation is not None
        status = annotation.status if annotation is not None else None

        self.status_button_group.setExclusive(False)
        for value, button in self.status_buttons.items():
            blocker = QSignalBlocker(button)
            button.setChecked(has_tile and status == value)
            button.setEnabled(has_tile)
            del blocker
        self.status_button_group.setExclusive(True)
        self.clear_status_button.setEnabled(has_tile and status is not None)
        self.validate_tile_button.setEnabled(has_tile)
        self.notes_edit.setEnabled(has_tile)

        if status is None:
            self.current_status_label.setText("当前 Tile 状态：未标注")
        else:
            self.current_status_label.setText(
                f"当前 Tile 状态：{TILE_STATUS_LABELS[status]}"
            )
        self.trainable_label.setText(
            f"Trainable：{'是' if derive_trainable(status) else '否'}"
        )
        completed = annotation.completed if annotation is not None else False
        self.completion_label.setText(
            "完成状态：已完成"
            if completed
            else "完成状态：未完成"
        )
        self.image_viewer.set_drawing_enabled(
            has_tile and drawing_enabled_for_status(status)
        )

    def set_current_tile_status(self, status: str | None) -> bool:
        tile_id = self._current_tile_id()
        current = self._current_tile_annotation()
        if tile_id is None or current is None:
            return False
        if current.status == status:
            self._refresh_status_panel()
            return True
        try:
            updated = current.with_status(status)
        except AnnotationValidationError as exc:
            self._refresh_status_panel()
            QMessageBox.warning(self, "无法修改 Tile 状态", str(exc))
            return False

        saved = self._persist_tile_change(
            updated, "status_changed", immediate=True
        )
        self._refresh_status_panel()
        status_text = "未标注" if status is None else TILE_STATUS_LABELS[status]
        if saved:
            self.statusBar().showMessage(f"当前 Tile 状态：{status_text}")
        return saved

    def clear_current_tile_status(self) -> None:
        self.set_current_tile_status(None)

    def validate_current_tile_completion(self) -> bool:
        tile_id = self._current_tile_id()
        if tile_id is None:
            return False
        try:
            self._sync_current_annotations_to_store()
            annotation = self._annotation_store[tile_id]
            validate_tile_completion(annotation)
            completed = annotation.mark_completed(utc_now_iso8601())
        except AnnotationValidationError as exc:
            self._refresh_status_panel()
            QMessageBox.warning(self, "当前 Tile 尚未完成", str(exc))
            return False

        saved = self._persist_tile_change(
            completed, "completed", immediate=True
        )
        self._refresh_status_panel()
        if saved:
            self.statusBar().showMessage("当前 Tile 已完成并自动保存")
        return saved

    def _on_drawing_blocked(self) -> None:
        annotation = self._current_tile_annotation()
        status = annotation.status if annotation is not None else None
        status_text = "未标注" if status is None else status
        message = (
            f"当前 Tile 状态为 {status_text}，不能添加 bbox。\n\n"
            "如需标注 FOD，请先将 Tile 状态改为未标注、positive 或 uncertain。"
        )
        self.statusBar().showMessage("当前 Tile 状态不允许添加 bbox")
        QMessageBox.warning(self, "无法添加 bbox", message)

    def _persist_tile_change(
        self,
        annotation: TileAnnotation,
        change_type: str,
        immediate: bool,
    ) -> bool:
        tile_id = self._current_tile_id()
        if tile_id is None:
            return False
        self._set_autosave_state("pending")
        if self._annotation_storage is None:
            self._annotation_store[tile_id] = annotation
            if immediate:
                self._set_autosave_state("saved")
            return True
        try:
            if immediate:
                self._annotation_storage.save_tile_annotation(
                    tile_id, annotation, change_type
                )
            else:
                self._annotation_storage.stage_tile_change(
                    tile_id, annotation, change_type
                )
            self._annotation_store = self._annotation_storage.annotations
        except AnnotationStorageError as exc:
            self._annotation_store = self._annotation_storage.annotations
            self._show_save_failure(exc)
            return False
        if immediate:
            self._set_autosave_state("saved")
        return True

    def _on_notes_changed(self) -> None:
        if self._loading_notes:
            return
        current = self._current_tile_annotation(use_canvas=False)
        if current is None:
            return
        try:
            updated = current.with_notes(self.notes_edit.toPlainText())
        except AnnotationValidationError as exc:
            QMessageBox.warning(self, "备注无效", str(exc))
            return
        if updated == current:
            return
        if self._persist_tile_change(updated, "notes_changed", immediate=False):
            self._notes_save_timer.start()
        self._refresh_status_panel()

    def _load_current_notes(self) -> None:
        annotation = self._current_tile_annotation(use_canvas=False)
        notes = annotation.notes if annotation is not None else ""
        self._loading_notes = True
        blocker = QSignalBlocker(self.notes_edit)
        self.notes_edit.setPlainText(notes)
        del blocker
        self._loading_notes = False

    def _flush_notes_timer(self) -> None:
        self.flush_pending_changes()

    def flush_pending_changes(self) -> bool:
        """Synchronously save all dirty annotation Sources before leaving."""
        self._notes_save_timer.stop()
        if self._annotation_storage is None:
            self._set_autosave_state("saved")
            return True
        try:
            self._annotation_storage.flush_all()
        except AnnotationStorageError as exc:
            self._show_save_failure(exc)
            return False
        self._set_autosave_state("saved")
        return True

    def retry_save(self) -> None:
        self.flush_pending_changes()

    def _set_autosave_state(self, state: str) -> None:
        labels = {
            "idle": "自动保存：未加载",
            "pending": "自动保存：待保存...",
            "saved": "自动保存：已保存",
            "failed": "自动保存：保存失败",
        }
        self.autosave_status_label.setText(labels[state])
        dirty = (
            self._annotation_storage is not None
            and self._annotation_storage.has_dirty_data
        )
        self.retry_save_button.setEnabled(state == "failed" or dirty)

    def _show_save_failure(self, error: AnnotationStorageError) -> None:
        self._set_autosave_state("failed")
        self.statusBar().showMessage("Annotation 自动保存失败，尚未写盘")
        QMessageBox.critical(
            self,
            "Annotation 自动保存失败",
            "当前内存数据仍然保留，但在保存成功前不能切换 Tile、"
            "返回项目设置或关闭程序。\n\n"
            f"{error}",
        )

    def _refresh_bbox_panel(self) -> None:
        annotations = self.image_viewer.get_annotations()
        selected_index = self.image_viewer.selected_annotation_index()
        self.bbox_list.blockSignals(True)
        self.bbox_list.clear()
        for index, annotation in enumerate(annotations):
            x1, y1, x2, y2 = annotation.bbox
            item = QListWidgetItem(
                f"#{index + 1} {annotation.class_name} "
                f"[{x1}, {y1}, {x2}, {y2}]"
            )
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.bbox_list.addItem(item)
        if selected_index >= 0:
            self.bbox_list.setCurrentRow(selected_index)
        self.bbox_list.blockSignals(False)
        self.bbox_count_label.setText(f"当前 Tile bbox 数量：{len(annotations)}")
        self._update_selected_bbox_info()

    def _on_canvas_selection_changed(
        self, _annotation: BBoxAnnotation | None
    ) -> None:
        selected_index = self.image_viewer.selected_annotation_index()
        self.bbox_list.blockSignals(True)
        self.bbox_list.setCurrentRow(selected_index)
        self.bbox_list.blockSignals(False)
        self._update_selected_bbox_info()

    def _on_bbox_list_row_changed(self, row: int) -> None:
        if row < 0:
            self.image_viewer.select_annotation(-1)
            return
        item = self.bbox_list.item(row)
        annotation_index = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(annotation_index, int):
            self.image_viewer.select_annotation(annotation_index)

    def _update_selected_bbox_info(self) -> None:
        annotation = self.image_viewer.selected_annotation()
        has_selection = annotation is not None
        self.change_bbox_class_button.setEnabled(has_selection)
        self.delete_bbox_button.setEnabled(has_selection)
        if annotation is None:
            self.selected_bbox_label.setText("选中框：未选择 bbox")
            return
        x1, y1, x2, y2 = annotation.bbox
        self.selected_bbox_label.setText(
            f"类别：{annotation.class_name} / "
            f"{FOD_CLASS_LABELS[annotation.class_name]}\n"
            f"BBox：[{x1}, {y1}, {x2}, {y2}]\n"
            f"宽度：{annotation.width} px\n"
            f"高度：{annotation.height} px"
        )

    def _on_active_class_changed(self, *_args) -> None:
        class_name = self.active_class_combo.currentData()
        if class_name not in FOD_CLASSES:
            return
        self.image_viewer.set_active_class(class_name)
        self.active_class_label.setText(
            f"当前绘制类别：{class_name} / {FOD_CLASS_LABELS[class_name]}"
        )

    def set_active_annotation_class(self, class_name: str) -> None:
        index = self.active_class_combo.findData(class_name)
        if index < 0:
            return
        if self.active_class_combo.currentIndex() == index:
            self._on_active_class_changed()
        else:
            self.active_class_combo.setCurrentIndex(index)

    def change_selected_bbox_class(self) -> None:
        class_name = self.active_class_combo.currentData()
        if class_name in FOD_CLASSES:
            self.image_viewer.change_selected_class(class_name)

    def delete_selected_bbox(self) -> None:
        if self.image_viewer.selected_annotation() is None:
            return
        current = self._current_tile_annotation()
        if current is None:
            return
        try:
            validate_bbox_deletion(current.status, len(current.objects))
        except AnnotationValidationError as exc:
            QMessageBox.warning(self, "无法删除 bbox", str(exc))
            return
        self.image_viewer.delete_selected_annotation()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.flush_pending_changes():
            QMessageBox.warning(
                self,
                "存在未保存标注",
                "存在尚未写入磁盘的标注数据，程序暂不关闭。\n"
                "请处理保存错误后点击“重试保存”。",
            )
            event.ignore()
            return
        if self._annotation_storage is not None:
            self._annotation_storage.close()
            self._annotation_storage = None
        super().closeEvent(event)

    def show_error(self, message: str):
        self._settings_valid = False
        self._validated_settings = None
        self._stage3_completed_settings = None
        self._browser_output_root = None
        self.scan_button.setEnabled(False)
        self.tile_button.setEnabled(False)
        self.open_browser_button.setEnabled(False)
        self.finalize_package_button.setEnabled(False)
        self.statusBar().showMessage("项目设置存在错误")
        QMessageBox.warning(self, "设置错误", message)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("FOD Annotation Tool")
    app.setOrganizationName("FOD Annotation Tool")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

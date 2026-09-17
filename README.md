# FOD Annotation Tool

机场跑道 FOD（Foreign Object Debris，外来物）数据初始化、固定尺寸切片、人工标注、Worker 成果封装以及多 Worker 严格合并桌面工具。

项目面向 Windows 10 / Windows 11，支持多名标注人员分别 clone 或使用 Windows 发行版离线工作。当前工具版本为 `0.1.0`，Source、Annotation 和 Worker Package schema 版本均为 `1.0`。

## 当前能力

- 递归扫描原始图片，使用 Pillow 检查格式、完整性和 `8192 × 5460` 尺寸。
- 分块计算完整 SHA256，生成与文件名和标注人员无关的确定性 SourceID。
- 检测重复原图、SHA256 前缀碰撞、路径嵌套和已有数据冲突。
- 按固定尺寸、固定步长切片；边缘采用回贴策略，不缩放、不产生小 Tile。
- 生成确定性 TileID、`source.json`、`sources.csv` 和 `tiles.csv`。
- 使用 `QGraphicsView` 浏览 Tile，支持缩放、适应窗口和中键平移。
- 在 Tile 像素坐标中绘制、选择、改类和删除 bbox。
- 支持四类 FOD：`metal`、`stone`、`rubber`、`other`。
- 支持 `positive`、`negative`、`exclude`、`uncertain` 四种 Tile 状态。
- 每个 Source 使用一个 `annotations.json`，自动保存 bbox、状态、备注和完成确认。
- 保存失败时保留 dirty 状态并阻止导航、返回或关闭，避免已知数据丢失。
- 严格校验并 Finalize Worker Package，生成 canonical manifests、`package.json` 和内容指纹。
- 对多个 Worker Package 做全量预检和冲突检测，使用 staging 生成 Master Dataset。
- 提供 PyInstaller onedir Windows 发行构建，最终用户无需安装 Python。

## 尚未包含

当前版本尚未实现 YOLO 数据集导出、Train/Val/Test 划分、正式审核工作流、bbox 拖动/缩放手柄、自定义快捷键和撤销/重做。这些功能不应通过手工修改 JSON 或 CSV 代替。

## 技术栈

- Python 3.11
- PySide6 6.11.2
- Pillow 12.3.0
- PyYAML 6.0.3
- pytest 9.1.1
- PyInstaller 6.22.3（仅构建 Windows 发行版）

## 普通用户快速开始

Windows 发行版采用 `onedir + ZIP`：

```text
FOD标注工具_Windows.zip
└─ FOD标注工具/
   ├─ FOD标注工具.exe
   ├─ 使用说明.txt
   └─ _internal/
```

使用步骤：

1. 完整解压 `FOD标注工具_Windows.zip`，不要直接在 ZIP 内运行程序。
2. 进入解压后的 `FOD标注工具` 文件夹。
3. 双击 `FOD标注工具.exe`。
4. 原始图片目录、Worker Output 目录和 Master Dataset 目录均通过 GUI 选择。

发行版内含 Python、PySide6/Qt 和 Pillow runtime，不要求用户安装 Python、Conda 或任何 pip 包。当前 EXE 未进行数字签名，部分电脑首次运行可能显示 Windows SmartScreen 提示。

## Worker 标注流程

### 1. 填写项目设置

在主页面填写：

- 原图输入目录；
- 成果输出目录（每名标注人员必须使用独立目录）；
- 场景：`S01`、`S02` 或 `S03`；
- 管理员分配的 `annotator_id`，例如 `A01`；
- `tile_size` 和 `overlap`。

`stride` 始终由下式计算，不能独立编辑：

```text
stride = tile_size - overlap
```

默认参数：

```text
原图尺寸：8192 × 5460
tile_size：1024
overlap：256
stride：768
```

任何设置发生变化后，之前的验证立即失效，必须重新点击“验证项目设置”。输入目录和输出目录不能相同，也不能互相包含。

### 2. 初始化 Source

点击“扫描原图并建立 Source 清单”。程序会：

1. 递归查找 `.jpg`、`.jpeg`、`.png`、`.tif`、`.tiff`、`.bmp`，扩展名不区分大小写；
2. 按相对输入目录的 POSIX 路径稳定排序；
3. 验证图片可读、完整且尺寸严格为 `8192 × 5460`；
4. 按 4 MiB 块计算完整 SHA256；
5. 检测重复内容和 SourceID 前缀碰撞；
6. 预检全部通过后，使用 staging 提交 Source 数据。

SourceID 算法：

```text
SourceID = SceneCode + "_" + SHA256 前 16 位的大写形式
```

示例：

```text
S01_A84F39BC21D7F238
```

SourceID 不包含原始文件名或 `annotator_id`。

### 3. 初始化 Tile

点击“生成切片与 Tile 清单”。Stage 4 会重新读取并交叉验证 `sources.csv`、`source.json` 和原图 SHA256，不依赖 GUI 临时值。

切片规则：

- 坐标原点为原图左上角 `(0, 0)`；
- `x` 向右增加，`y` 向下增加；
- 遍历顺序为 Y 外层、X 内层；
- Tile 必须完整位于原图内；
- 边缘无法被普通 stride 覆盖时，最后一块回贴到 `length - tile_size`；
- 不 resize，不旋转，不翻转，不产生小 Tile；
- 输出统一为 RGB JPEG，`quality=95`、`subsampling=0`。

默认 X 坐标：

```text
0, 768, 1536, 2304, 3072, 3840, 4608, 5376, 6144, 6912, 7168
```

默认 Y 坐标：

```text
0, 768, 1536, 2304, 3072, 3840, 4436
```

因此默认每个 Source 生成 `11 × 7 = 77` 个 Tile。

TileID 算法：

```text
TileID = SourceID + "_X" + x 的 6 位十进制补零 + "_Y" + y 的 6 位十进制补零
```

示例：

```text
S01_A84F39BC21D7F238_X000768_Y001536
```

### 4. 打开标注工作区

可以在同一次运行中完成 Source/Tile 初始化后进入，也可以选择已有合法 Stage 4 输出目录并点击“进入 Tile 浏览器”。程序会严格读取 `manifests/tiles.csv`，不会通过扫描 JPG 猜测 Tile 顺序。

第一次进入时，如果所有 Source 都不存在 `annotations.json`，程序会统一初始化；如果全部存在，则严格校验并加载。如果只有部分 Source 存在 `annotations.json`，程序会拒绝自动补齐，以免覆盖或丢失已有标注。

### 5. bbox 标注

bbox 正式格式为：

```text
[x1, y1, x2, y2]
```

坐标使用 Tile 像素边界语义：

```text
0 <= x1 < x2 <= tile_width
0 <= y1 < y2 <= tile_height
width  = x2 - x1
height = y2 - y1
```

Scene 浮点坐标会对左/上边界取 `floor`，对右/下边界取 `ceil`，再裁剪到图片边界。bbox 的最小合法尺寸为 `2 × 2` 像素。

四种 bbox 类别：

| 快捷键 | class_name | 中文含义 |
| --- | --- | --- |
| `1` | `metal` | 金属异物 |
| `2` | `stone` | 石块异物 |
| `3` | `rubber` | 橡胶异物 |
| `4` | `other` | 其它异物 |

类别中文名只用于显示；持久化数据始终使用英文 `class_name`。

### 6. 设置 Tile 状态

未标注使用 JSON `null`，不是第五种正式状态，也不会保存为 `unlabeled`。

| status | 含义 | bbox 规则 | trainable |
| --- | --- | --- | --- |
| `positive` | 有 FOD 的有效场地区域 | 至少 1 个 bbox | `true` |
| `negative` | 无 FOD 的有效场地区域 | 必须为 0 个 bbox | `true` |
| `exclude` | 不参与训练的区域 | 必须为 0 个 bbox | `false` |
| `uncertain` | 当前无法可靠判断 | 允许有或没有 bbox | `false` |
| `null` | 未标注 | 可暂存 bbox，但不能完成 | `false` |

程序不会为了切换状态自动删除 bbox，也不会自动猜测 `positive`。`negative` 和 `exclude` 状态禁止新建 bbox；`positive` 的最后一个 bbox 在状态修改前不能删除。

### 7. 自动保存与完成确认

下列操作会自动保存当前 Source 的 `annotations.json`：

- 新建或删除 bbox；
- 修改 bbox 类别；
- 修改或清除 Tile 状态；
- 修改 Notes；
- 按 Space 或点击“验证当前 Tile”。

Notes 使用约 400 ms debounce；导航、返回设置和关闭程序前会强制 flush。保存使用同目录临时文件、`flush()`、`os.fsync()` 和 `os.replace()` 原子替换。

只有状态非空且状态/bbox 组合合法时才能标记 `completed=true`。已经完成的 Tile 如果 bbox、类别、状态或 Notes 发生变化，会立即失效为：

```json
{
  "completed": false,
  "completed_at": null
}
```

### 8. Finalize Worker Package

所有 Tile 完成后，在项目设置页面点击“校验并封装 Worker Package”。程序会重新校验：

- 原图 SHA256 和 Source metadata；
- Tile 文件存在性、尺寸和路径；
- Annotation schema、状态、bbox、完成状态；
- SourceID、TileID 和 manifest 一致性；
- annotator 一致性。

全部通过后，程序从权威 Source/Annotation 数据重建 canonical `sources.csv` 和 `tiles.csv`，生成 `package.json`、验证报告和 SHA256 内容指纹。Finalize 后如果再次修改标注，必须重新 Finalize，否则 Merge 会检测到 fingerprint 不一致。

## 管理员 Merge 流程

1. 在主页面点击“Merge Worker Packages”。
2. 添加一个或多个已经 Finalize 的 Worker Output 根目录。
3. 选择一个新的空 Master Dataset 输出目录。
4. 执行 Merge。

Merge 会先完整验证所有包，并检测跨包冲突，包括：

- 重复 Worker Package；
- 重复 SourceID；
- 不同 SourceID 对应相同原图 SHA256；
- 重复 TileID；
- package schema、统计或内容指纹不一致；
- 缺失、损坏或被修改的图片和 metadata；
- 非空、嵌套或不安全的输出路径。

只有全量预检通过后才会在 staging 中构建 Master Dataset 并提交。不要通过手工复制多个 Worker 的 `sources/` 目录代替 Merge。

## 浏览和标注快捷键

| 操作 | 快捷键 / 鼠标 |
| --- | --- |
| 上一张 Tile | `A` 或 `Left` |
| 下一张 Tile | `D` 或 `Right` |
| 适应窗口 | `F` |
| 放大 | 鼠标滚轮向上或 `-` |
| 缩小 | 鼠标滚轮向下或 `*` |
| 100% | 工具栏“100%”按钮 |
| 平移 | 按住鼠标中键拖动 |
| 新建 bbox | 在空白图片区域按住左键拖动 |
| 选择 bbox | 左键点击已有框 |
| 删除选中 bbox | `Delete` |
| 选择 bbox 类别 | `1` / `2` / `3` / `4` |
| 设置 Negative | `N` |
| 设置 Exclude | `X` |
| 设置 Uncertain | `U` |
| 验证并标记当前 Tile 完成 | `Space` |

`positive` 没有固定快捷键，需要在已有 bbox 后点击状态按钮。左键保留给 bbox 绘制，中键始终用于平移。

## Worker Output 结构

Stage 8 标注并 Finalize 后的典型结构：

```text
worker_output/
├─ package.json
├─ sources/
│  └─ <SourceID>/
│     ├─ source.json
│     ├─ annotations.json
│     └─ tiles/
│        └─ <TileID>.jpg
├─ manifests/
│  ├─ sources.csv
│  └─ tiles.csv
└─ logs/
   ├─ operations.log
   └─ package_validation_*.json
```

Stage 3 不复制原始大图。原图仍由 `input_dir + original_relative_path` 定位，并通过完整 SHA256 防止被替换。

### source.json 核心字段

```text
schema_version, source_id, scene, group_id,
original_filename, original_relative_path, original_sha256,
original_width, original_height,
tile_size, overlap, stride,
annotator_id, created_at
```

`source.json` 和 manifests 不保存用户机器上的原图绝对路径；操作日志可以记录本机路径用于排查。

### annotations.json 顶层字段

```text
schema_version, source_id, scene, annotator_id,
created_by, created_at, updated_at, tiles
```

每个 Tile annotation 包含：

```text
tile_id, x, y, width, height,
status, trainable, objects,
annotator, created_by,
review_status, reviewed_by,
notes, completed, completed_at,
created_at, updated_at
```

对象格式：

```json
{
  "class_name": "metal",
  "bbox": [100, 200, 160, 260]
}
```

`annotations.json` 是人工标注的权威数据源。日常自动保存不会修改 `source.json`、`sources.csv` 或 `tiles.csv`；Finalize 时才重建 canonical manifests。

## Master Dataset 结构

```text
master_dataset/
├─ master.json
├─ merge_report.json
├─ sources/
│  └─ <SourceID>/
│     ├─ source.json
│     ├─ annotations.json
│     └─ tiles/
├─ manifests/
│  ├─ sources.csv
│  └─ tiles.csv
└─ logs/
   └─ merge.log
```

`master.json` 记录 Worker Package 数量、annotator、content fingerprint、场景、Source/Tile 数量及汇总标注统计。

## 数据安全设计

- 所有正式数据在完整预检通过后才开始写入。
- Source、Tile、Annotation、Package 和 Merge 初始化均使用 staging 或原子替换。
- 初始化不会覆盖、删除或自动合并已有数据。
- 不自动去重重复原图，不自动修改冲突 ID。
- 不静默跳过损坏图片、缺失 Tile 或错误 annotation。
- 不把输入/输出绝对路径写入正式 Source/Tile metadata。
- Annotation 保存失败时保留内存数据和 dirty 标记，并阻止离开当前工作区。
- Worker Package 和 Master Dataset 都通过内容指纹与严格 manifest 校验防止静默篡改。

## 开发环境

推荐使用 Conda：

```powershell
conda create -n fod-annotator python=3.11 -y
conda activate fod-annotator
python -m pip install -r requirements.txt
```

启动开发版：

```powershell
python main.py
```

运行测试：

```powershell
python -m py_compile main.py source_manager.py tile_manager.py image_viewer.py annotation_model.py annotation_canvas.py annotation_storage.py package_manager.py merge_manager.py merge_dialog.py
pytest -q
```

测试覆盖 Source、Tile 几何、Manifest 加载、Viewer、bbox、Tile 状态、Annotation 持久化、原子写入失败、Finalize、fingerprint、Merge 冲突和 GUI offscreen 构造。当前基线为 `128 passed`。

## 构建 Windows 发行版

PyInstaller 是构建依赖，不写入普通运行依赖：

```powershell
conda activate fod-annotator
python -m pip install -r requirements-build.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_windows.ps1
```

也可以显式指定 Python 3.11：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_windows.ps1 `
  -PythonPath D:\path\to\python.exe
```

构建脚本只清理项目中的 `build/`、`dist/` 和对应 `release/` 产物，使用 [fod_annotation_tool.spec](fod_annotation_tool.spec) 构建 windowed onedir 发行版，不使用 UPX，也不请求管理员权限。

输出：

```text
release/FOD标注工具/
release/FOD标注工具_Windows.zip
```

## 项目结构

```text
fod-annotation-tool/
├─ main.py                       # PySide6 主窗口和流程编排
├─ source_manager.py             # Source 扫描、SHA256、metadata、staging
├─ tile_manager.py               # Tile 几何、裁剪、manifest 和校验
├─ image_viewer.py               # QGraphicsView 图片缩放与中键平移
├─ annotation_model.py           # bbox、Tile 状态和纯数据规则
├─ annotation_canvas.py          # bbox Scene 交互
├─ annotation_storage.py         # annotations.json 初始化、校验和自动保存
├─ package_manager.py            # Worker Package Finalize 与 fingerprint
├─ merge_manager.py              # 多 Worker 严格 Merge 和 Master 构建
├─ merge_dialog.py               # Merge GUI
├─ tests/                        # pytest 自动测试
├─ packaging/                    # 发行说明模板和冻结运行 smoke harness
├─ requirements.txt              # 开发/测试依赖
├─ requirements-build.txt        # PyInstaller 构建依赖
├─ fod_annotation_tool.spec      # Windows onedir 构建定义
└─ build_windows.ps1             # Windows 自动构建与 ZIP 脚本
```

## 协作约定

- 管理员应确保同一原图只分配给一个 Worker；程序仍会做重复 SHA256 兜底检查。
- 不要多人同时使用同一个 Worker Output。
- 不要手工修改 `source.json`、`annotations.json`、`sources.csv`、`tiles.csv`、`package.json`、`master.json`。
- Worker 应在全部 Tile 完成后 Finalize，并将整个 Worker Output 文件夹交给管理员。
- 管理员必须使用内置 Merge 功能生成 Master Dataset。
- 真实机场图片、Worker Output、Master Dataset、日志和构建产物不应提交到 Git。

## 许可证

当前仓库尚未提供 `LICENSE` 文件。公开发布或用于团队外分发前，请根据项目归属补充适当许可证。

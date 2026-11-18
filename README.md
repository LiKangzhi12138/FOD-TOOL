# FOD Annotation Tool

机场跑道 FOD（Foreign Object Debris，外来物）数据初始化、固定尺寸切片、人工标注、Worker 成果封装以及多 Worker 严格合并桌面工具。

项目面向 Windows 10 / Windows 11，支持多名标注人员分别 clone 或使用 Windows 发行版离线工作。当前工具版本为 `0.2.0`。Source 和 Annotation schema 为 `1.0`，Worker Package 支持 `1.0` 和 `1.1`，新 Master 使用 `1.1`。

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

Merge 界面默认勾选“重复 Source 自动保留第一份（按照添加顺序）”。按列表添加顺序决定优先级，SourceID 与完整原图 SHA256、scene/group 和 Tile 几何必须一致。所有输入包先通过完整性校验，再保留第一份完整 Source 的 metadata、图片及 annotation。存在 annotation 分歧时保留第一份的重点负样本结果。整份 Source 的状态、bbox 和备注均一致时，重点负样本取安全并集。

取消勾选可使用原有严格冲突模式。Python 接口 `merge_worker_packages(..., first_wins=True)` 显式启用 first-wins，默认值 `False` 保留严格模式。

对已授权的 V3 场景/切片冲突，可显式传入 `first_wins=True, resolve_source_variants=True`。该模式按完整原图 SHA256 分组，scene/group 差异采用首份完整 Source。几何冲突优先选择 `1024/256/768` 且包含完整 77 Tile 的版本，多个标准版本按输入顺序选择。仅在同坐标、同宽高且双方均为合法 Negative、空 bbox 时逐 Tile 合并重点负样本。被选中的 SourceID、metadata、图片和 annotation 直接保留原始版本。报告新增 `original_scenes`、`final_scene`、`final_group_id`。

Master 可保留不同独立 Source 的真实切片参数，`validate_master_dataset` 对其逐 Source 校验。Worker 和切片初始化入口仍要求包内统一参数。

成功输出额外包含 `duplicate_resolution_report.csv`（UTF-8 BOM），记录被选择及跳过的 Worker、annotation 分歧和新增重点负样本数。`merge_report.json` 与 `master.json` 的 `duplicate_resolution` 提供输入/最终 Source 数量和去重统计。统计由实际选中的 Tile 重新计算。

`master.json.content_fingerprint` 使用按 POSIX 相对路径排序的文件内容摘要重建。每个文件依次加入 `UTF-8路径 + NUL + 小写SHA256 + LF`，最终计算 SHA256。覆盖图片、Source/Annotation JSON、manifests 和去重报告，排除 `logs/` 与根目录 `master.json`。可调用 `merge_manager.validate_master_dataset(path)` 重新校验格式、annotation、统计、重点负样本及内容指纹。原 Worker 的指纹在 Master 中仅作来源追溯。

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

测试覆盖 Source、Tile 几何、Manifest 加载、Viewer、bbox、Tile 状态、Annotation 持久化、原子写入失败、Finalize、fingerprint、Merge 冲突和 GUI offscreen 构造。重点负样本升级后基线为 160 项，first-wins Merge 增加 13 项，授权的 Source 版本处理增加 7 项，当前结果为 `180 passed`。

若系统临时目录路径过长，可指定项目内的独立短路径运行验收。

```powershell
pytest -q --basetemp=.test_tmp_acceptance -p no:cacheprovider
```

## 构建 Windows 发行版

PyInstaller 是构建依赖，不写入普通运行依赖：

```powershell
conda activate fod-annotator
python -m pip install -r requirements-build.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_windows.ps1 -ReleaseVersion 0.2.0
```

也可以显式指定 Python 3.11：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_windows.ps1 `
  -PythonPath D:\path\to\python.exe
```

构建脚本在 `build/<版本>/`、`dist/<版本>/` 中构建，并将发行包输出到 `release/<版本>/`。已经存在的同版本发行目录或 ZIP 会使构建停止，请为后续发行指定新的版本。构建使用 [fod_annotation_tool.spec](fod_annotation_tool.spec) 定义的 windowed onedir 模式。

输出：

```text
release/0.2.0/FOD标注工具/
release/0.2.0/FOD标注工具_Windows.zip
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

## 重点负样本接口（0.2.0）

在右侧 Tile 状态分组中，Negative Tile 可勾选“重点负样本（训练时必须保留）”。标记立即原子保存，切换 Tile 和重启后恢复。清除状态或改为其他状态会自动清除标记。该标记独立于四种检测类别和四种 Tile 状态。

唯一持久化来源为 Worker 或 Master 根目录下的 `manifests/protected_negatives.csv`。Annotation JSON schema 保持 `1.0`。

```csv
scene,source_id,tile_id
S01,S01_A84F39BC21D7F238,S01_A84F39BC21D7F238_X000768_Y001536
```

文件使用 UTF-8 BOM（Python `utf-8-sig`）与 CRLF 换行，列顺序固定为 `scene,source_id,tile_id`。每条记录对应一张 Tile。顺序遵循 Tile manifest，重复记录直接报错。取消全部勾选后，文件保留表头且没有数据行。

### 草稿与正式交付校验

工作区允许对尚未完成的 Negative 先勾选。草稿记录必须匹配已存在 Tile 的 scene 和 SourceID，并满足 `negative`、`trainable=true`、`objects=[]`。Finalize 和 Merge 进一步要求 `completed=true`。非法记录会报告 SourceID 和 TileID，用户需要修正实际数据后重试。

重点负样本保存失败时，dirty 标记会保留。现有导航、返回和关闭前的 flush 会重试保存，持续失败会阻止离开工作区。状态变化时先写入清除后的清单，再提交 annotation。若后一步写入失败，磁盘可能暂时保留原 Negative annotation 和已清除的标记，内存中的状态变更仍为 dirty，重试后完成提交。突然断电后的跨文件恢复遵循这两个独立原子文件的实际内容。

### Worker 格式兼容与完整性

- 历史 `1.0` Worker 缺少清单时按空集读取，继续使用原来的内容指纹算法。
- 含清单的 Worker（包括只有表头的空清单）在 Finalize 时声明 schema `1.1`。清单完整字节的 SHA256 被加入 `content_fingerprint`，标签为 `manifests/protected_negatives.csv`。
- `1.1` 包缺少清单、清单被修改、存在无效记录或与 annotation 冲突时，校验失败。重新 Finalize 也会拒绝将缺失清单的 `1.1` 包降级。
- `1.0` 包出现未声明的清单时，必须使用新版工具重新 Finalize。
- 旧版工具会拒绝 `1.1` Worker。含重点负样本的数据应通过新版 Finalize 和 Merge 处理，以保留标记。

### Master 与后续数据划分工具

新版 Merge 对每个 Worker 逐条验证，并沿用原有跨包 SourceID、原图 SHA256、TileID 冲突检测。成功后在 Master 输出完整清单，同时在 `master.json` 写入以下描述。

```json
{
  "schema_version": "1.1",
  "protected_negatives": {
    "path": "manifests/protected_negatives.csv",
    "sha256": "完整64位小写SHA256",
    "count": 20
  }
}
```

后续划分工具应执行以下步骤。

1. 读取 `master.json`，对声明的 CSV 完整字节重新计算 SHA256，与 `protected_negatives.sha256` 比较，并验证 count。
2. 用 `utf-8-sig`、`newline=""` 和 `csv.DictReader` 读取固定三列。
3. 通过 `source_id + tile_id` 关联 `sources/<SourceID>/annotations.json` 与 `manifests/tiles.csv`，逐条检查 scene、归属、Negative、completed、trainable 和空 objects。
4. 保留所有 Positive 与重点 Negative，对普通 Negative 执行抽样。
5. 将清单 SHA256 和 Master 标识写入划分或导出报告，绑定实际使用的输入。

历史 `1.0` Master 没有该文件和描述时按无重点负样本处理。新版 `1.1` Master 必须同时具备清单与描述，包括空清单。`1.0` Master 如果单独出现清单，应先确认其来源并重新通过新版 Merge 生成有完整性声明的 Master。

Master 的新增 SHA256 描述绑定清单内容。旧版 Master 没有覆盖此文件的整体 fingerprint，因此后续工具必须显式验证该描述。该校验属于数据完整性检查，发行来源的可信性仍由交付流程保证。

## 许可证

当前仓库尚未提供 `LICENSE` 文件。公开发布或用于团队外分发前，请根据项目归属补充适当许可证。

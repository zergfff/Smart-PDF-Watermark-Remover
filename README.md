# Smart PDF Watermark Remover（智能 PDF 去水印工具）

一键清除 PDF 中反复出现的水印（文本 / 图片 / Logo），并移除文档加密与权限限制（禁止打印、复制等）。

提供两种用法：

| 模式 | 入口 | 适用场景 |
|---|---|---|
| 🖥️ GUI 桌面版 | `python main.py` | 自动频率分析 + 交互勾选确认，日常手动清理 |
| 🔧 CLI 命令行版 | `python pdf_dewatermark.py` | 按关键词精确删除，适合批处理 / 脚本化 |

## ✨ 功能特性

* **智能频率分析**（GUI）：自动统计全文档元素，识别出现频率超过阈值（默认 30%）的疑似水印。
* **交互式确认**（GUI）：删除前预览并勾选，支持过滤、悬停定位原文档位置。
* **内容流级删除**：不是"红色矩形遮盖"。直接解析 PDF 内容流（Contents），只删除绘制水印的 `BT…ET` 文本块或图片对象本身，**完全不触碰正文/图形**。
* **图片水印移除**：自动检测被多数页实际绘制、且渲染面积较大的重复图片（含 Form XObject 容器包装），删除其 `Do` 绘制操作与资源项；页眉/Logo 等小图不会误删（可用 `--image-xref` 手动指定）。
* **PDF 拖拽打开**（GUI）：把 PDF 文件直接拖进窗口即可打开。
* **字符串感知解析**：扫描内容流时追踪 `(...)` 字符串与 `<...>` 十六进制串的嵌套状态，只在字符串之外识别 `BT/ET`，正文里出现 "BT" 字样也绝不会被误删。
* **XObject 递归处理**：水印嵌在 Form XObject 里也能清除。
* **一键去权限**：保存时 `encryption=False`，彻底移除加密字典与权限位（禁止打印/复制/修改等）。
* **自动验证**（CLI）：输出后自动用 PyMuPDF 逐页检查水印残留；配套 `verify_clean.py` 可做像素级渲染对比。
* **多线程加速**（GUI）：多进程并行分析文档结构。

## 🚀 快速开始

### 环境要求
- Python 3.8 或更高版本
- Windows / macOS / Linux

### 安装
```bash
pip install -r requirements.txt
```

### GUI 桌面版
```bash
python main.py
```
载入 PDF → 分析水印 → 勾选确认 → 保存结果（默认 `cleaned_原文件名.pdf`）。

### CLI 命令行版
```bash
# 精确删除命中关键词的文本块 + 移除加密/权限
python pdf_dewatermark.py in.pdf -o out.pdf -k "C2 - Confidential"

# 多关键词（子串匹配，忽略大小写）
python pdf_dewatermark.py in.pdf -o out.pdf \
    -k "Confidential" -k "exclusive use of Sichuan"

# 验证结果：加密移除？水印残留？正文保留？像素差异？
python verify_clean.py out.pdf -k "Confidential" --body "29-11-67" \
    --orig in.pdf --pages 1 26 104
```
不加 `-o` 时输出到 `<原文件名>_cleaned.pdf`；`--verify`（默认开启）会在输出后自动检查残留。

### 批处理整个文件夹
```bash
python batch_clean.py   # 自动识别 原始/ 里每份 PDF 的水印字符串 → 内容流级清理 → 保存到 处理后/
```

## 🧪 真实案例

`29-11-67 Rev3.pdf`（104 页，A4）：每页同一位置以约 52° 斜向绘制
`C2 - Confidential - Downloaded by: Liu Changfa on 07-Jan-2026 for the exclusive use of Sichuan Airlines under SLS Terms & Conditions`，
并带 AES-128（R=4）加密与权限限制（P=-60，禁止打印/复制等）。

清理结果：
- ✅ 104 页水印全部清除，残留 0 页
- ✅ 加密字典与权限位完全移除
- ✅ 正文像素级零损伤：抽查 4 页渲染对比，差异像素仅约 1.6%，且差异区域恰好是水印斜带

## 📦 打包 EXE（可选）

```bash
pip install pyinstaller
# 单文件版（方便分享，启动需解压，约 3s）
pyinstaller -w -F main.py --name Extreme_PDF_Cleaner
# 目录版（启动最快 ~0.3s，exe 与 _internal 目录需放一起）
pyinstaller -w main.py --name Extreme_PDF_Cleaner
```

> 💡 **启动速度**：想要秒开请用**目录版**（免解压）；单文件版适合单个分享但每次启动要解压 70MB。GitHub Release 同时附带单文件版与目录版 zip。

## 依赖

```
pip install PyMuPDF PyQt6 pikepdf xxhash
```
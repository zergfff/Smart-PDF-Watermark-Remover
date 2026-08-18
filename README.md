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
"`C2 - Confidential - Downloaded by: Liu Changfa on 07-Jan-2026 for the
exclusive use of Sichuan Airlines under SLS Terms & Conditions`"，并带 AES-128（R=4）加密
与权限限制（P=-60，禁止打印/复制等）。清理结果：104 页水印全部清除、残留 0 页、
加密字典与权限位完全移除、正文像素级零损伤（差异 1.6% 仅在水印斜带）。

`HST85-2024.pdf`（16 页，中文 CID 字体水印"以正式出版文本为准"，包在旋转 35° 的
Form XObject 里）：几何签名删除 16 页全清、残留 0/16、正文零损伤。

---

## 🤖 Agent（AI 助手）调用方式

本工具可直接被 AI Agent（Hermes / Claude Code / Codex / Cursor 等）驱动，
形成「软件找水印 → 编号 → 你确认号 → Agent 删除」的人机闭环：

```bash
# 1) 列出疑似水印并编号（带每个候选的「去除前/去除后」对比预览图）
python dewatermark_agent.py list 文件.pdf --before-after-dir ./ba
#   输出形如：
#   [1] TEXT  '以正式出版文本为准'  (36.0pt, 16页, bbox=..., 旋转-35.0°)
#   [3] IMAGE xref=262 (26页, bbox=...)
#   并生成 01_before.png / 01_after.png ...（同一区域 前/后 对比，供看图确认）

# 2) 按编号删除（只删该候选，自动复检）
python dewatermark_agent.py remove 文件.pdf --select 1,3
#   --select all  删除全部候选
```

**推荐 Agent 工作流：**
1. Agent 运行 `dewatermark_agent.py list <pdf> --before-after-dir ./ba`
2. Agent 读出编号候选 + 每张「前/后」对比图，贴给用户并配文，如：
   `候选 #1（水印：斜体大字"以正式出版文本为准"）—— 前/后对比`
3. 用户看图报号（如`3 号`）
4. Agent 运行 `remove 文件.pdf --select 3`，自动删除并复检，把结果 PDF 交付用户

> 对 Agent 的提示：
> - `list --json` 输出机器可读候选列表（含 id/kind/text/size/bbox/origin/rot/count/pages/xref），便于程序化解析
> - `list --previews-dir` 只出候选区域截图（不跑删除）；`--before-after-dir` 额外生成「前/后」对比
> - 文本水印按「关键词 + 几何(字号/旋转/起点)」删除、图片水印按 xref 删除，
>   内置 CID/中文乱码字体水印处理，绝大多数 PDF 无需额外传参

## 依赖

```
pip install PyMuPDF PyQt6 pikepdf xxhash
```
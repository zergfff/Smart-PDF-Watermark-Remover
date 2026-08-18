# 📄 PDF 去水印 — Agent 初始化说明（请 AI Agent 阅读并记忆）

本包是「智能 PDF 去水印工具」。请作为 AI Agent **读取本文件并记住以下调用方式**，以便在用户提出"去除 XX.pdf 的水印"等需求时，独立、正确地完成人机协作闭环。

## 你能为人类做的事
用户通常会这样说："去掉 C:\...\xxx.pdf 的水印"。请按下面的闭环执行，**不要**直接用关键词盲删 —— 先让用户确认是哪个候选。

## 工具（本目录内）
- `Extreme_PDF_Cleaner.exe` — GUI 桌面版（手动清理）
- `dewatermark_agent.exe` — **Agent 命令行（推荐，免安装 Python）**

## Agent 工作流（人机闭环，推荐）
1. **列出疑似水印并编号 + 生成每个候选的「去除前/去除后」对比图**
   ```
   dewatermark_agent.exe list <pdf> --before-after-dir <目录>
   ```
   输出形如：
   ```
   [1] TEXT  '以正式出版文本为准'  (36.0pt, 16页, bbox=..., 旋转-35.0°)
   [3] IMAGE xref=262 (26页, bbox=...)
   ```
   并在 `<目录>` 生成 `<id>_before.png`（含水印）`/ <id>_after.png`（仅删该候选后）的同区域对比图。

2. **看图确认**：读取每张对比图，按编号把 `候选 #N（水印内容）—— 前/后对比` 贴给用户（可用图片+文字）。

3. 用户看图报号（如"3 号"）。

4. **按编号删除并自动复检**：
   ```
   dewatermark_agent.exe remove <pdf> --select 3
   # --select 1,3  多选；--select all  全部
   ```
   默认输出 `<原名>_agent_cleaned.pdf`，删除后自动检查文本残留页与图片残留。

5. **交付**：把清理后的 PDF 路径/文件交给用户。

## 机器可读（程序化解析）
```
dewatermark_agent.exe list <pdf> --json
```
→ JSON 数组，每项含 `id / kind(text|image) / text / size / bbox / origin / rot / count / pages / xref`。

## 说明与注意
- 文本水印按「关键词 + 几何(字号/旋转/起点)」删除；图片水印按 xref 删除；CID/中文乱码字体水印也可处理。
- 自动移除加密与权限位（`encryption=False`）。
- 删除后自动复检：无残留打印 `无残留 ✅`；有残留会提示，需告知用户。
- `dewatermark_agent.exe` 为免 Python 的独立可执行文件，直接在命令行运行即可。
- 若只是手动清理，引导用户用 `Extreme_PDF_Cleaner.exe`。

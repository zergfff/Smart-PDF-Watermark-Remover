import sys
import os
import json
import math
import tempfile
import uuid
import xxhash
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor
from PyQt6.QtWidgets import (QApplication, QMainWindow, QPushButton, QVBoxLayout, QHBoxLayout,
                             QWidget, QFileDialog, QLabel, QProgressBar, QMessageBox, QTextEdit,
                             QDialog, QCheckBox, QScrollArea, QFrame, QSpinBox, QLineEdit, QComboBox,
                             QMenu, QScrollBar, QRubberBand)
from PyQt6.QtGui import QPixmap, QImage, QTextCursor, QPainter, QPen, QColor, QPalette
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QEvent, QSize, QRect, QPoint

# --- 环境适配 ---
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"

# 重型库(占位)：后台线程延迟加载，加快窗口出现
fitz = None
pikepdf = None
_dw = None

def _load_heavy_libs():
    global fitz, pikepdf, _dw
    import fitz
    import pikepdf
    import pdf_dewatermark as _dw

# --- 多语言配置 ---
TRANSLATIONS = {
    "zh": {
        "title": "Extreme PDF Cleaner - 极速清理工具",
        "open": "📂 载入 PDF",
        "clean": "⚡ 分析水印",
        "save": "💾 保存结果",
        "settings": "⚙️ 设置",
        "page": "页",
        "orig": "原图预览",
        "cleaned": "清洗预览",
        "dialog_title": "确认疑似水印 - 请手动勾选并悬停预览位置",
        "all": "全选",
        "none": "清空",
        "search": "🔍 过滤内容...",
        "ok": "确定清理勾选项",
        "img_header": "Repeated Images (Logo)",
        "txt_header": "Repeated Text",
        "count": "次数",
        "del": "",
        "preview_tip": "💡 鼠标指向左侧图片查看原文档位置",
        "set_title": "软件设置",
        "set_ratio": "疑似水印识别比例 (10-100%):",
        "set_lang": "语言 (Language):",
        "set_keywords": "手动水印关键词(逗号分隔):",
        "set_save": "保存设置",
        "cancel": "⏹ 停止",
        "apply_all": "应用到其余所有文件",
        "recent": "最近打开",
        "zoom_in": "放大",
        "zoom_out": "缩小",
        "fit_width": "适合宽度",
        "fit_page": "适合页面",
        "analyzing": "分析正在进行中，请等待完成…",
        "batch_done": "批量处理完成",
        "batch_cancel": "已取消",
        "verify_ok": "复检通过：无残留",
        "verify_warn": "复检发现残留"
    },
    "en": {
        "title": "Extreme PDF Cleaner",
        "open": "📂 Load PDF",
        "clean": "⚡ Analyze Watermark",
        "save": "💾 Save Result",
        "settings": "⚙️ Settings",
        "page": "Page",
        "orig": "Original Preview",
        "cleaned": "Cleaned Preview",
        "dialog_title": "Confirm Watermarks - Hover to Preview",
        "all": "Select All",
        "none": "Clear",
        "search": "🔍 Filter...",
        "ok": "Apply Selection",
        "img_header": "Repeated Images (Logo)",
        "txt_header": "Repeated Text",
        "count": "Count",
        "del": "",
        "preview_tip": "💡 Hover over items to see location in document",
        "set_title": "Settings",
        "set_ratio": "Watermark Ratio (10-100%):",
        "set_lang": "Language:",
        "set_keywords": "Manual watermark keywords (comma separated):",
        "set_save": "Save Settings",
        "cancel": "⏹ Stop",
        "apply_all": "Apply to all remaining files",
        "recent": "Recent Files",
        "zoom_in": "Zoom In",
        "zoom_out": "Zoom Out",
        "fit_width": "Fit Width",
        "fit_page": "Fit Page",
        "analyzing": "Analysis in progress, please wait...",
        "batch_done": "Batch processing finished",
        "batch_cancel": "Cancelled",
        "verify_ok": "Verify passed: no residual",
        "verify_warn": "Verify found residual"
    }
}

# --- 配置与语言 ---
def config_path():
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ExtremePDFCleaner")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config.json")

def load_config():
    cfg = {"lang": None, "ratio": 30, "keywords": [], "recent_files": [], "last_dir": ""}
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            for k in cfg:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    return cfg

def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def detect_system_lang():
    """根据系统 UI 语言决定首次语言：中文系统 → zh，其他 → en。"""
    try:
        import ctypes
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        if (langid & 0x3FF) == 0x04:  # LANG_CHINESE
            return "zh"
    except Exception:
        pass
    return "en"

def apply_dark_mode(app, enable):
    """跟随系统的深色模式（注册表 AppsUseLightTheme）。"""
    try:
        import ctypes
        key = ctypes.windll.advapi32.RegGetValueW
        # 读取是否浅色主题
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            light = winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 1
        dark = (not light) if enable is None else enable
    except Exception:
        dark = False
    app.setStyle("Fusion")
    if dark:
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window, QColor(35, 38, 41))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(238, 238, 238))
        pal.setColor(QPalette.ColorRole.Base, QColor(30, 33, 36))
        pal.setColor(QPalette.ColorRole.AlternateBase, QColor(42, 45, 48))
        pal.setColor(QPalette.ColorRole.Text, QColor(238, 238, 238))
        pal.setColor(QPalette.ColorRole.Button, QColor(48, 51, 53))
        pal.setColor(QPalette.ColorRole.ButtonText, QColor(238, 238, 238))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(61, 139, 253))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
        pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(35, 38, 41))
        pal.setColor(QPalette.ColorRole.ToolTipText, QColor(238, 238, 238))
        app.setPalette(pal)
    else:
        app.setPalette(app.style().standardPalette())

# --- 设置对话框 ---
class SettingsDialog(QDialog):
    def __init__(self, current_ratio, current_lang, current_keywords, scale, parent=None):
        super().__init__(parent)
        self.scale = scale
        self.t = TRANSLATIONS[current_lang]
        self.setWindowTitle(self.t["set_title"])
        self.setFixedWidth(int(340 * scale))
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self.t["set_ratio"]))
        self.ratio_spin = QSpinBox()
        self.ratio_spin.setRange(10, 100)
        self.ratio_spin.setValue(current_ratio)
        self.ratio_spin.setSuffix("%")
        layout.addWidget(self.ratio_spin)
        
        layout.addWidget(QLabel(self.t["set_lang"]))
        self.lang_combo = QComboBox()
        self.lang_combo.addItem("English", "en")
        self.lang_combo.addItem("中文", "zh")
        index = self.lang_combo.findData(current_lang)
        self.lang_combo.setCurrentIndex(index if index >= 0 else 0)
        layout.addWidget(self.lang_combo)

        layout.addWidget(QLabel(self.t["set_keywords"]))
        self.keywords_edit = QLineEdit(", ".join(current_keywords))
        self.keywords_edit.setPlaceholderText("Confidential, Internal Use Only")
        layout.addWidget(self.keywords_edit)
        
        self.btn_save = QPushButton(self.t["set_save"])
        self.btn_save.clicked.connect(self.accept)
        layout.addWidget(self.btn_save)

    def get_values(self):
        kws = [k.strip() for k in self.keywords_edit.text().replace("，", ",").split(",") if k.strip()]
        return self.ratio_spin.value(), self.lang_combo.currentData(), kws

# --- 1. 底层计算逻辑 ---
def is_bbox_similar(bbox1, bbox2, tolerance=2.0):
    """判断两个 bbox 是否在容差范围内相似"""
    return all(abs(a - b) <= tolerance for a, b in zip(bbox1, bbox2))

def analyze_chunk_worker(file_path, page_indices):
    import fitz  # 子进程内确保加载（主进程是延迟加载）
    results = []
    errors = []
    doc = None
    try:
        doc = fitz.open(file_path)
        for i in page_indices:
            try:
                page = doc[i]
                rect = page.rect
                pw, ph = round(rect.width, 1), round(rect.height, 1)
                page_data = {'index': i, 'size_key': (pw, ph), 'imgs': [], 'texts': []}
                for img in page.get_images(full=True):
                    try:
                        pix = fitz.Pixmap(doc, img[0])
                        # 如果图片太大，采样计算哈希以节省内存和时间
                        if pix.size > 1024 * 1024: # > 1MB
                            h = xxhash.xxh64(pix.samples[::4]).hexdigest()
                        else:
                            h = xxhash.xxh64(pix.samples).hexdigest()
                        page_data['imgs'].append({'hash': h, 'xref': img[0]})
                    except Exception as e:
                        errors.append(f"Page {i} image error: {str(e)}")
                        continue
                blocks = page.get_text("rawdict")["blocks"]
                for b in blocks:
                    if b["type"] != 0: continue
                    for line in b["lines"]:
                        spans = line["spans"]
                        content = "".join(ch.get("c", "")
                                           for sp in spans for ch in sp.get("chars", [])).strip()
                        if len(content) > 1:
                            bbox = tuple([round(v, 1) for v in line["bbox"]])
                            size = round(max(sp.get("size", 0) for sp in spans), 1)
                            origin = None
                            rot = 0.0
                            for sp in spans:
                                chs = sp.get("chars") or []
                                if chs and chs[0].get("origin"):
                                    origin = tuple(round(v, 1) for v in chs[0]["origin"])
                                # 旋转角：用前两个字符原点算基线方向（fitz y 向下）
                                if len(chs) >= 2 and chs[0].get("origin") and chs[1].get("origin"):
                                    o0, o1 = chs[0]["origin"], chs[1]["origin"]
                                    rot = round(math.degrees(math.atan2(o1[1] - o0[1], o1[0] - o0[0])), 1)
                                if origin is not None:
                                    break
                            page_data['texts'].append({'text': content, 'bbox': bbox,
                                                       'size': size, 'origin': origin,
                                                       'rot': rot})
                results.append(page_data)
            except Exception as e:
                errors.append(f"Page {i} general error: {str(e)}")
    except Exception as e:
        errors.append(f"Worker file open error: {str(e)}")
    finally:
        if doc: doc.close()
    return results, errors

# --- 1.5 内容流级删除（替代红色遮盖 redaction，避免误删正文） ---
import re as _re

def _parse_strings_in_block(block: bytes) -> list:
    """从 BT..ET 块中提取所有 Tj/TJ 字符串（明文 (...) 与 hex <...>）。"""
    out = []
    i = 0
    n = len(block)
    while i < n:
        c = block[i]
        if c == ord("("):
            j = i + 1
            depth = 1
            buf = bytearray()
            while j < n and depth > 0:
                ch = block[j]
                if ch == ord("\\"):
                    if j + 1 >= n:
                        break
                    nxt = block[j + 1]
                    mapping = {ord("n"): 10, ord("r"): 13, ord("t"): 9,
                               ord("b"): 8, ord("f"): 12, ord("("): 40,
                               ord(")"): 41, ord("\\"): 92}
                    if nxt in mapping:
                        buf.append(mapping[nxt])
                    elif nxt == ord("0") and j + 3 < n and all(
                            ord("0") <= block[j + k] <= ord("7") for k in (1, 2, 3)):
                        buf.append(int(block[j + 1:j + 4], 8))
                        j += 3
                    else:
                        buf.append(nxt)
                    j += 2
                    continue
                if ch == ord("("):
                    depth += 1
                elif ch == ord(")"):
                    depth -= 1
                    if depth == 0:
                        break
                buf.append(ch)
                j += 1
            out.append(bytes(buf))
            i = j + 1
        elif c == ord("<"):
            j = i + 1
            while j < n and block[j] != ord(">"):
                j += 1
            hexpart = _re.sub(rb"\s", b"", block[i + 1:j])
            if len(hexpart) % 2:
                hexpart += b"0"
            try:
                out.append(bytes.fromhex(hexpart.decode("ascii")))
            except ValueError:
                out.append(b"")
            i = j + 1
        else:
            i += 1
    return out


def _find_ops(stream: bytes):
    """扫描内容流，返回字符串之外的 (BT/ET, 位置) 操作符。
    跳过 (...) 字符串（含转义/嵌套）、<...> 十六进制串与 <<...>> 字典的字节，
    避免把正文文本里出现的大写 BT/ET 误判为操作符。"""
    ops = []
    i, n = 0, len(stream)
    while i < n:
        c = stream[i]
        if c == 0x28:  # '(' 字符串
            depth = 1
            i += 1
            while i < n and depth:
                ch = stream[i]
                if ch == 0x5C:  # 反斜杠转义
                    i += 2
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                i += 1
            continue
        if c == 0x3C:  # '<' 十六进制串或字典
            if i + 1 < n and stream[i + 1] == 0x3C:
                j = stream.find(b">>", i + 2)
                i = n if j < 0 else j + 2
            else:
                j = stream.find(b">", i + 1)
                i = n if j < 0 else j + 1
            continue
        if c in (0x42, 0x45) and stream[i:i + 2] in (b"BT", b"ET"):
            ops.append((stream[i:i + 2], i))
            i += 2
            continue
        i += 1
    return ops


def _split_bt_et_pieces(stream: bytes):
    """按字符串感知的 BT/ET 切块，返回保持原始顺序的片段列表。
    每个片段是 ('block', bytes) 或 ('rest', bytes)。
    删除水印块时必须按此顺序重建，否则 rest/block 交错位置错乱，
    会把相邻 token 拼成 'ETq'/'ETBT' 之类的非法操作符。"""
    ops = _find_ops(stream)
    pieces = []
    i = 0
    k, n_ops = 0, len(ops)
    while k < n_ops:
        op, p = ops[k]
        if op == b"BT":
            e_pos, j = None, k + 1
            while j < n_ops:
                if ops[j][0] == b"ET":
                    e_pos = ops[j][1]
                    break
                j += 1
            if e_pos is None:  # 未闭合 BT：把剩余内容整个当作块
                if p > i:
                    pieces.append(("rest", stream[i:p]))
                pieces.append(("block", stream[p:]))
                i = len(stream)
                break
            if p > i:
                pieces.append(("rest", stream[i:p]))
            pieces.append(("block", stream[p:e_pos + 2]))
            i = e_pos + 2
            k = j + 1
        else:
            k += 1
    if i < len(stream):
        pieces.append(("rest", stream[i:]))
    return pieces


def _split_bt_et_blocks(stream: bytes):
    """按字符串感知的 BT/ET 把内容流切成块。返回 (块列表, 非块内容拼接)。"""
    pieces = _split_bt_et_pieces(stream)
    blocks = [b for kind, b in pieces if kind == "block"]
    tail = b"".join(b for kind, b in pieces if kind == "rest")
    return blocks, tail


def _block_matches(block: bytes, keywords: list) -> bool:
    """块内任一字符串命中任一关键词（子串匹配，忽略大小写）。"""
    if not keywords:
        return False
    for s in _parse_strings_in_block(block):
        ls = s.lower()
        for kw in keywords:
            if kw.lower() in ls:
                return True
    return False


def _strip_watermark_stream(data: bytes, keywords: list):
    """从单个内容流中删除命中关键词的 BT..ET 块。
    返回 (新流或 None 表示流已空, 删除块数)。"""
    pieces = _split_bt_et_pieces(data)
    kept = []
    removed = 0
    for kind, b in pieces:
        if kind == "block" and _block_matches(b, keywords):
            removed += 1
        else:
            kept.append(b)
    if removed == 0:
        return data, 0
    new_data = b"".join(kept)  # 保持原始顺序；rest 与保留块交错位置不变
    # 若删除后只剩 q/Q 之类的空壳，整个流移除
    stripped = new_data.replace(b"q", b"").replace(b"Q", b"").strip()
    if not stripped:
        return None, removed
    return new_data, removed


def _wm_process_page(pdf, page, keywords: list) -> int:
    """处理一页的 Contents（数组或单流），返回删除的块数。"""
    total = 0
    contents = page.Contents
    if contents is None:
        return 0
    if isinstance(contents, pikepdf.Array):
        new_arr = pikepdf.Array()
        for s in contents:
            if s is None:
                new_arr.append(s)
                continue
            data = s.read_bytes()
            new_data, removed = _strip_watermark_stream(data, keywords)
            total += removed
            if removed == 0:
                new_arr.append(s)  # 未改动，原样保留
                continue
            if new_data is None:
                continue  # 整个流被删
            # 只对改动过的流重写；不传 filter，pikepdf 保留原压缩方式
            s.write(new_data)
            new_arr.append(s)
        if len(new_arr) == 0:
            # 页面没有内容了：给一个空流避免损坏
            page.Contents = pikepdf.Stream(pdf, b"")
        else:
            page.Contents = new_arr
    else:
        data = contents.read_bytes()
        new_data, removed = _strip_watermark_stream(data, keywords)
        total += removed
        if removed == 0:
            return total
        if new_data is None:
            page.Contents = pikepdf.Stream(pdf, b"")
        else:
            contents.write(new_data)
    return total


def _wm_process_xobjects(resources, keywords: list) -> int:
    """递归处理 Resources/XObject 中的 Form 流。"""
    total = 0
    if resources is None or "/XObject" not in resources:
        return 0
    xo = resources["/XObject"]
    for name in list(xo.keys()):
        obj = xo[name]
        if obj is None:
            continue
        if "/Subtype" in obj and str(obj["/Subtype"]) == "/Form":
            data = obj.read_bytes()
            new_data, removed = _strip_watermark_stream(data, keywords)
            total += removed
            if removed:
                if new_data is None:
                    del xo[name]
                else:
                    obj.write(new_data)
            if "/Resources" in obj:
                total += _wm_process_xobjects(obj["/Resources"], keywords)
    return total


# --- 2. 交互确认对话框 ---
class EnhancedWatermarkDialog(QDialog):
    def __init__(self, img_data, text_blocks, doc, lang="en", scale=1.0, parent=None):
        super().__init__(parent)
        self.t = TRANSLATIONS[lang]
        self.setWindowTitle(self.t["dialog_title"])
        self.doc = doc
        self.scale = scale
        self.img_boxes = {}; self.text_line_boxes = []; self.text_cards = []
        
        available_geom = QApplication.primaryScreen().availableGeometry()
        self.resize(int(available_geom.width() * 0.95), int(available_geom.height() * 0.85))
        
        main_layout = QHBoxLayout(self)
        left_container = QWidget(); left_container.setFixedWidth(int(450 * scale))
        left_side = QVBoxLayout(left_container)
        
        tool_layout = QHBoxLayout()
        btn_all = QPushButton(self.t["all"]); btn_none = QPushButton(self.t["none"])
        btn_all.clicked.connect(self.select_all); btn_none.clicked.connect(self.select_none)
        self.search_bar = QLineEdit(); self.search_bar.setPlaceholderText(self.t["search"])
        self.search_bar.textChanged.connect(self.filter_items)
        tool_layout.addWidget(btn_all); tool_layout.addWidget(btn_none); tool_layout.addWidget(self.search_bar)
        left_side.addLayout(tool_layout)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content_widget = QWidget(); self.scroll_layout = QVBoxLayout(content_widget)

        IMG_STAT_STYLE = "color: #e74c3c; font-weight: bold; font-size: 9pt;"
        TXT_STAT_STYLE = "color: #3498db; font-weight: bold; font-size: 9pt;"

        if img_data:
            header_img = QLabel(f"<b>{self.t['img_header']}</b>")
            header_img.setStyleSheet("color: #e74c3c;")
            self.scroll_layout.addWidget(header_img)
            for h, info in img_data.items():
                QApplication.processEvents() # 保持 UI 响应
                frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
                l = QHBoxLayout(frame); l.setContentsMargins(5, 5, 5, 5)
                cb = QCheckBox(""); cb.setChecked(False); self.img_boxes[h] = cb
                l.addWidget(cb)
                try:
                    pix = fitz.Pixmap(self.doc, info['xref'])
                    # 如果原图很大，先缩放再转 QImage。
                    # 注意：新版 pymupdf 的 Pixmap(pix, matrix) 构造器有兼容问题，
                    # 用 scale() 原地缩放；失败则直接用原图（预览由 QPixmap.scaled 兜底）
                    if pix.width > 400 or pix.height > 400:
                        try:
                            zoom = min(400 / pix.width, 400 / pix.height)
                            pix.scale(zoom, zoom)
                        except Exception:
                            pass
                    qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
                    lab = QLabel()
                    lab.setPixmap(QPixmap.fromImage(qimg).scaled(int(150*scale), int(80*scale), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                except Exception as e:
                    # 预览失败也要保留可勾选条目，避免候选静默消失
                    print(f"Error loading img preview: {e}")
                    lab = QLabel("⚠ preview failed")
                lab.setProperty("loc_info", {"page": info['sample_page'], "bbox": info['sample_bbox'], "type": "img",
                                             "pages": info.get('pages', [info['sample_page']])})
                lab.installEventFilter(self)
                l.addWidget(lab); l.addStretch()
                l.addWidget(QLabel(f"<span style='{IMG_STAT_STYLE}'>{self.t['count']}: {info['count']}</span>"))
                self.scroll_layout.addWidget(frame)

        if text_blocks:
            header_txt = QLabel(f"<b>{self.t['txt_header']}</b>")
            header_txt.setStyleSheet("color: #3498db;")
            self.scroll_layout.addWidget(header_txt)
            for key, info in text_blocks.items():
                QApplication.processEvents() # 保持 UI 响应
                frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
                row_layout = QHBoxLayout(frame); row_layout.setContentsMargins(5, 5, 5, 5)
                cb = QCheckBox(); cb.setChecked(False)
                row_layout.addWidget(cb)
                try:
                    sample_page = self.doc[info['sample_page']]
                    bbox = info.get('bbox', (0, 0, 0, 0))
                    clip_rect = fitz.Rect(bbox) + (-10, -5, 10, 5)
                    # 降低预览图精度以加快速度
                    pix = sample_page.get_pixmap(clip=clip_rect, matrix=fitz.Matrix(1.5, 1.5))
                    qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
                    img_lab = QLabel()
                    img_lab.setPixmap(QPixmap.fromImage(qimg).scaled(int(220*scale), int(60*scale), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    # 设置 type 为 txt
                    img_lab.setProperty("loc_info", {"page": info['sample_page'], "bbox": bbox, "type": "txt",
                                                     "pages": info.get('pages', [info['sample_page']])})
                    img_lab.installEventFilter(self)
                    row_layout.addWidget(img_lab)
                except Exception as e:
                    print(f"Error loading text preview: {e}")
                row_layout.addStretch()
                row_layout.addWidget(QLabel(f"<span style='{TXT_STAT_STYLE}'>{self.t['count']}: {info['count']}</span>"))
                self.text_line_boxes.append({'checkbox': cb, 'content': key[0], 'bbox': info.get('bbox', (0, 0, 0, 0)), 'size': key[1],
                                             'origin': info.get('origin'), 'rot': info.get('rot', 0.0)})
                self.text_cards.append((frame, key[0].lower()))
                self.scroll_layout.addWidget(frame)

        scroll.setWidget(content_widget); left_side.addWidget(scroll)
        self.apply_all_cb = QCheckBox(self.t["apply_all"])
        self.apply_all_cb.setChecked(False)
        left_side.addWidget(self.apply_all_cb)
        btn_ok = QPushButton(self.t["ok"]); btn_ok.clicked.connect(self.accept)
        btn_ok.setFixedHeight(int(45*scale)); left_side.addWidget(btn_ok)
        
        self.location_preview = QLabel(self.t["preview_tip"])
        self.location_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.location_preview.setStyleSheet("border: 2px solid #ddd; background: #ffffff; border-radius: 5px;")
        self.location_preview.installEventFilter(self)
        right_col = QVBoxLayout()
        right_col.addWidget(self.location_preview, 1)
        self.hover_hint = QLabel("")
        self.hover_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hover_hint.setStyleSheet("color: #888;")
        right_col.addWidget(self.hover_hint)
        main_layout.addWidget(left_container)
        main_layout.addLayout(right_col, 1)
        self._hover_pages = []
        self._hover_pos = 0
        self._hover_bbox = None
        self._hover_type = "txt"

    def eventFilter(self, source, event):
        et = event.type()
        if et == QEvent.Type.Enter:
            loc = source.property("loc_info")
            if loc:
                pages = loc.get("pages") or [loc["page"]]
                self._hover_pages = list(pages)
                try:
                    self._hover_pos = self._hover_pages.index(loc["page"])
                except ValueError:
                    self._hover_pos = 0
                self._hover_bbox = loc["bbox"]
                self._hover_type = loc["type"]
                self.show_location_on_page(loc["page"], loc["bbox"], loc["type"])
                self._update_hover_hint()
            return True
        # 悬停候选后，滚轮在"该候选出现的各页"间切换预览
        if et == QEvent.Type.Wheel and source is self.location_preview and len(self._hover_pages) > 1:
            delta = event.angleDelta().y()
            n = len(self._hover_pages)
            self._hover_pos = (self._hover_pos + (-1 if delta > 0 else 1)) % n
            pg = self._hover_pages[self._hover_pos]
            self.show_location_on_page(pg, self._hover_bbox, self._hover_type)
            self._update_hover_hint()
            event.accept()
            return True
        return super().eventFilter(source, event)

    def _update_hover_hint(self):
        if len(self._hover_pages) > 1:
            self.hover_hint.setText(
                f"🖱 滚轮切换该候选出现的页面（{self._hover_pos + 1}/{len(self._hover_pages)}）")
        else:
            self.hover_hint.setText("")

    def show_location_on_page(self, page_idx, bbox, mark_type):
        try:
            page = self.doc[page_idx]
            view_w, view_h = self.location_preview.width() - 20, self.location_preview.height() - 20
            zoom = min(view_w / page.rect.width, view_h / page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            pixmap = QPixmap.fromImage(QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888))
            
            painter = QPainter(pixmap)
            # 根据类型设置颜色：图片为红，文字为蓝
            color = QColor(231, 76, 60) if mark_type == "img" else QColor(52, 152, 219)
            painter.setPen(QPen(color, 2, Qt.PenStyle.SolidLine))
            
            # 计算适配大小的椭圆 (尺寸大一圈)
            target_rect = fitz.Rect(bbox) * zoom
            padding = 6 # 椭圆比实际内容多出的边距
            ellipse_rect = target_rect.irect # 获取整数矩形
            ellipse_rect.x0 -= padding
            ellipse_rect.y0 -= padding
            ellipse_rect.x1 += padding
            ellipse_rect.y1 += padding
            
            painter.drawEllipse(ellipse_rect.x0, ellipse_rect.y0, ellipse_rect.width, ellipse_rect.height)
            painter.end()
            self.location_preview.setPixmap(pixmap)
        except Exception as e:
            self.location_preview.setText(f"Preview error: {e}")

    def select_all(self):
        for cb in self.img_boxes.values(): cb.setChecked(True)
        for item in self.text_line_boxes: item['checkbox'].setChecked(True)
    def select_none(self):
        for cb in self.img_boxes.values(): cb.setChecked(False)
        for item in self.text_line_boxes: item['checkbox'].setChecked(False)
    def filter_items(self, text):
        # 忽略所有空白（中文/IME 输入可能带空格或全角空格）
        q = "".join(text.lower().split())
        for frame, content in self.text_cards:
            c = "".join(content.split())
            frame.setVisible(q in c)
    def get_selection(self):
        imgs = [h for h, cb in self.img_boxes.items() if cb.isChecked()]
        txts = [{'text': i['content'], 'bbox': i['bbox'], 'size': i['size'],
                 'origin': i.get('origin'), 'rot': i.get('rot', 0.0)}
                for i in self.text_line_boxes if i['checkbox'].isChecked()]
        return imgs, txts

    def get_apply_all(self):
        return self.apply_all_cb.isChecked()

# --- 3. 后台清理工作线程 ---
class MasterWorker(QThread):
    progress = pyqtSignal(int)
    log_signal = pyqtSignal(str)
    need_confirm = pyqtSignal(dict, dict)
    finished = pyqtSignal(object)
    failed = pyqtSignal()

    def __init__(self, file_path, ratio_threshold=30):
        super().__init__()
        self.file_path = file_path
        self.ratio_threshold = ratio_threshold / 100.0
        self.confirmed_hashes = []; self.confirmed_texts = []
        self.is_confirmed = False
        self.stop_flag = False            # 取消标志
        self.extra_keywords = []          # 设置里的手动关键词
        self.batch_files = []             # 批量模式文件列表（空 = 单文件）
        self.apply_all_requested = False  # 确认弹窗勾选"应用到全部"
        self.apply_all_confirms = None    # 批量复用的确认项 (hashes, texts)

    def run(self):
        try:
            files = self.batch_files or [self.file_path]
            outputs = []
            for fi, fpath in enumerate(files):
                if self.stop_flag:
                    self.log_signal.emit(">>> Cancelled.")
                    break
                self.is_confirmed = False
                self.confirmed_hashes = []
                self.confirmed_texts = []
                if len(files) > 1:
                    self.log_signal.emit(f">>> [{fi+1}/{len(files)}] {os.path.basename(fpath)}")
                out_path = self._process_one(fpath, fi, len(files))
                if self.stop_flag:
                    self.log_signal.emit(">>> Cancelled.")
                    break
                if out_path:
                    outputs.append(out_path)
            if outputs and not self.stop_flag:
                self.log_signal.emit(f">>> Batch processing finished: {len(outputs)} file(s)")
                try:
                    self.finished.emit(fitz.open(outputs[-1]))
                except Exception:
                    self.finished.emit(None)
            elif self.stop_flag or not outputs:
                self.failed.emit()
        except Exception as e:
            self.log_signal.emit(f"Error: {e}")
            self.failed.emit()

    def _process_one(self, fpath, fi, nfiles):
        """处理单个文件：分析 → 确认 → 清理 → 复检。返回输出路径或 None。"""
        if _dw is None or fitz is None or pikepdf is None:
            _load_heavy_libs()
        self.log_signal.emit(">>> Starting analysis...")
        doc = fitz.open(fpath)
        total = len(doc)
        cpu_count = max(1, (os.cpu_count() or 4) - 1)
        chunk_size = max(1, total // cpu_count)
        ranges = [list(range(i, min(i + chunk_size, total))) for i in range(0, total, chunk_size)]
        self.log_signal.emit(f">>> PDF loaded: {total} pages. Using {cpu_count} CPU cores.")
        all_page_results = []
        with ProcessPoolExecutor(max_workers=cpu_count) as executor:
            futures = [executor.submit(analyze_chunk_worker, fpath, r) for r in ranges]
            for i, f in enumerate(futures):
                if self.stop_flag:
                    executor.shutdown(wait=False, cancel_futures=True)
                    doc.close()
                    return None
                res, errs = f.result()
                all_page_results.extend(res)
                for e in errs:
                    self.log_signal.emit(f"Worker Warning: {e}")
                self.log_signal.emit(f">>> Scanning progress: {int((i+1)/len(futures)*100)}%")

        size_groups = {}
        for data in all_page_results:
            size_groups.setdefault(data['size_key'], []).append(data)

        final_img_candidates = {}; final_txt_candidates = {}
        pages_by_hash = {}
        pages_by_tk = {}
        for size_key, pages in size_groups.items():
            group_count = len(pages)
            threshold = max(2, group_count * self.ratio_threshold)
            img_counts = {}; txt_counts = {}
            for p in pages:
                unique_hashes = set(img['hash'] for img in p['imgs'])
                for h in unique_hashes:
                    pages_by_hash.setdefault(h, []).append(p['index'])
                    img_counts[h] = img_counts.get(h, 0) + 1
                    if h not in final_img_candidates:
                        for img in p['imgs']:
                            if img['hash'] == h:
                                img_rect = doc[p['index']].get_image_rects(img['xref'])[0]
                                final_img_candidates[h] = {'xref': img['xref'], 'count': 0,
                                                           'sample_page': p['index'],
                                                           'sample_bbox': tuple(img_rect)}
                for t in p['texts']:
                    tk = (t['text'], t['size'], size_key)
                    pages_by_tk.setdefault(tk, []).append(p['index'])
                    txt_counts[tk] = txt_counts.get(tk, 0) + 1
                    if tk not in final_txt_candidates:
                        final_txt_candidates[tk] = {'sample_page': p['index'], 'count': 0,
                                                    'bbox': t['bbox'],
                                                    'origin': t.get('origin'),
                                                    'rot': t.get('rot', 0.0)}
            for h, count in img_counts.items():
                if count >= threshold:
                    final_img_candidates[h]['count'] += count
            for tk, count in txt_counts.items():
                if count >= threshold:
                    final_txt_candidates[tk]['count'] += count

        final_img_candidates = {k: v for k, v in final_img_candidates.items() if v['count'] > 0}
        final_txt_candidates = {k: v for k, v in final_txt_candidates.items() if v['count'] > 0}
        # 记录每个候选出现的页面列表（弹窗里滚轮切换预览用）
        for h, info in final_img_candidates.items():
            info['pages'] = sorted(set(pages_by_hash.get(h, [info['sample_page']])))
        for tk, info in final_txt_candidates.items():
            info['pages'] = sorted(set(pages_by_tk.get(tk, [info['sample_page']])))

        # 确认环节：批量且已有复用确认项时跳过弹窗
        ic, tc = list(self.confirmed_hashes), list(self.confirmed_texts)
        if self.apply_all_confirms is not None:
            ic, tc = list(self.apply_all_confirms[0]), list(self.apply_all_confirms[1])
            self.log_signal.emit(">>> Applying previous selections to this file...")
        else:
            self.log_signal.emit(">>> Waiting for user confirmation...")
            self.need_confirm.emit(final_img_candidates, final_txt_candidates)
            while not self.is_confirmed and not self.stop_flag:
                self.msleep(50)
            if self.stop_flag:
                doc.close()
                return None
            ic, tc = list(self.confirmed_hashes), list(self.confirmed_texts)
            if self.apply_all_requested and nfiles > 1:
                self.apply_all_confirms = (list(ic), list(tc))

        self.log_signal.emit(">>> Applying cleaning process...")
        keywords = [c['text'].encode('utf-8') for c in tc] + \
                   [k.encode('utf-8') for k in self.extra_keywords]
        confirmed_xrefs = set()
        for i in range(total):
            page = doc[i]
            for img in page.get_images(full=True):
                try:
                    pix = fitz.Pixmap(doc, img[0])
                    if xxhash.xxh64(pix.samples).hexdigest() in set(ic):
                        confirmed_xrefs.add(img[0])
                except Exception:
                    continue
            self.progress.emit(int((i + 1) / total * 30))

        try:
            pdf = pikepdf.open(fpath)
        except Exception:
            # 加密等无法直接打开时，退回 fitz 转存后再处理（尽力而为）
            tmp_imgs = os.path.join(tempfile.gettempdir(),
                                    f"__wm_imgs_{uuid.uuid4().hex}_{os.path.basename(fpath)}")
            doc.save(tmp_imgs, garbage=4, deflate=True)
            pdf = pikepdf.open(tmp_imgs)
        doc.close()

        removed_total = 0
        for i, page in enumerate(pdf.pages):
            if self.stop_flag:
                pdf.close()
                return None
            n = _wm_process_page(pdf, page, keywords)
            n += _wm_process_xobjects(page.get("/Resources"), keywords)
            removed_total += n
            self.progress.emit(30 + int((i + 1) / total * 30))
        # 几何签名删除（CID/特殊编码字体水印的关键词兜底）
        geo_removed = 0
        geo_targets = []
        for c in tc:
            if c.get('origin') and c.get('size'):
                geo_targets.append((float(c['size']), float(c.get('rot', 0.0)),
                                    float(c['origin'][0]), float(c['origin'][1])))
        if geo_targets:
            for page in pdf.pages:
                if self.stop_flag:
                    break
                geo_removed += _dw.process_page_geo(pdf, page, geo_targets)
        img_removed = 0
        if confirmed_xrefs:
            img_cand = _dw.find_image_objgens(pdf, confirmed_xrefs)
            img_removed = _dw.remove_image_watermarks(pdf, img_cand)

        if self.batch_files:
            out_path = os.path.join(os.path.dirname(fpath),
                                    os.path.splitext(os.path.basename(fpath))[0] + "_cleaned.pdf")
        else:
            out_path = os.path.join(tempfile.gettempdir(),
                                    f"__wm_final_{uuid.uuid4().hex}_{os.path.basename(fpath)}")
        pdf.save(out_path, encryption=False)
        pdf.close()
        self.progress.emit(100)

        # 复检：文本残留页 + 图片 xref 残留
        resid = 0
        left_imgs = []
        try:
            chk = fitz.open(out_path)
            for pg in chk:
                t = pg.get_text()
                if any(k.decode('utf-8', 'replace').lower() in t.lower() for k in keywords):
                    resid += 1
            left_imgs = [g for pg in chk for g in pg.get_images(full=True) if g[0] in confirmed_xrefs]
            chk.close()
        except Exception:
            pass
        self.log_signal.emit(f">>> 文本水印块删除: {removed_total} 个(关键词)；几何删除: {geo_removed} 个；图片水印 Do 删除: {img_removed} 个；权限限制已移除")
        if resid == 0 and not left_imgs:
            self.log_signal.emit(">>> Verify passed: no residual")
        else:
            self.log_signal.emit(f">>> Verify warning: text residual {resid} pages, image residual {len(left_imgs)}")
        return out_path

# --- 4. 主程序窗口 ---
class UltraAppFinal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        # 首次启动按系统语言；之后按用户最后选择
        self.lang = self.config.get("lang") or detect_system_lang()
        self.doc_orig = self.doc_clean = None
        self.display_lists = {}; self.file_path = ""
        self.ratio_threshold = self.config.get("ratio", 30)
        self.extra_keywords = list(self.config.get("keywords", []))
        self.worker = None
        self.zoom = 1.0
        self.fit_mode = "fit_page"   # fit_page / fit_width / custom
        self.batch_files = []
        self._sb_guard = False       # 滚动条联动防递归
        self._rb_start = None        # 矩形框放大起点
        self._rubber = None

        self.scale = QApplication.primaryScreen().logicalDotsPerInch() / 96.0
        self.init_ui(); self.setAcceptDrops(True)
        self.setGeometry(QApplication.primaryScreen().availableGeometry())
        self.showMaximized()
        self.refresh_ui_text()
        self.add_log("Tip: drag & drop PDF file(s) anywhere to open / batch clean")

    def init_ui(self):
        main_widget = QWidget(); self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget); sidebar = QVBoxLayout()

        # 菜单栏：文件(打开/最近文件/退出)
        menubar = self.menuBar()
        self.menu_file = menubar.addMenu("File")
        act_open = self.menu_file.addAction("Open...")
        act_open.triggered.connect(self.load_file_dialog)
        self.menu_recent = self.menu_file.addMenu("Recent Files")
        act_exit = self.menu_file.addAction("Exit")
        act_exit.triggered.connect(self.close)

        self.btn_open = QPushButton(); self.btn_clean = QPushButton()
        self.btn_save = QPushButton(); self.btn_save.setEnabled(False)
        self.btn_cancel = QPushButton(); self.btn_cancel.setEnabled(False)
        self.btn_settings = QPushButton()
        self.pbar = QProgressBar(); self.log_output = QTextEdit(); self.log_output.setReadOnly(True)

        for b in [self.btn_open, self.btn_clean, self.btn_save, self.btn_cancel, self.btn_settings]:
            b.setFixedHeight(int(42 * self.scale)); sidebar.addWidget(b)
        sidebar.addWidget(self.log_output); sidebar.addWidget(self.pbar)

        viewer = QVBoxLayout(); nav = QHBoxLayout()
        self.page_spin = QSpinBox(); self.total_label = QLabel("/ 0")
        nav.addStretch(); nav.addWidget(self.page_spin); nav.addWidget(self.total_label); nav.addStretch()
        # 缩放控制
        self.btn_zo = QPushButton("−"); self.btn_zi = QPushButton("+")
        self.btn_fw = QPushButton(); self.btn_fp = QPushButton()
        self.zoom_label = QLabel("100%")
        for b in [self.btn_zo, self.btn_zi, self.btn_fw, self.btn_fp]:
            b.setFixedHeight(int(28 * self.scale))
        nav.addWidget(self.btn_zo); nav.addWidget(self.zoom_label); nav.addWidget(self.btn_zi)
        nav.addWidget(self.btn_fw); nav.addWidget(self.btn_fp)
        comp = QHBoxLayout()
        self.scroll_orig = QScrollArea(); self.lab_orig = QLabel()
        self.scroll_clean = QScrollArea(); self.lab_clean = QLabel()
        for s, l in [(self.scroll_orig, self.lab_orig), (self.scroll_clean, self.lab_clean)]:
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            s.setWidget(l)
            s.setWidgetResizable(False)
            s.setAlignment(Qt.AlignmentFlag.AlignCenter)
            s.installEventFilter(self)
            l.installEventFilter(self)   # 支持左键拖框放大
        comp.addWidget(self.scroll_orig); comp.addWidget(self.scroll_clean)
        # 页码滚动条：适合页面模式下显示，值=页码（拉到最底=最后一页）
        preview_row = QHBoxLayout()
        preview_row.addLayout(comp)
        self.page_bar = QScrollBar(Qt.Orientation.Vertical)
        self.page_bar.setRange(0, 0)
        preview_row.addWidget(self.page_bar)
        viewer.addLayout(nav); viewer.addLayout(preview_row)

        layout.addLayout(sidebar, 1); layout.addLayout(viewer, 4)
        self.btn_open.clicked.connect(self.load_file_dialog)
        self.btn_clean.clicked.connect(self.start_task)
        self.btn_save.clicked.connect(self.save_as_pdf)
        self.btn_cancel.clicked.connect(self.stop_task)
        self.btn_settings.clicked.connect(self.show_settings)
        self.btn_zo.clicked.connect(lambda: self.zoom_by(0.8))
        self.btn_zi.clicked.connect(lambda: self.zoom_by(1.25))
        self.btn_fw.clicked.connect(lambda: self.set_fit("fit_width"))
        self.btn_fp.clicked.connect(lambda: self.set_fit("fit_page"))
        self.page_spin.valueChanged.connect(self.update_previews)
        self.page_bar.valueChanged.connect(self._on_page_bar)

    def refresh_ui_text(self):
        t = TRANSLATIONS[self.lang]
        self.setWindowTitle(t["title"])
        self.btn_open.setText(t["open"])
        self.btn_clean.setText(t["clean"])
        self.btn_save.setText(t["save"])
        self.btn_cancel.setText(t["cancel"])
        self.btn_settings.setText(t["settings"])
        self.btn_fw.setText(t["fit_width"])
        self.btn_fp.setText(t["fit_page"])
        self.menu_file.setTitle(t["recent"] if False else "File")
        self.menu_recent.setTitle(t["recent"])
        self.lab_orig.setText(t["orig"])
        self.lab_clean.setText(t["cleaned"])
        if self.doc_orig:
            self.total_label.setText(f"/ {len(self.doc_orig)} {t['page']}")
        self.rebuild_recent_menu()

    def rebuild_recent_menu(self):
        self.menu_recent.clear()
        for p in list(self.config.get("recent_files", []))[:10]:
            if os.path.isfile(p):
                act = self.menu_recent.addAction(os.path.basename(p))
                act.setToolTip(p)
                act.triggered.connect(lambda _=False, pp=p: self.load_pdf(pp))
        if self.menu_recent.isEmpty():
            self.menu_recent.addAction("(empty)").setEnabled(False)

    def show_settings(self):
        dialog = SettingsDialog(self.ratio_threshold, self.lang, self.extra_keywords, self.scale, self)
        if dialog.exec():
            self.ratio_threshold, self.lang, self.extra_keywords = dialog.get_values()
            self.config["ratio"] = self.ratio_threshold
            self.config["lang"] = self.lang
            self.config["keywords"] = self.extra_keywords
            save_config(self.config)
            self.refresh_ui_text()

    def add_log(self, text):
        self.log_output.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
        self.log_output.moveCursor(QTextCursor.MoveOperation.End)

    def eventFilter(self, source, event):
        et = event.type()
        # --- 左键拖框放大 ---
        if source in (self.lab_orig, self.lab_clean):
            if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._rb_start = event.position().toPoint()
                if self._rubber is None:
                    self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, source)
                self._rubber.setGeometry(QRect(self._rb_start, self._rb_start))
                self._rubber.show()
                return True
            if et == QEvent.Type.MouseMove and self._rb_start is not None:
                self._rubber.setGeometry(QRect(self._rb_start, event.position().toPoint()).normalized())
                return True
            if et == QEvent.Type.MouseButtonRelease and self._rb_start is not None:
                self._rubber.hide()
                rect = QRect(self._rb_start, event.position().toPoint()).normalized()
                self._rb_start = None
                if rect.width() >= 8 and rect.height() >= 8:
                    self._zoom_to_rect(source, rect)
                return True
        # --- 滚轮策略 ---
        if et == QEvent.Type.Wheel and self.doc_orig:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                self.zoom_around(event.globalPosition(), 1.2 if delta > 0 else 1 / 1.2)
                event.accept()
                return True
            if self.fit_mode == "fit_page":
                # 页码导航模式：滚轮翻页
                delta = event.angleDelta().y()
                self.page_spin.setValue(self.page_spin.value() + (-1 if delta > 0 else 1))
                return True
            # 放大模式：页面放不下交给滚动条；放得下则翻页
            sb_orig = self.scroll_orig.verticalScrollBar()
            sb_clean = self.scroll_clean.verticalScrollBar()
            has_range = (sb_orig is not None and sb_orig.maximum() > 0) or \
                        (sb_clean is not None and sb_clean.maximum() > 0)
            if has_range:
                return False
            delta = event.angleDelta().y()
            self.page_spin.setValue(self.page_spin.value() + (-1 if delta > 0 else 1))
            return True
        return super().eventFilter(source, event)

    def keyPressEvent(self, event):
        if self.doc_orig:
            if event.key() == Qt.Key.Key_Left:
                self.page_spin.setValue(self.page_spin.value() - 1)
                return
            if event.key() == Qt.Key.Key_Right:
                self.page_spin.setValue(self.page_spin.value() + 1)
                return
        super().keyPressEvent(event)

    def zoom_by(self, factor):
        self.fit_mode = "custom"
        self.zoom = max(0.2, min(12.0, self.zoom * factor))
        self.zoom_label.setText(f"{int(self.zoom * 100)}%")
        self.update_previews()
        # 缩放后两侧预览显示同一区域
        self._sync_scroll_views(self.scroll_orig)

    def zoom_around(self, gp, factor):
        """以鼠标位置为中心缩放（鼠标下的 PDF 内容保持不动）。"""
        scroll = None
        for sc in (self.scroll_orig, self.scroll_clean):
            vp = sc.viewport()
            if vp.rect().contains(vp.mapFromGlobal(gp.toPoint())):
                scroll = sc
                break
        if scroll is None:
            scroll = self.scroll_orig
        lab = self.lab_orig if scroll is self.scroll_orig else self.lab_clean
        self._zoom_around_local(scroll, lab.mapFromGlobal(gp.toPoint()), factor)

    def _zoom_around_local(self, scroll, local, factor):
        """锚点缩放核心：保持鼠标下的 PDF 点屏幕位置不变。"""
        lab = self.lab_orig if scroll is self.scroll_orig else self.lab_clean
        doc = self.doc_orig if scroll is self.scroll_orig else self.doc_clean
        if doc is None or lab.width() <= 0:
            return
        hsb, vsb = scroll.horizontalScrollBar(), scroll.verticalScrollBar()
        old_z = self.zoom
        new_z = max(0.2, min(12.0, old_z * factor))
        if abs(new_z - old_z) < 1e-9:
            return
        # 鼠标下的 PDF 坐标（当前视图）
        px = (local.x() + hsb.value()) / old_z
        py = (local.y() + vsb.value()) / old_z
        self.zoom = new_z
        self.fit_mode = "custom"
        self.zoom_label.setText(f"{int(new_z * 100)}%")
        self.update_previews()
        # 缩放后让该 PDF 点仍位于鼠标下
        hsb.setValue(int(px * new_z - local.x()))
        vsb.setValue(int(py * new_z - local.y()))
        self._sync_scroll_views(scroll)

    def set_fit(self, mode):
        self.fit_mode = mode
        self.update_previews()

    def _on_page_bar(self, value):
        """页码滚动条联动：值=页码-1。"""
        if self._sb_guard or not self.doc_orig:
            return
        target = value + 1
        if 1 <= target <= self.page_spin.maximum() and target != self.page_spin.value():
            self.page_spin.setValue(target)

    def _sync_scroll_views(self, src):
        """让左右两个预览显示同一区域（同步滚动位置）。"""
        other = self.scroll_clean if src is self.scroll_orig else self.scroll_orig
        if other is None:
            return
        self._sb_guard = True
        other.horizontalScrollBar().setValue(src.horizontalScrollBar().value())
        other.verticalScrollBar().setValue(src.verticalScrollBar().value())
        self._sb_guard = False

    def _zoom_to_rect(self, source, rect):
        """把预览里框选区域放大到视口并居中（两侧同步）。"""
        doc = self.doc_orig if source is self.lab_orig else self.doc_clean
        if doc is None:
            return
        scroll = self.scroll_orig if source is self.lab_orig else self.scroll_clean
        idx = self.page_spin.value() - 1
        page = doc[idx]
        lw, lh = source.width(), source.height()
        if lw <= 0 or lh <= 0:
            return
        # 像素矩形 -> PDF 坐标
        rx0 = page.rect.width * rect.x() / lw
        ry0 = page.rect.height * rect.y() / lh
        rx1 = page.rect.width * rect.right() / lw
        ry1 = page.rect.height * rect.bottom() / lh
        vw = scroll.viewport().width() - 10
        vh = scroll.viewport().height() - 10
        z = min(vw / max(1.0, rx1 - rx0), vh / max(1.0, ry1 - ry0))
        self.zoom = max(0.2, min(12.0, z))
        self.fit_mode = "custom"
        self.zoom_label.setText(f"{int(self.zoom * 100)}%")
        self.update_previews()
        # 居中到选区中心，并同步另一侧预览
        cx = (rx0 + rx1) / 2
        cy = (ry0 + ry1) / 2
        hsb = scroll.horizontalScrollBar()
        vsb = scroll.verticalScrollBar()
        hsb.setValue(int(cx * self.zoom - vw / 2))
        vsb.setValue(int(cy * self.zoom - vh / 2))
        self._sync_scroll_views(scroll)

    def update_previews(self):
        if not self.doc_orig:
            return
        idx = self.page_spin.value() - 1

        # 页码滚动条：适合页面模式显示(值=页码)，放大模式隐藏(用滚动区自带滚动条)
        page_mode = self.fit_mode == "fit_page"
        self.page_bar.setVisible(page_mode)
        if page_mode:
            self._sb_guard = True
            self.page_bar.setRange(0, self.doc_orig.page_count - 1)
            self.page_bar.setValue(idx)
            self._sb_guard = False
        for s in (self.scroll_orig, self.scroll_clean):
            s.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff if page_mode
                else Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        def compute_zoom(doc, scroll):
            if self.fit_mode == "fit_page":
                vw, vh = scroll.viewport().width() - 10, scroll.viewport().height() - 10
                return min(vw / doc[idx].rect.width, vh / doc[idx].rect.height)
            if self.fit_mode == "fit_width":
                vw = scroll.viewport().width() - 10
                return vw / doc[idx].rect.width
            return self.zoom

        def render_to_label(doc, lab, scroll):
            try:
                z = compute_zoom(doc, scroll)
                pix = doc[idx].get_pixmap(matrix=fitz.Matrix(z, z))
                qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
                lab.setPixmap(QPixmap.fromImage(qimg))
                lab.setFixedSize(pix.width, pix.height)  # 尺寸超过视口时滚动条出现
                if self.fit_mode != "custom":
                    self.zoom_label.setText(f"{int(z * 100)}%")
            except Exception as e:
                lab.setText(f"Render error: {e}")
        render_to_label(self.doc_orig, self.lab_orig, self.scroll_orig)
        if self.doc_clean:
            render_to_label(self.doc_clean, self.lab_clean, self.scroll_clean)

    def _update_recent(self, path):
        rec = [p for p in self.config.get("recent_files", []) if p != path]
        rec.insert(0, path)
        self.config["recent_files"] = rec[:10]
        self.config["last_dir"] = os.path.dirname(path)
        save_config(self.config)
        self.rebuild_recent_menu()

    def load_pdf(self, path):
        """加载 PDF 文件（文件对话框、拖拽、最近文件共用的入口）。"""
        if fitz is None:
            _load_heavy_libs()
        if not path or not os.path.isfile(path):
            self.add_log(f"File not found: {path}")
            return False
        try:
            self.doc_orig = fitz.open(path)
        except Exception as ex:
            self.add_log(f"Open failed: {os.path.basename(path)} ({ex})")
            return False
        self.file_path = path
        self.display_lists = {}; self.doc_clean = None
        self.btn_save.setEnabled(False)
        self.page_spin.setRange(1, len(self.doc_orig)); self.page_spin.setValue(1)
        self.add_log(f"File loaded: {os.path.basename(path)}")
        self._update_recent(path)
        self.refresh_ui_text()
        self.update_previews()
        return True

    def load_file_dialog(self):
        start = self.config.get("last_dir") or ""
        path, _ = QFileDialog.getOpenFileName(self, "PDF", start, "PDF Files (*.pdf)")
        if path:
            self.load_pdf(path)

    # ---- PDF 拖拽打开 / 批量 ----
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            urls = [u.toLocalFile() for u in event.mimeData().urls()]
            if any(u and u.lower().endswith(".pdf") for u in urls):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):
        urls = [u.toLocalFile() for u in event.mimeData().urls()]
        pdfs = [u for u in urls if u and u.lower().endswith(".pdf")]
        if not pdfs:
            event.ignore()
            return
        event.acceptProposedAction()
        if len(pdfs) == 1:
            self.batch_files = []
            self.load_pdf(pdfs[0])
        else:
            self.batch_files = pdfs
            self.load_pdf(pdfs[0])
            self.add_log(f"Dropped {len(pdfs)} PDFs -> batch clean mode. Click Analyze to process all.")
            self.add_log("  提示: 第一个文件的确认勾选可'应用到其余所有文件'")

    def start_task(self):
        if not self.doc_orig:
            return
        if self.worker is not None and self.worker.isRunning():
            self.add_log(">>> " + TRANSLATIONS[self.lang]["analyzing"])
            return
        self.pbar.setValue(0)
        self.btn_clean.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        files = self.batch_files if len(self.batch_files) > 1 else [self.file_path]
        self.worker = MasterWorker(files[0], self.ratio_threshold)
        self.worker.batch_files = files if len(files) > 1 else []
        self.worker.extra_keywords = list(self.extra_keywords)
        self.worker.progress.connect(self.pbar.setValue)
        self.worker.log_signal.connect(self.add_log)
        self.worker.need_confirm.connect(self.ask_user)
        self.worker.finished.connect(self.task_done)
        self.worker.failed.connect(lambda: (self.btn_clean.setEnabled(True),
                                            self.btn_cancel.setEnabled(False)))
        self.worker.start()

    def stop_task(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop_flag = True
            self.add_log(">>> 正在停止…")
            self.btn_cancel.setEnabled(False)

    def ask_user(self, ic, tc):
        dialog = EnhancedWatermarkDialog(ic, tc, self.doc_orig, lang=self.lang, scale=self.scale, parent=self)
        if dialog.exec():
            h, t = dialog.get_selection()
            self.worker.confirmed_hashes, self.worker.confirmed_texts = h, t
            self.worker.apply_all_requested = dialog.get_apply_all()
            self.add_log(f"User confirmed: {len(h)} images, {len(t)} text blocks selected.")
        else:
            self.add_log("Clean process cancelled by user.")
        self.worker.is_confirmed = True

    def task_done(self, doc):
        self.btn_clean.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if doc is None:
            return
        self.doc_clean = doc
        self.btn_save.setEnabled(True)
        self.update_previews()

    def save_as_pdf(self):
        if self.doc_clean is None:
            return
        default = os.path.join(os.path.dirname(self.file_path),
                               f"cleaned_{os.path.basename(self.file_path)}")
        path, _ = QFileDialog.getSaveFileName(self, "Save", default, "PDF (*.pdf)")
        if path:
            self.doc_clean.save(path, garbage=4, deflate=True)
            self.add_log(f"Saved to: {path}")

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    app = QApplication(sys.argv)
    apply_dark_mode(app, None)
    window = UltraAppFinal()
    # 重型库后台延迟加载，窗口先出现
    import threading
    threading.Thread(target=_load_heavy_libs, daemon=True).start()
    sys.exit(app.exec())

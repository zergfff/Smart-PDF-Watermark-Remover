import sys
import os
import tempfile
import xxhash
import fitz
import pikepdf
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor
from PyQt6.QtWidgets import (QApplication, QMainWindow, QPushButton, QVBoxLayout, QHBoxLayout,
                             QWidget, QFileDialog, QLabel, QProgressBar, QMessageBox, QTextEdit,  
                             QDialog, QCheckBox, QScrollArea, QFrame, QSpinBox, QLineEdit, QComboBox)
from PyQt6.QtGui import QPixmap, QImage, QTextCursor, QPainter, QPen, QColor
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QEvent, QSize

# --- 环境适配 ---
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"

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
        "set_save": "保存设置"
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
        "set_save": "Save Settings"
    }
}

# --- 设置对话框 ---
class SettingsDialog(QDialog):
    def __init__(self, current_ratio, current_lang, scale, parent=None):
        super().__init__(parent)
        self.scale = scale
        self.t = TRANSLATIONS[current_lang]
        self.setWindowTitle(self.t["set_title"])
        self.setFixedWidth(int(300 * scale))
        
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
        
        self.btn_save = QPushButton(self.t["set_save"])
        self.btn_save.clicked.connect(self.accept)
        layout.addWidget(self.btn_save)

    def get_values(self):
        return self.ratio_spin.value(), self.lang_combo.currentData()

# --- 1. 底层计算逻辑 ---
def is_bbox_similar(bbox1, bbox2, tolerance=2.0):
    """判断两个 bbox 是否在容差范围内相似"""
    return all(abs(a - b) <= tolerance for a, b in zip(bbox1, bbox2))

def analyze_chunk_worker(file_path, page_indices):
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
                for img in page.get_images():
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
                blocks = page.get_text("dict")["blocks"]
                for b in blocks:
                    if b["type"] != 0: continue
                    for line in b["lines"]:
                        content = "".join([span["text"] for span in line["spans"]]).strip()
                        if len(content) > 1:
                            bbox = tuple([round(v, 1) for v in line["bbox"]])
                            size = round(max(span["size"] for span in line["spans"]), 1)
                            page_data['texts'].append({'text': content, 'bbox': bbox, 'size': size})
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
                try:
                    pix = fitz.Pixmap(self.doc, info['xref'])
                    # 如果原图很大，先缩放再转 QImage
                    if pix.width > 400 or pix.height > 400:
                        zoom = min(400/pix.width, 400/pix.height)
                        pix = fitz.Pixmap(pix, fitz.Matrix(zoom, zoom))
                    qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
                    lab = QLabel()
                    lab.setPixmap(QPixmap.fromImage(qimg).scaled(int(150*scale), int(80*scale), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    lab.setProperty("loc_info", {"page": info['sample_page'], "bbox": info['sample_bbox'], "type": "img"})
                    lab.installEventFilter(self)
                    l.addWidget(cb); l.addWidget(lab); l.addStretch()
                    l.addWidget(QLabel(f"<span style='{IMG_STAT_STYLE}'>{self.t['count']}: {info['count']}</span>"))
                    self.scroll_layout.addWidget(frame)
                except Exception as e:
                    print(f"Error loading img preview: {e}")

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
                    img_lab.setProperty("loc_info", {"page": info['sample_page'], "bbox": bbox, "type": "txt"})
                    img_lab.installEventFilter(self)
                    row_layout.addWidget(img_lab)
                except Exception as e:
                    print(f"Error loading text preview: {e}")
                row_layout.addStretch()
                row_layout.addWidget(QLabel(f"<span style='{TXT_STAT_STYLE}'>{self.t['count']}: {info['count']}</span>"))
                self.text_line_boxes.append({'checkbox': cb, 'content': key[0], 'bbox': info.get('bbox', (0, 0, 0, 0)), 'size': key[1]})
                self.text_cards.append((frame, key[0].lower()))
                self.scroll_layout.addWidget(frame)

        scroll.setWidget(content_widget); left_side.addWidget(scroll)
        btn_ok = QPushButton(self.t["ok"]); btn_ok.clicked.connect(self.accept)
        btn_ok.setFixedHeight(int(45*scale)); left_side.addWidget(btn_ok)
        
        self.location_preview = QLabel(self.t["preview_tip"])
        self.location_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.location_preview.setStyleSheet("border: 2px solid #ddd; background: #ffffff; border-radius: 5px;")
        main_layout.addWidget(left_container); main_layout.addWidget(self.location_preview, 1)

    def eventFilter(self, source, event):
        if event.type() == QEvent.Type.Enter:
            loc = source.property("loc_info")
            if loc: self.show_location_on_page(loc["page"], loc["bbox"], loc["type"])
            return True
        return super().eventFilter(source, event)

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
        for frame, content in self.text_cards: frame.setVisible(text.lower() in content)
    def get_selection(self):
        imgs = [h for h, cb in self.img_boxes.items() if cb.isChecked()]
        txts = [{'text': i['content'], 'bbox': i['bbox'], 'size': i['size']} for i in self.text_line_boxes if i['checkbox'].isChecked()]
        return imgs, txts

# --- 3. 后台清理工作线程 ---
class MasterWorker(QThread):
    progress = pyqtSignal(int)
    log_signal = pyqtSignal(str) 
    need_confirm = pyqtSignal(dict, dict)
    finished = pyqtSignal(object)

    def __init__(self, file_path, ratio_threshold=30):
        super().__init__()
        self.file_path = file_path
        self.ratio_threshold = ratio_threshold / 100.0
        self.confirmed_hashes = []; self.confirmed_texts = []
        self.is_confirmed = False

    def run(self):
        try:
            self.log_signal.emit(">>> Starting analysis...")
            doc = fitz.open(self.file_path)
            total = len(doc); all_page_results = []
            cpu_count = max(1, (os.cpu_count() or 4) - 1)
            chunk_size = max(1, total // cpu_count)
            ranges = [list(range(i, min(i + chunk_size, total))) for i in range(0, total, chunk_size)]
            
            self.log_signal.emit(f">>> PDF loaded: {total} pages. Using {cpu_count} CPU cores.")
            
            with ProcessPoolExecutor(max_workers=cpu_count) as executor:
                futures = [executor.submit(analyze_chunk_worker, self.file_path, r) for r in ranges]
                for i, f in enumerate(futures):
                    res, errs = f.result()
                    all_page_results.extend(res)
                    for e in errs: self.log_signal.emit(f"Worker Warning: {e}")
                    self.log_signal.emit(f">>> Scanning progress: {int((i+1)/len(futures)*100)}%")

            size_groups = {}
            for data in all_page_results:
                size_groups.setdefault(data['size_key'], []).append(data)

            final_img_candidates = {}; final_txt_candidates = {}
            for size_key, pages in size_groups.items():
                group_count = len(pages)
                threshold = max(2, group_count * self.ratio_threshold) 
                img_counts = {}; txt_counts = {}
                for p in pages:
                    unique_hashes = set(img['hash'] for img in p['imgs'])
                    for h in unique_hashes:
                        img_counts[h] = img_counts.get(h, 0) + 1
                        if h not in final_img_candidates:
                            for img in p['imgs']:
                                if img['hash'] == h:
                                    img_rect = doc[p['index']].get_image_rects(img['xref'])[0]
                                    final_img_candidates[h] = {'xref': img['xref'], 'count': 0, 'sample_page': p['index'], 'sample_bbox': tuple(img_rect)}
                    for t in p['texts']:
                        # 聚类 key：文本内容+字号（不再要求 bbox 完全一致，避免旋转/舍入差异导致匹配失败）
                        tk = (t['text'], t['size'], size_key)
                        txt_counts[tk] = txt_counts.get(tk, 0) + 1
                        if tk not in final_txt_candidates:
                            final_txt_candidates[tk] = {'sample_page': p['index'], 'count': 0, 'bbox': t['bbox']}

                for h, count in img_counts.items():
                    if count >= threshold: final_img_candidates[h]['count'] += count
                for tk, count in txt_counts.items():
                    if count >= threshold: final_txt_candidates[tk]['count'] += count

            final_img_candidates = {k: v for k, v in final_img_candidates.items() if v['count'] > 0}
            final_txt_candidates = {k: v for k, v in final_txt_candidates.items() if v['count'] > 0}

            self.log_signal.emit(">>> Waiting for user confirmation...")
            self.need_confirm.emit(final_img_candidates, final_txt_candidates)
            while not self.is_confirmed: self.msleep(50)
            
            self.log_signal.emit(">>> Applying cleaning process...")
            # ---- 1) 图片水印：fitz delete_image（按 xref 哈希，仅删图片对象本身） ----
            tmp_imgs = os.path.join(tempfile.gettempdir(), f"__wm_imgs_{os.path.basename(self.file_path)}")
            for i in range(total):
                page = doc[i]
                for img in page.get_images():
                    try:
                        pix = fitz.Pixmap(doc, img[0])
                        if xxhash.xxh64(pix.samples).hexdigest() in self.confirmed_hashes:
                            page.delete_image(img[0])
                    except Exception:
                        continue
                self.progress.emit(int((i + 1) / total * 50))
            doc.save(tmp_imgs, garbage=4, deflate=True)
            doc.close()

            # ---- 2) 文本水印：pikepdf 内容流级删除（只删绘制水印的 BT..ET 块，不碰正文）
            #        同时 encryption=False 彻底移除加密字典与权限位（禁打印/复制/修改等） ----
            keywords = [c['text'].encode('utf-8') for c in self.confirmed_texts]
            removed_total = 0
            pdf = pikepdf.open(tmp_imgs)
            for i, page in enumerate(pdf.pages):
                n = _wm_process_page(pdf, page, keywords)
                n += _wm_process_xobjects(page.get("/Resources"), keywords)
                removed_total += n
                self.progress.emit(50 + int((i + 1) / total * 50))
            out_tmp = os.path.join(tempfile.gettempdir(), f"__wm_final_{os.path.basename(self.file_path)}")
            pdf.save(out_tmp, encryption=False)
            pdf.close()
            self.log_signal.emit(f">>> 文本水印块删除: {removed_total} 个；权限限制已移除")
            self.log_signal.emit(">>> Done! Cleaned PDF is ready for preview/save.")
            self.finished.emit(fitz.open(out_tmp))
        except Exception as e: self.log_signal.emit(f"Error: {e}")

# --- 4. 主程序窗口 ---
class UltraAppFinal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.doc_orig = self.doc_clean = None
        self.display_lists = {}; self.file_path = ""
        self.ratio_threshold = 30
        self.lang = "en"
        
        self.scale = QApplication.primaryScreen().logicalDotsPerInch() / 96.0
        self.init_ui(); self.setAcceptDrops(True)
        self.setGeometry(QApplication.primaryScreen().availableGeometry())
        self.showMaximized()
        self.refresh_ui_text()

    def init_ui(self):
        main_widget = QWidget(); self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget); sidebar = QVBoxLayout()
        
        self.btn_open = QPushButton(); self.btn_clean = QPushButton()
        self.btn_save = QPushButton(); self.btn_save.setEnabled(False)
        self.btn_settings = QPushButton()
        self.pbar = QProgressBar(); self.log_output = QTextEdit(); self.log_output.setReadOnly(True)
        
        for b in [self.btn_open, self.btn_clean, self.btn_save, self.btn_settings]:
            b.setFixedHeight(int(50 * self.scale)); sidebar.addWidget(b)
        sidebar.addWidget(self.log_output); sidebar.addWidget(self.pbar)
        
        viewer = QVBoxLayout(); nav = QHBoxLayout()
        self.page_spin = QSpinBox(); self.total_label = QLabel("/ 0")
        nav.addStretch(); nav.addWidget(self.page_spin); nav.addWidget(self.total_label); nav.addStretch()
        comp = QHBoxLayout()
        self.scroll_orig = QScrollArea(); self.lab_orig = QLabel()
        self.scroll_clean = QScrollArea(); self.lab_clean = QLabel()
        for s, l in [(self.scroll_orig, self.lab_orig), (self.scroll_clean, self.lab_clean)]:
            l.setAlignment(Qt.AlignmentFlag.AlignCenter); s.setWidget(l); s.setWidgetResizable(True)
            s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); s.installEventFilter(self)
        comp.addWidget(self.scroll_orig); comp.addWidget(self.scroll_clean)
        viewer.addLayout(nav); viewer.addLayout(comp)
        
        layout.addLayout(sidebar, 1); layout.addLayout(viewer, 4)
        self.btn_open.clicked.connect(self.load_file_dialog)
        self.btn_clean.clicked.connect(self.start_task)
        self.btn_save.clicked.connect(self.save_as_pdf)
        self.btn_settings.clicked.connect(self.show_settings)
        self.page_spin.valueChanged.connect(self.update_previews)

    def refresh_ui_text(self):
        t = TRANSLATIONS[self.lang]
        self.setWindowTitle(t["title"])
        self.btn_open.setText(t["open"])
        self.btn_clean.setText(t["clean"])
        self.btn_save.setText(t["save"])
        self.btn_settings.setText(t["settings"])
        self.lab_orig.setText(t["orig"])
        self.lab_clean.setText(t["cleaned"])
        if self.doc_orig:
            self.total_label.setText(f"/ {len(self.doc_orig)} {t['page']}")

    def show_settings(self):
        dialog = SettingsDialog(self.ratio_threshold, self.lang, self.scale, self)
        if dialog.exec():
            self.ratio_threshold, self.lang = dialog.get_values()
            self.refresh_ui_text()

    def add_log(self, text):
        self.log_output.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
        self.log_output.moveCursor(QTextCursor.MoveOperation.End)

    def eventFilter(self, source, event):
        if event.type() == QEvent.Type.Wheel and self.doc_orig:
            delta = event.angleDelta().y()
            if delta > 0: self.page_spin.setValue(self.page_spin.value() - 1)
            else: self.page_spin.setValue(self.page_spin.value() + 1)
            return True
        return super().eventFilter(source, event)

    def update_previews(self):
        if not self.doc_orig: return
        idx = self.page_spin.value() - 1
        def render_to_label(doc, lab, scroll):
            try:
                page_data = self.display_lists.get(idx) if doc == self.doc_orig else doc[idx]
                if doc == self.doc_orig and idx not in self.display_lists:
                    self.display_lists[idx] = doc[idx].get_displaylist()
                    page_data = self.display_lists[idx]
                target_w, target_h = scroll.viewport().width() - 5, scroll.viewport().height() - 5
                zoom = min(target_w / doc[idx].rect.width, target_h / doc[idx].rect.height)
                pix = page_data.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                lab.setPixmap(QPixmap.fromImage(QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)))
            except Exception as e:
                lab.setText(f"Render error: {e}")
        render_to_label(self.doc_orig, self.lab_orig, self.scroll_orig)
        if self.doc_clean: render_to_label(self.doc_clean, self.lab_clean, self.scroll_clean)

    def load_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "PDF", "", "PDF Files (*.pdf)")
        if path:
            self.add_log(f"File loaded: {os.path.basename(path)}")
            self.doc_orig = fitz.open(path); self.file_path = path
            self.display_lists = {}; self.doc_clean = None
            self.page_spin.setRange(1, len(self.doc_orig)); self.page_spin.setValue(1)
            self.refresh_ui_text()
            self.update_previews()

    def start_task(self):
        if not self.doc_orig: return
        self.pbar.setValue(0)
        self.worker = MasterWorker(self.file_path, self.ratio_threshold)
        self.worker.progress.connect(self.pbar.setValue)
        self.worker.log_signal.connect(self.add_log)
        self.worker.need_confirm.connect(self.ask_user)
        self.worker.finished.connect(self.task_done)
        self.worker.start()

    def ask_user(self, ic, tc):
        dialog = EnhancedWatermarkDialog(ic, tc, self.doc_orig, lang=self.lang, scale=self.scale, parent=self)
        if dialog.exec():
            h, t = dialog.get_selection()
            self.worker.confirmed_hashes, self.worker.confirmed_texts = h, t
            self.add_log(f"User confirmed: {len(h)} images, {len(t)} text blocks selected.")
        else:
            self.add_log("Clean process cancelled by user.")
        self.worker.is_confirmed = True

    def task_done(self, doc):
        self.doc_clean = doc; self.btn_save.setEnabled(True); self.update_previews()

    def save_as_pdf(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save", f"cleaned_{os.path.basename(self.file_path)}", "PDF (*.pdf)")
        if path: 
            self.doc_clean.save(path, garbage=4, deflate=True)
            self.add_log(f"Saved to: {path}")

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    app = QApplication(sys.argv); window = UltraAppFinal(); sys.exit(app.exec())

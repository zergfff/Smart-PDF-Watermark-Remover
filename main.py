import sys
import os
import xxhash
import fitz
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
def analyze_chunk_worker(file_path, page_indices):
    results = []
    doc = None
    try:
        doc = fitz.open(file_path)
        for i in page_indices:
            page = doc[i]
            rect = page.rect
            pw, ph = round(rect.width, 1), round(rect.height, 1)
            page_data = {'index': i, 'size_key': (pw, ph), 'imgs': [], 'texts': []}
            for img in page.get_images():
                try:
                    pix = fitz.Pixmap(doc, img[0])
                    h = xxhash.xxh64(pix.samples).hexdigest()
                    page_data['imgs'].append({'hash': h, 'xref': img[0]})
                except: continue
            blocks = page.get_text("dict")["blocks"]
            for b in blocks:
                if b["type"] != 0: continue
                for line in b["lines"]:
                    content = "".join([span["text"] for span in line["spans"]]).strip()
                    if len(content) > 1:
                        bbox = tuple([round(v, 1) for v in line["bbox"]])
                        page_data['texts'].append({'text': content, 'bbox': bbox})
            results.append(page_data)
    except: pass
    finally:
        if doc: doc.close()
    return results

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
                frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
                l = QHBoxLayout(frame); l.setContentsMargins(5, 5, 5, 5)
                cb = QCheckBox(""); cb.setChecked(False); self.img_boxes[h] = cb
                pix = fitz.Pixmap(self.doc, info['xref'])
                qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
                lab = QLabel()
                lab.setPixmap(QPixmap.fromImage(qimg).scaled(int(150*scale), int(80*scale), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                # 设置 type 为 img
                lab.setProperty("loc_info", {"page": info['sample_page'], "bbox": info['sample_bbox'], "type": "img"})
                lab.installEventFilter(self)
                l.addWidget(cb); l.addWidget(lab); l.addStretch()
                l.addWidget(QLabel(f"<span style='{IMG_STAT_STYLE}'>{self.t['count']}: {info['count']}</span>"))
                self.scroll_layout.addWidget(frame)

        if text_blocks:
            header_txt = QLabel(f"<b>{self.t['txt_header']}</b>")
            header_txt.setStyleSheet("color: #3498db;")
            self.scroll_layout.addWidget(header_txt)
            for key, info in text_blocks.items():
                frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
                row_layout = QHBoxLayout(frame); row_layout.setContentsMargins(5, 5, 5, 5)
                cb = QCheckBox(); cb.setChecked(False)
                row_layout.addWidget(cb)
                try:
                    sample_page = self.doc[info['sample_page']]
                    clip_rect = fitz.Rect(key[1]) + (-10, -5, 10, 5)
                    pix = sample_page.get_pixmap(clip=clip_rect, matrix=fitz.Matrix(2, 2))
                    qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
                    img_lab = QLabel()
                    img_lab.setPixmap(QPixmap.fromImage(qimg).scaled(int(220*scale), int(60*scale), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    # 设置 type 为 txt
                    img_lab.setProperty("loc_info", {"page": info['sample_page'], "bbox": key[1], "type": "txt"})
                    img_lab.installEventFilter(self)
                    row_layout.addWidget(img_lab)
                except: pass
                row_layout.addStretch()
                row_layout.addWidget(QLabel(f"<span style='{TXT_STAT_STYLE}'>{self.t['count']}: {info['count']}</span>"))
                self.text_line_boxes.append({'checkbox': cb, 'content': key[0], 'bbox': key[1], 'size': key[2]})
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
        except: pass

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
                    all_page_results.extend(f.result())
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
                        tk = (t['text'], t['bbox'], size_key)
                        txt_counts[tk] = txt_counts.get(tk, 0) + 1
                        if tk not in final_txt_candidates:
                            final_txt_candidates[tk] = {'sample_page': p['index'], 'count': 0}

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
            for i in range(total):
                page = doc[i]; pw, ph = round(page.rect.width, 1), round(page.rect.height, 1); cur_size = (pw, ph)
                for img in page.get_images():
                    pix = fitz.Pixmap(doc, img[0])
                    if xxhash.xxh64(pix.samples).hexdigest() in self.confirmed_hashes: page.delete_image(img[0])
                p_dict = page.get_text("dict")
                for b in p_dict["blocks"]:
                    if b["type"] != 0: continue
                    for line in b["lines"]:
                        txt = "".join([s["text"] for s in line["spans"]]).strip()
                        bbox = tuple([round(v, 1) for v in line["bbox"]])
                        for conf in self.confirmed_texts:
                            if txt == conf['text'] and bbox == conf['bbox'] and cur_size == conf['size']:
                                page.add_redact_annot(line["bbox"])
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
                self.progress.emit(int((i + 1) / total * 100))
            
            self.log_signal.emit(">>> Done! Cleaned PDF is ready for preview/save.")
            self.finished.emit(doc)
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
            except: pass
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

import sys
import os
import hashlib
import fitz  # PyMuPDF
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor
from PyQt6.QtWidgets import (QApplication, QMainWindow, QPushButton, QVBoxLayout, QHBoxLayout,
                              QWidget, QFileDialog, QLabel, QProgressBar, QMessageBox, QTextEdit,  
                              QDialog, QCheckBox, QScrollArea, QFrame, QSpinBox, QLineEdit)
from PyQt6.QtGui import QPixmap, QImage, QTextCursor
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QEvent

# --- 环境适配 ---
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"

def analyze_size_groups(file_path):
    size_groups = {}  
    doc = None
    try:
        doc = fitz.open(file_path)
        if doc.is_encrypted: doc.authenticate("")  
        for i, page in enumerate(doc):
            rect = page.rect
            size_key = (round(rect.width, 1), round(rect.height, 1))
            if size_key not in size_groups: size_groups[size_key] = []
            size_groups[size_key].append(i)
    except: pass
    finally:
        if doc: doc.close()
    return size_groups

def analyze_chunk_worker(file_path, page_indices):
    results = {'imgs': [], 'texts': []}
    doc = None
    try:
        doc = fitz.open(file_path)
        if doc.is_encrypted: doc.authenticate("")  
        for i in page_indices:
            page = doc[i]
            for img in page.get_images():
                try:
                    pix = fitz.Pixmap(doc, img[0])
                    h = hashlib.md5(pix.samples[:2048]).hexdigest()
                    results['imgs'].append({'hash': h, 'xref': img[0], 'page': i})
                except: continue
            
            blocks = page.get_text("dict")["blocks"]
            for b in blocks:
                if b["type"] != 0: continue
                block_bbox = tuple([round(v/1)*1 for v in b["bbox"]])
                lines = []
                for line in b["lines"]:
                    content = "".join([span["text"] for span in line["spans"]]).strip()
                    if len(content) > 1:
                        line_bbox = tuple([round(v/1)*1 for v in line["bbox"]])
                        lines.append({'text': content, 'bbox': line_bbox})
                if lines:
                    results['texts'].append({'block_bbox': block_bbox, 'lines': lines, 'page': i})
    except: pass
    finally:
        if doc: doc.close()
    return results

class EnhancedWatermarkDialog(QDialog):
    def __init__(self, img_data, text_blocks, doc, scale=1.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm Potential Watermarks")
        self.doc = doc
        self.scale = scale
        self.img_boxes = {}
        self.text_cards = []  
        self.text_line_boxes = []  
        self.setMinimumSize(int(1100 * scale), int(850 * scale))  
        
        main_layout = QVBoxLayout(self)
        
        tool_layout = QHBoxLayout()
        btn_all = QPushButton("Select All"); btn_none = QPushButton("Clear All")
        btn_all.clicked.connect(self.select_all); btn_none.clicked.connect(self.select_none)
        
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("🔍 Search text content...")
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.textChanged.connect(self.filter_items)
        
        tool_layout.addWidget(btn_all); tool_layout.addWidget(btn_none)
        tool_layout.addSpacing(20)
        tool_layout.addWidget(QLabel("Filter Text:"))
        tool_layout.addWidget(self.search_bar)
        main_layout.addLayout(tool_layout)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        content_widget = QWidget(); self.scroll_layout = QVBoxLayout(content_widget)
        render_mat = fitz.Matrix(2.0 * scale, 2.0 * scale)  

        self.img_section_header = QLabel("<b>🖼️ Repeated Images (>30%):</b>")
        self.img_container = QWidget()
        self.img_layout = QVBoxLayout(self.img_container)
        self.img_layout.setContentsMargins(0,0,0,0)
        
        if img_data:
            self.scroll_layout.addWidget(self.img_section_header)
            sorted_imgs = sorted(img_data.items(), key=lambda x: x[1]['count'], reverse=True)
            for h, info in sorted_imgs:
                frame = QFrame(); frame.setStyleSheet("QFrame { background: #fff; border: 1px solid #ccc; border-radius: 5px; }")
                l = QHBoxLayout(frame)
                cb = QCheckBox(f"Remove Image"); self.img_boxes[h] = cb
                pix = fitz.Pixmap(self.doc, info['xref'])
                qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
                lab = QLabel(); lab.setPixmap(QPixmap.fromImage(qimg).scaledToHeight(int(80*scale)))
                count_lab = QLabel(f"found in {info['count']} pages")
                count_lab.setStyleSheet("color: blue; font-style: italic;")
                l.addWidget(cb); l.addWidget(lab); l.addStretch(); l.addWidget(count_lab)
                self.img_layout.addWidget(frame)
            self.scroll_layout.addWidget(self.img_container)

        if text_blocks:
            self.text_header = QLabel("<b>🔤 Potential Text Watermarks (>30%):</b>")
            self.scroll_layout.addWidget(self.text_header)
            sorted_texts = sorted(text_blocks.items(), key=lambda x: x[1]['count'], reverse=True)
            for b_bbox, info in sorted_texts:
                frame = QFrame()
                frame.setStyleSheet("QFrame { background: #fdfdfd; border: 1px solid #dcdde1; border-radius: 8px; margin: 5px; }")
                v_layout = QVBoxLayout(frame)
                
                sample_page = self.doc[info['sample_page']]
                clip_rect = fitz.Rect(b_bbox) + (-10, -5, 10, 5)
                pix = sample_page.get_pixmap(clip=clip_rect, matrix=render_mat)
                qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
                img_lab = QLabel()
                pix_map = QPixmap.fromImage(qimg)
                max_w = int(950 * scale)
                if pix_map.width() > max_w:
                    pix_map = pix_map.scaledToWidth(max_w, Qt.TransformationMode.SmoothTransformation)
                img_lab.setPixmap(pix_map)
                img_lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
                img_lab.setStyleSheet("background: #f9f9f9; border-bottom: 1px solid #eee; padding: 10px;")
                v_layout.addWidget(img_lab)

                combined_text_for_search = ""
                for line in info['lines']:
                    line_row = QHBoxLayout()
                    cb = QCheckBox(line['text'])
                    cb.setStyleSheet("QCheckBox { color: red; font-weight: bold; border: none; }")
                    count_lab = QLabel(f"found in {info['count']} pages")
                    count_lab.setStyleSheet("color: blue; font-size: 11px;")
                    line_row.addWidget(cb); line_row.addStretch(); line_row.addWidget(count_lab)
                    v_layout.addLayout(line_row)
                    self.text_line_boxes.append({'checkbox': cb, 'content': line['text'], 'bbox': line['bbox']})
                    combined_text_for_search += line['text'].lower() + " "
                
                self.scroll_layout.addWidget(frame)
                self.text_cards.append((frame, combined_text_for_search))

        scroll.setWidget(content_widget)
        main_layout.addWidget(scroll)
        
        btn_ok = QPushButton("Apply Cleaning")
        btn_ok.clicked.connect(self.accept)
        btn_ok.setFixedHeight(int(50*scale))
        btn_ok.setStyleSheet("font-weight: bold; font-size: 14px;")  
        main_layout.addWidget(btn_ok)

    def filter_items(self, search_text):
        query = search_text.lower().strip()
        if query:
            self.img_section_header.hide(); self.img_container.hide()
        else:
            self.img_section_header.show(); self.img_container.show()
        for frame, text in self.text_cards:
            frame.setVisible(query in text)

    def select_all(self):
        for cb in self.img_boxes.values(): cb.setChecked(True)
        for item in self.text_line_boxes: item['checkbox'].setChecked(True)

    def select_none(self):
        for cb in self.img_boxes.values(): cb.setChecked(False)
        for item in self.text_line_boxes: item['checkbox'].setChecked(False)

    def get_selection(self):
        imgs = [h for h, cb in self.img_boxes.items() if cb.isChecked()]
        txts = [(item['content'], item['bbox']) for item in self.text_line_boxes if item['checkbox'].isChecked()]
        return imgs, txts

class MasterWorker(QThread):
    progress = pyqtSignal(int)
    log_signal = pyqtSignal(str) 
    need_confirm = pyqtSignal(dict, dict)
    finished = pyqtSignal(object)

    def __init__(self, file_path):
        super().__init__()
        self.file_path = file_path
        self.confirmed_hashes = []; self.confirmed_lines = []
        self.is_confirmed = False

    def run(self):
        doc = None
        try:
            self.log_signal.emit(f"Starting analysis for: {os.path.basename(self.file_path)}")
            size_groups = analyze_size_groups(self.file_path)
            doc = fitz.open(self.file_path)
            if doc.is_encrypted: doc.authenticate("")

            final_img_cands = {}; final_text_cands = {}
            cpu_count = min(os.cpu_count() or 4, 8)
            self.log_signal.emit(f"Analyzing structure with {cpu_count} CPU cores...")

            with ProcessPoolExecutor(max_workers=cpu_count) as executor:
                for size, indices in size_groups.items():
                    total_p = len(indices)
                    if total_p < 2: continue  
                    chunk_size = max(1, total_p // cpu_count)
                    futures = [executor.submit(analyze_chunk_worker, self.file_path, indices[i:i + chunk_size]) for i in range(0, total_p, chunk_size)]
                    
                    img_stats = {}; text_stats = {}
                    for f in futures:
                        res = f.result()
                        for img in res['imgs']:
                            h = img['hash']
                            if h not in img_stats: img_stats[h] = {'xref': img['xref'], 'pages': set()}
                            img_stats[h]['pages'].add(img['page'])
                        for block in res['texts']:
                            b_bbox = block['block_bbox']
                            if b_bbox not in text_stats:  
                                text_stats[b_bbox] = {'lines': block['lines'], 'sample_page': block['page'], 'pages': set()}
                            text_stats[b_bbox]['pages'].add(block['page'])
                    
                    threshold = total_p * 0.3
                    for h, stat in img_stats.items():
                        if len(stat['pages']) >= threshold:
                            final_img_cands[h] = {'xref': stat['xref'], 'count': len(stat['pages'])}
                    for bbox, stat in text_stats.items():
                        if len(stat['pages']) >= threshold:
                            final_text_cands[bbox] = {'lines': stat['lines'], 'sample_page': stat['sample_page'], 'count': len(stat['pages'])}

            self.log_signal.emit(f"Found {len(final_img_cands)} image types and {len(final_text_cands)} text blocks as candidates.")
            self.need_confirm.emit(final_img_cands, final_text_cands)
            while not self.is_confirmed: self.msleep(100)

            self.log_signal.emit(f"Starting redaction process...")
            total_doc = len(doc)
            for i in range(total_doc):
                page = doc[i]
                for img in page.get_images():
                    try:
                        pix = fitz.Pixmap(doc, img[0])
                        if hashlib.md5(pix.samples[:2048]).hexdigest() in self.confirmed_hashes:
                            page.delete_image(img[0])
                    except: continue
                
                page_dict = page.get_text("dict")
                for b in page_dict["blocks"]:
                    if b["type"] != 0: continue
                    for line in b["lines"]:
                        l_text = "".join([span["text"] for span in line["spans"]]).strip()
                        l_bbox = tuple([round(v/1)*1 for v in line["bbox"]])
                        if (l_text, l_bbox) in self.confirmed_lines:
                            page.add_redact_annot(line["bbox"], fill=None)
                
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
                self.progress.emit(int((i + 1) / total_doc * 100))
            
            self.log_signal.emit("Cleaning completed successfully.")
            self.finished.emit(doc)
        except Exception as e:
            self.log_signal.emit(f"ERROR: {str(e)}")
            if doc: doc.close()

class UltraAppFinal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Smart PDF Watermark Remover")
        self.scale = QApplication.primaryScreen().geometry().width() / 1920
        self.resize(int(1500 * self.scale), int(950 * self.scale))
        self.doc_orig = self.doc_clean = None
        self.file_path = ""
        # 启用拖拽功能
        self.setAcceptDrops(True)
        self.init_ui()

    def init_ui(self):
        main_widget = QWidget(); layout = QHBoxLayout(main_widget)
        sidebar = QVBoxLayout()
        self.btn_open = QPushButton("📂 Open PDF")
        self.btn_clean = QPushButton("⚡ Analyze")
        self.btn_save = QPushButton("💾 Save")
        self.btn_save.setEnabled(False)
        self.pbar = QProgressBar()
        
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setStyleSheet("""
            QTextEdit {
                background-color: #2b2b2b;
                color: #a9b7c6;
                font-family: 'Consolas', monospace;
                font-size: 11px;
                border-radius: 4px;
            }
        """)
        log_label = QLabel("<b>Activity Log:</b>")

        for b in [self.btn_open, self.btn_clean, self.btn_save]: b.setFixedHeight(int(55*self.scale))

        sidebar.addWidget(self.btn_open)
        sidebar.addWidget(self.btn_clean)
        sidebar.addWidget(self.btn_save)
        sidebar.addSpacing(20)
        sidebar.addWidget(log_label)
        sidebar.addWidget(self.log_output) 
        sidebar.addWidget(self.pbar)
        
        viewer_layout = QVBoxLayout()
        self.page_spin = QSpinBox(); self.total_label = QLabel("/ 0 Pages")
        nav = QHBoxLayout(); nav.addStretch(); nav.addWidget(self.page_spin); nav.addWidget(self.total_label); nav.addStretch()
        
        comp = QHBoxLayout()
        self.scroll_orig = QScrollArea(); self.lab_orig = QLabel("Original View (Drop PDF Here)")
        self.scroll_clean = QScrollArea(); self.lab_clean = QLabel("Cleaned View")
        for s, l in [(self.scroll_orig, self.lab_orig), (self.scroll_clean, self.lab_clean)]:
            l.setAlignment(Qt.AlignmentFlag.AlignCenter); s.setWidget(l); s.setWidgetResizable(True); s.installEventFilter(self)
        comp.addWidget(self.scroll_orig); comp.addWidget(self.scroll_clean)
        
        viewer_layout.addLayout(nav); viewer_layout.addLayout(comp)
        layout.addLayout(sidebar, 1); layout.addLayout(viewer_layout, 4)
        self.setCentralWidget(main_widget)
        
        self.btn_open.clicked.connect(self.load_file_dialog)
        self.btn_clean.clicked.connect(self.start_task)
        self.btn_save.clicked.connect(self.save_as_pdf)
        self.page_spin.valueChanged.connect(self.update_previews)
        self.log("Ready. Please open or drag a PDF file here to begin.")

    # --- 拖拽事件处理 ---
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            file_path = event.mimeData().urls()[0].toLocalFile()
            if file_path.lower().endswith(".pdf"):
                event.accept()
            else:
                event.ignore()
        else:
            event.ignore()

    def dropEvent(self, event):
        file_path = event.mimeData().urls()[0].toLocalFile()
        self.load_file_from_path(file_path)

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_output.append(f"[{timestamp}] {message}")
        self.log_output.moveCursor(QTextCursor.MoveOperation.End)

    def eventFilter(self, source, event):
        if event.type() == QEvent.Type.Wheel and self.doc_orig:
            self.page_spin.setValue(self.page_spin.value() + (-1 if event.angleDelta().y() > 0 else 1))
            return True
        return super().eventFilter(source, event)

    def load_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", "", "PDF Files (*.pdf)")
        if path:
            self.load_file_from_path(path)

    def load_file_from_path(self, path):
        """统一的文件加载逻辑"""
        try:
            if self.doc_orig:
                self.doc_orig.close()
            self.file_path = path
            self.doc_orig = fitz.open(path)
            self.page_spin.setRange(1, len(self.doc_orig))
            self.page_spin.setValue(1)
            self.total_label.setText(f"/ {len(self.doc_orig)} Pages")
            self.doc_clean = None # 重置清理后的文档
            self.btn_save.setEnabled(False)
            self.log(f"Loaded: {os.path.basename(path)}")
            self.update_previews()
        except Exception as e:
            self.log(f"Error loading file: {str(e)}")

    def update_previews(self):
        if not self.doc_orig: return
        mat = fitz.Matrix(1.5 * self.scale, 1.5 * self.scale)
        def draw(doc, lab, scroll):
            idx = self.page_spin.value()-1
            if idx >= len(doc): return
            pix = doc[idx].get_pixmap(matrix=mat)
            qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
            lab.setPixmap(QPixmap.fromImage(qimg).scaledToWidth(scroll.width() - 30, Qt.TransformationMode.SmoothTransformation))
        draw(self.doc_orig, self.lab_orig, self.scroll_orig)
        if self.doc_clean: draw(self.doc_clean, self.lab_clean, self.scroll_clean)
        else: self.lab_clean.clear(); self.lab_clean.setText("Cleaned View")

    def start_task(self):
        if not self.doc_orig:
            self.log("Warning: No file loaded.")
            return
        self.worker = MasterWorker(self.file_path)
        self.worker.progress.connect(self.pbar.setValue)
        self.worker.log_signal.connect(self.log) 
        self.worker.need_confirm.connect(self.ask_user)
        self.worker.finished.connect(self.task_done)
        self.worker.start()

    def ask_user(self, ic, tc):
        if not ic and not tc:
            self.log("System: No watermarks detected based on repetition rules.")
            QMessageBox.information(self, "No Watermarks", "None found matching the strict criteria.")
            self.worker.is_confirmed = True; return
        
        self.log("System: Waiting for user confirmation...")
        dialog = EnhancedWatermarkDialog(ic, tc, self.doc_orig, scale=self.scale, parent=self)
        if dialog.exec():  
            h, l = dialog.get_selection()
            self.worker.confirmed_hashes, self.worker.confirmed_lines = h, l
            self.log(f"User confirmed: {len(h)} images and {len(l)} text types for removal.")
        else:
            self.log("User cancelled selection.")
        self.worker.is_confirmed = True

    def task_done(self, doc):
        self.doc_clean = doc; self.update_previews(); self.btn_save.setEnabled(True)
        self.log("UI updated with cleaned version. Ready to save.")

    def save_as_pdf(self):
        orig_name = os.path.basename(self.file_path)
        default_save_name = f"cleaned_{orig_name}"
        path, _ = QFileDialog.getSaveFileName(self, "Save", default_save_name, "PDF Files (*.pdf)")
        if path:
            self.log(f"Saving to: {path} ...")
            self.doc_clean.save(path, garbage=4, deflate=True)
            self.log("File saved and optimized.")
            QMessageBox.information(self, "Success", "File saved successfully!")

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    app = QApplication(sys.argv); window = UltraAppFinal(); window.show(); sys.exit(app.exec())
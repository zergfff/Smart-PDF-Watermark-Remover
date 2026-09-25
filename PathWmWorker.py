"""
PathWmWorker - 路径水印删除后台线程
====================================
全文扫描 PDF 内容流，删除匹配指定属性（填充色、线条色、透明度）的路径块。
并用 PyMuPDF 红化兜底删除命中图元，处理容器/嵌套路径。
"""
from __future__ import annotations
import os
import time
import tempfile
import traceback
from typing import Optional

from PyQt6 import QtCore

from path_wm import (
    get_page_stream, set_page_stream,
    remove_matching_paths,
)


class PathWmWorker(QtCore.QThread):
    """路径水印删除 Worker。"""

    progress = QtCore.pyqtSignal(int, str)
    done = QtCore.pyqtSignal(object, str)
    error = QtCore.pyqtSignal(str)

    def __init__(self, pdf_path: str, settings: dict, parent=None):
        super().__init__(parent)
        self.pdf_path = pdf_path
        self.settings = settings
        self.out_path = self._make_out_path()
        self._cancel = False

    def _make_out_path(self):
        base = os.path.basename(self.pdf_path)
        stem = os.path.splitext(base)[0]
        return os.path.join(tempfile.gettempdir(), f"{stem}_pathwm.pdf")

    def _make_flatten_path(self):
        base = os.path.basename(self.pdf_path)
        stem = os.path.splitext(base)[0]
        return os.path.join(tempfile.gettempdir(), f"{stem}_pathwm_flatten.pdf")

    def cancel(self):
        self._cancel = True

    def run(self):
        import fitz  # PyMuPDF - 延迟加载，避免模块导入时依赖
        import pikepdf
        t0 = time.time()
        self.progress.emit(0, "打开 PDF...")
        try:
            self._run_impl(fitz, pikepdf, t0)
        except Exception as e:
            self.error.emit(f"路径水印删除失败: {e}\n{traceback.format_exc()}")

    def _run_impl(self, fitz, pikepdf, t0):
        # 0. 先展平页面容器，尽量把 Form XObject 里的路径纳入页面内容流。
        try:
            pre_doc = fitz.open(self.pdf_path)
            for pno in range(pre_doc.page_count):
                page = pre_doc[pno]
                try:
                    page.clean_contents()
                except Exception:
                    pass
            flattened_path = self._make_flatten_path()
            pre_doc.save(flattened_path, garbage=4, deflate=True)
            pre_doc.close()
            self.pdf_path = flattened_path
        except Exception as e:
            self.progress.emit(0, f"展平容器失败，将继续直接删除: {e}")

        # 1. pikepdf 打开并删除内容流路径块
        content_removed_total = 0
        with pikepdf.open(self.pdf_path) as pdf:
            n_pages = len(pdf.pages)
            self.progress.emit(0, f"扫描 {n_pages} 页...")

            match_fill = self.settings.get('fill_color') is not None
            match_stroke = self.settings.get('stroke_color') is not None
            match_opacity = self.settings.get('fill_alpha') is not None
            fill_color = self.settings.get('fill_color')
            stroke_color = self.settings.get('stroke_color')
            opacity = self.settings.get('fill_alpha')

            for pno in range(n_pages):
                if self._cancel:
                    return

                page = pdf.pages[pno]
                content = get_page_stream(pdf, page)
                if not content:
                    continue

                new_content, removed = remove_matching_paths(
                    content,
                    fill_color=fill_color if match_fill else None,
                    stroke_color=stroke_color if match_stroke else None,
                    opacity=opacity if match_opacity else None,
                    match_fill=match_fill,
                    match_stroke=match_stroke,
                    match_opacity=match_opacity,
                )

                if removed > 0:
                    ok = set_page_stream(pdf, page, new_content)
                    if ok:
                        content_removed_total += removed
                        self.progress.emit(
                            int((pno + 1) / n_pages * 100),
                            f"Page {pno+1}/{n_pages}: 删除 {removed} 个路径 (累计 {content_removed_total})"
                        )
                    else:
                        self.progress.emit(
                            int((pno + 1) / n_pages * 100),
                            f"Page {pno+1}/{n_pages}: 写入失败"
                        )

            # 2. 保存内容流删除结果
            self.progress.emit(95, "保存中...")
            pdf.save(self.out_path)

        # 3. 已移除"几何兜底"（红化框 + PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED）。
        #    原因：该兜底按"是否碰到红化框"删除矢量，**颜色完全不参与判定**，
        #    因此选中灰色(191)填充时，与框相交的白色(255)填充也会被一起删掉
        #    （实测 page0：白 1→0、灰 7→6）。颜色筛选只在内容流删除那一步生效，
        #    兜底把这一步的成果覆盖了。内容流删除已按 fill_color/stroke_color
        #    精确匹配（容差 0.02），且第 0 步已展平容器，兜底无存在必要。
        total_removed = content_removed_total
        self.progress.emit(96, f"内容流删除完成: {total_removed} 个路径（已不做几何兜底）")

        # 4. 用 fitz 重新打开用于预览
        doc = fitz.open(self.out_path)
        elapsed = time.time() - t0
        self.progress.emit(100, f"完成: 删除 {total_removed} 个路径 ({elapsed:.1f}s)")
        self.done.emit(doc, self.out_path)

    @staticmethod
    def _color_close(a, b, tol=0.02):
        if not a or not b:
            return False
        if isinstance(a, str) or isinstance(b, str):
            return False
        try:
            return all(abs(float(a[i]) - float(b[i])) <= tol for i in range(3))
        except Exception:
            return False

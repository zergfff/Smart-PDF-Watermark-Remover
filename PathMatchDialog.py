"""
PathMatchDialog - 路径匹配对话框
=================================
用户勾选属性（填充色、透明度、线条色、路径类型），
全文扫描匹配的路径并删除。

UI 布局：
  [填充色] #FF0000  [选择颜色...] [x]   [透明度] 100%
  [线条色] #000000  [选择颜色...] [x]   [线宽   ] 0.5
  [路径类型] [x]填充  [x]描边
  [x] 全文删除

  ┌────────────────────────────────────────┐
  │ 预览：匹配到 15 个路径                   │
  └────────────────────────────────────────┘

         [取消]  [确定并删除]
"""
from __future__ import annotations
import os
import tempfile
from PyQt6 import QtCore, QtGui, QtWidgets



class ColorCheckRow(QtWidgets.QWidget):
    """一行颜色 + 透明度选择器"""
    color_changed = QtCore.pyqtSignal(object)  # (rgb_tuple or None, alpha)

    def __init__(self, label: str, parent=None, default_rgb=(0, 0, 0), default_alpha=1.0, default_checked=False):
        super().__init__(parent)
        self.default_rgb = default_rgb
        self.default_alpha = default_alpha
        self.default_checked = default_checked

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        lab = QtWidgets.QLabel(label)
        lab.setStyleSheet("font-size: 11px; font-weight: 600;")
        lay.addWidget(lab)

        # 复选框：是否启用
        self.chk = QtWidgets.QCheckBox()
        self.chk.setChecked(default_checked)
        self.chk.toggled.connect(self._on_toggled)
        lay.addWidget(self.chk)

        # 颜色按钮
        self.btn_color = QtWidgets.QPushButton()
        self.btn_color.setFixedSize(36, 22)
        self.btn_color.clicked.connect(self._on_pick_color)
        lay.addWidget(self.btn_color)

        # 色值文本
        self.txt_hex = QtWidgets.QLabel()
        self.txt_hex.setStyleSheet("font-size: 10px; color: #888;")
        lay.addWidget(self.txt_hex)

        self._set_color_btn()

        # 透明度滑杆
        self.spin_alpha = QtWidgets.QDoubleSpinBox()
        self.spin_alpha.setRange(0.0, 1.0)
        self.spin_alpha.setSingleStep(0.1)
        self.spin_alpha.setDecimals(2)
        self.spin_alpha.setValue(default_alpha)
        self.spin_alpha.setFixedWidth(50)
        lay.addWidget(self.spin_alpha)

        lay.addStretch()

        # 更新显示
        self._on_toggled(default_checked)

    def _on_toggled(self, checked: bool):
        self.btn_color.setEnabled(checked)
        self.spin_alpha.setEnabled(checked)
        self.txt_hex.setEnabled(checked)
        self.color_changed.emit(self._get_value())

    def _on_pick_color(self):
        cur = QtGui.QColor(*[int(v * 255) for v in self._current_rgb])
        c = QtGui.QColorDialog.getColor(cur, self, "选择颜色")
        if c.isValid():
            self._current_rgb = (c.red() / 255.0, c.green() / 255.0, c.blue() / 255.0)
            self._set_color_btn()
            self.color_changed.emit(self._get_value())

    def _set_color_btn(self):
        r, g, b = self._current_rgb
        hex_str = f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
        self.btn_color.setStyleSheet(
            f"background-color: {hex_str}; border: 2px solid #333; border-radius: 3px;"
        )
        self.txt_hex.setText(hex_str)

    @property
    def _current_rgb(self):
        if not hasattr(self, '_rgb'):
            self._rgb = self.default_rgb
        return self._rgb

    @_current_rgb.setter
    def _current_rgb(self, val):
        self._rgb = val
        r, g, b = val
        hex_str = f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
        self.btn_color.setStyleSheet(
            f"background-color: {hex_str}; border: 2px solid #333; border-radius: 3px;"
        )
        self.txt_hex.setText(hex_str)

    def _get_value(self):
        if not self.chk.isChecked():
            return None
        return (self._current_rgb, self.spin_alpha.value())

    def set_color(self, rgb: tuple):
        self._current_rgb = rgb
        self._set_color_btn()
        self.color_changed.emit(self._get_value())

    def get_settings(self):
        return self._get_value()


class PathMatchDialog(QtWidgets.QDialog):
    """路径匹配对话框 - 勾选属性，全文删除匹配路径"""

    def __init__(self, doc, page_no: int, pdf_path: str, page_rect, scale: int, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.page_no = page_no
        self.pdf_path = pdf_path
        self.page_rect = page_rect
        self.scale = scale
        self._refreshing = False

        self.setWindowTitle("路径水印 - 选择属性删除")
        self.resize(520, 580)
        self._matches_cache = []
        self._build_ui()
        QtCore.QTimer.singleShot(0, self._refresh_preview)

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(12, 12, 12, 12)

        # 标题
        title = QtWidgets.QLabel("路径属性匹配删除")
        title.setStyleSheet("font-size: 14px; font-weight: 700; color: #1890ff;")
        root.addWidget(title)

        hint = QtWidgets.QLabel("勾选要匹配的属性，确定后全文删除匹配路径")
        hint.setStyleSheet("font-size: 10px; color: #888;")
        root.addWidget(hint)

        # 填充色行
        self.fill_row = ColorCheckRow("填充色", default_rgb=(0, 0, 0), default_alpha=1.0, default_checked=True)
        self.fill_row.color_changed.connect(self._refresh_preview)
        root.addWidget(self.fill_row)

        # 线条色行
        self.stroke_row = ColorCheckRow("线条色", default_rgb=(0, 0, 0), default_alpha=1.0, default_checked=False)
        self.stroke_row.color_changed.connect(self._refresh_preview)
        root.addWidget(self.stroke_row)

        # 路径类型
        type_row = QtWidgets.QHBoxLayout()
        type_row.setSpacing(8)
        type_lab = QtWidgets.QLabel("路径类型")
        type_lab.setStyleSheet("font-size: 11px; font-weight: 600;")
        type_row.addWidget(type_lab)

        self.chk_fill = QtWidgets.QCheckBox("填充")
        self.chk_fill.setChecked(True)
        self.chk_fill.setStyleSheet("font-size: 11px;")
        type_row.addWidget(self.chk_fill)

        self.chk_stroke = QtWidgets.QCheckBox("描边")
        self.chk_stroke.setChecked(True)
        self.chk_stroke.setStyleSheet("font-size: 11px;")
        type_row.addWidget(self.chk_stroke)

        self.chk_both = QtWidgets.QCheckBox("两者")
        self.chk_both.setChecked(True)
        self.chk_both.setStyleSheet("font-size: 11px;")
        type_row.addWidget(self.chk_both)

        self.chk_fill.toggled.connect(lambda: self._refresh_preview())
        self.chk_stroke.toggled.connect(lambda: self._refresh_preview())
        self.chk_both.toggled.connect(lambda: self._refresh_preview())
        type_row.addStretch()
        root.addLayout(type_row)

        # 全文删除
        self.chk_all = QtWidgets.QCheckBox("全文删除（所有页面）")
        self.chk_all.setChecked(True)
        self.chk_all.setStyleSheet("font-size: 11px; font-weight: 600; color: #e74c3c;")
        self.chk_all.toggled.connect(lambda: self._refresh_preview())
        root.addWidget(self.chk_all)

        # 预览
        root.addWidget(QtWidgets.QLabel(""))
        self.preview_label = QtWidgets.QLabel("正在扫描...")
        self.preview_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(80)
        self.preview_label.setStyleSheet(
            "background: #f5f5f5; border: 1px solid #ddd; border-radius: 4px; font-size: 11px;"
        )
        root.addWidget(self.preview_label)
        # 按钮
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch()
        btn_cancel = QtWidgets.QPushButton("取消")
        btn_cancel.setFixedWidth(90)
        btn_cancel.clicked.connect(self.reject)
        btns.addWidget(btn_cancel)

        btn_ok = QtWidgets.QPushButton("确定并删除")
        btn_ok.setFixedWidth(120)
        btn_ok.setStyleSheet(
            "background: #1890ff; color: white; font-weight: 700; border: none; border-radius: 4px; padding: 6px;"
        )
        btn_ok.clicked.connect(self.accept)
        btns.addWidget(btn_ok)
        root.addLayout(btns)

    def _refresh_preview(self):
        """预览当前页符合策略的路径：渲染页面并画出命中框。"""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            import fitz
            page = self.doc[self.page_no]
            scale = max(0.1, min(1.0, float(self.scale or 1.0)))
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            img = QtGui.QImage(
                bytes(pix.samples),
                pix.width,
                pix.height,
                pix.stride,
                QtGui.QImage.Format.Format_RGB888,
            ).copy()
            if img.isNull():
                raise RuntimeError("failed to create QImage from pixmap samples")


            painter = QtGui.QPainter(img)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

            hits = []
            drawings = page.get_drawings(extended=True)
            for d in drawings:
                r = d.get('rect')
                if not r:
                    continue
                fill = d.get('fill')
                stroke = d.get('color')
                if self.fill_row.get_settings():
                    fc = self.fill_row.get_settings()[0]
                    if fill is None or not self._color_close(fill, fc):
                        continue
                if self.stroke_row.get_settings():
                    sc = self.stroke_row.get_settings()[0]
                    if stroke is None or not self._color_close(stroke, sc):
                        continue
                hits.append(r)

            if hits:
                painter.setPen(QtGui.QPen(QtGui.QColor(255, 77, 79), 2))
                painter.setBrush(QtGui.QBrush(QtGui.QColor(255, 77, 79, 40)))
                for r in hits:
                    rect = QtCore.QRectF(
                        r.x0 * scale,
                        r.y0 * scale,
                        r.width * scale,
                        r.height * scale,
                    )
                    painter.drawRect(rect)
            else:
                painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 160)))
                painter.drawText(QtCore.QRectF(0, 0, pix.width, pix.height), QtGui.Qt.AlignmentFlag.AlignCenter, "当前页未命中")
            painter.end()

            # 预览保持原始宽高比，避免被标签拉伸变形
            pm = QtGui.QPixmap.fromImage(img)
            target_size = self.preview_label.size()
            if target_size.width() <= 0 or target_size.height() <= 0:
                target_size = QtCore.QSize(360, 220)
            self.preview_label.setScaledContents(True)
            self.preview_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            pm = pm.scaled(target_size, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
            self.preview_label.setFixedSize(pm.size())
            self.preview_label.setPixmap(pm)
            self.preview_label.setText("")
            self.preview_label.setStyleSheet("background: #f5f5f5; border: 1px solid #ddd; border-radius: 4px; font-size: 11px;")
        except Exception as e:
            self.preview_label.setScaledContents(False)
            self.preview_label.setPixmap(QtGui.QPixmap())
            self.preview_label.setText(f"预览失败: {e}")
            self.preview_label.setStyleSheet(
                "background: #fff2f0; border: 1px solid #ffccc7; border-radius: 4px; font-size: 11px; color: #cf1322;"
            )
        finally:
            self._refreshing = False

    def _color_close(self, a, b, tol=0.02):
        if not a or not b:
            return False
        if isinstance(a, str) or isinstance(b, str):
            return False
        try:
            return all(abs(float(a[i]) - float(b[i])) <= tol for i in range(3))
        except Exception:
            return False

    def get_settings(self) -> dict:
        """获取匹配设置"""
        fill = self.fill_row.get_settings()
        stroke = self.stroke_row.get_settings()

        # 路径类型
        path_types = set()
        if self.chk_fill.isChecked():
            path_types.add('f')
        if self.chk_stroke.isChecked():
            path_types.add('s')
        if self.chk_both.isChecked():
            path_types.update({'f', 's'})

        return {
            'fill_color': fill[0] if fill else None,
            'fill_alpha': fill[1] if fill else None,
            'stroke_color': stroke[0] if stroke else None,
            'stroke_alpha': stroke[1] if stroke else None,
            'path_types': path_types,
            'delete_all_pages': self.chk_all.isChecked(),
        }

    def get_output_path(self) -> str:
        base = os.path.basename(self.pdf_path)
        stem = os.path.splitext(base)[0]
        out = os.path.join(tempfile.gettempdir(), f"{stem}_pathwm.pdf")
        return out
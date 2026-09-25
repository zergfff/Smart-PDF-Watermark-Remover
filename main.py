import sys
import os
import json
import math
import time
import tempfile
import uuid
import subprocess
import importlib
import traceback
import threading

from datetime import datetime
from concurrent.futures import ProcessPoolExecutor

# --- Debug 模式（崩溃诊断）---
# 启用方式：命令行 --debug 或环境变量 PDF_DEBUG=1
# 日志输出到 %APPDATA%/ExtremePDFCleaner/logs/debug.log
#
# faulthandler 固定写文件、绝不写 stderr：
# PyInstaller -w（无控制台）构建里 sys.stderr is None，
# 直接调用 faulthandler.enable() 会抛 RuntimeError: sys.stderr is None，
# 窗口版 EXE 会在启动瞬间崩溃。写文件同时也让原生崩溃留下栈。
import faulthandler

CRASH_LOG_PATH = os.path.join(
    os.environ.get('APPDATA', os.path.expanduser('~')),
    'ExtremePDFCleaner', 'logs', 'crash.log'
)
def _ensure_crash_log():
    """确保 crash.log 所在目录存在（hook 里兜底用）。"""
    try:
        os.makedirs(os.path.dirname(CRASH_LOG_PATH), exist_ok=True)
    except Exception:
        pass


_fault_file = None
try:
    try:
        _ensure_crash_log()
        _fault_file = open(CRASH_LOG_PATH, 'a', buffering=1, encoding='utf-8', errors='replace')
        faulthandler.enable(file=_fault_file, all_threads=True)
    except Exception:
        # 退路：仅在确实存在 stderr（控制台运行）时才用 stderr
        try:
            if sys.stderr is not None:
                faulthandler.enable()
        except Exception:
            pass
except Exception:
    pass

DEBUG = os.environ.get('PDF_DEBUG', '').lower() in ('1', 'true', 'yes', 'on')
if '--debug' in sys.argv:
    DEBUG = True
    sys.argv = [a for a in sys.argv if a != '--debug']

DEBUG_LOG_PATH = os.path.join(
    os.environ.get('APPDATA', os.path.expanduser('~')),
    'ExtremePDFCleaner', 'logs', 'debug.log'
)

def _dbg(msg, *args):
    """写 debug 日志到文件（线程安全，立即 flush，不受 GUI 影响）。"""
    if not DEBUG:
        return
    try:
        if args:
            msg = msg % args
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        tid = threading.get_ident() % 10000
        line = f'[{ts}] [T{tid}] {msg}'
        with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
            f.flush()
    except Exception:
        pass  # debug 日志本身不能崩

def _dbg_exc(ctx, exc):
    """记录异常到 debug 日志。"""
    _dbg(f'EXCEPTION in {ctx}: {type(exc).__name__}: {exc}')
    _dbg(traceback.format_exc())

# 全局未捕获异常 hook
# 重要：无条件写日志（不再只在 --debug 时写）。
# PyQt6 在槽/虚函数里遇到未捕获异常时会调用 sys.excepthook，随后 qFatal→abort；
# 装了这个 hook 才能把 traceback 留在 crash.log 里，否则窗口版 EXE 会静默闪退。
_orig_excepthook = sys.excepthook
def _debug_excepthook(exc_type, exc_value, exc_tb):
    try:
        txt = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    except Exception:
        txt = f'{exc_type.__name__}: {exc_value}'
    try:
        _dbg(f'UNCAUGHT: {exc_type.__name__}: {exc_value}')
        _dbg(txt)
    except Exception:
        pass
    try:
        _ensure_crash_log()
        with open(CRASH_LOG_PATH, 'a', encoding='utf-8', errors='replace') as f:
            f.write(f'\n[UNCAUGHT EXCEPTION {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}]\n')
            f.write(txt)
            f.write('\n')
    except Exception:
        pass
    _orig_excepthook(exc_type, exc_value, exc_tb)
sys.excepthook = _debug_excepthook

# --- 依赖自动检查与安装（在任何第三方库导入之前）---
# 缺失依赖会阻止主程序启动；此模块只用标准库，因此任何第三方库缺失都检测得到
REQUIRED_DEPS = {
    "PyMuPDF":   {"import_name": "pymupdf", "package": "pymupdf"},
    "pikepdf":   {"import_name": "pikepdf", "package": "pikepdf"},
    "PyQt6":     {"import_name": "PyQt6",  "package": "PyQt6"},
    "xxhash":    {"import_name": "xxhash", "package": "xxhash"},
    "opencv-python": {"import_name": "cv2", "package": "opencv-python-headless"},
    "numpy":     {"import_name": "numpy",  "package": "numpy"},
}


# 冻结（PyInstaller）构建标记。
# 关键：冻结包里 sys.executable 就是本 EXE，一旦走 pip 安装路径，
# subprocess 会把 EXE 自己再启动一遍（无 -m pip 也照样启动 GUI），
# 子进程又发现缺包 → 再启动自身 → 递归自启动；
# 而且 -w（无控制台）构建里 sys.stdin/stdout/stderr 全是 None，
# 在 input()/print() 处直接抛异常 → 进程 fail-fast 退出（0xC0000409），
# 界面上只看到一句 Traceback。因此冻结构建必须完全跳过依赖安装。
FROZEN = bool(getattr(sys, "frozen", False))


def _startup_log(msg):
    """启动阶段的日志（此刻 app 日志器尚未初始化，直接追加到当日日志文件）。"""
    try:
        path = os.path.join(
            os.environ.get('APPDATA', os.path.expanduser('~')),
            'ExtremePDFCleaner', 'logs',
            'app_%s.log' % datetime.now().strftime('%Y%m%d')
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


def check_dependencies(quiet=False):
    """检查 REQUIRED_DEPS 里的模块，返回缺失的 (name, package) 列表。"""
    missing = []
    for name, spec in REQUIRED_DEPS.items():
        try:
            importlib.import_module(spec["import_name"])
        except Exception:
            missing.append((name, spec["package"]))
    return missing


def install_missing_packages(missing):
    """用 pip 安装缺失包，返回 (success, output)。"""
    if FROZEN:
        # 冻结包里 sys.executable 是本 EXE：跑 pip 等于递归启动自身 → 崩溃。
        return False, ("frozen build: pip 自安装已禁用"
                       "（sys.executable 指向本 EXE，会递归启动自身）")
    pkgs = [pkg for _, pkg in missing]
    if not pkgs:
        return True, "no packages to install"
    # 找可用的 Python 解释器
    exe = sys.executable
    exe_name = os.path.basename(exe).lower()
    if exe_name in ("pythonw.exe", "pythonw", "wpython.exe"):
        # GUI 模式下 sys.executable 是 pythonw.exe，pip 需要控制台 python
        for alt in ["python.exe", "python3.exe"]:
            cand = os.path.join(os.path.dirname(exe), alt)
            if os.path.isfile(cand):
                exe = cand
                break
        else:
            # 退回到系统 PATH
            import shutil
            found = shutil.which("python") or shutil.which("python3")
            if found:
                exe = found
            else:
                return False, f"找不到 python.exe（当前 sys.executable={exe}）"
    cmd = [exe, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", *pkgs]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0:
            return True, proc.stdout or "(installed ok)"
        return False, (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "pip install 超时（>10 分钟）"
    except Exception as e:
        return False, f"install error: {e!r}"


def confirm_and_install_deps():
    """缺失依赖时弹框确认并安装。返回 True 表示继续，False 表示用户取消或安装失败。
    即使 PyQt6 缺失（因此无 GUI）也能通过命令行提示 + 直接安装 + 命令行反馈完成流程。
    """
    if FROZEN:
        return True   # 冻结包自带运行时，不做依赖检查/安装
    missing = check_dependencies()
    if not missing:
        return True
    pkg_list = "\n".join([f"  - {n}  (安装: {p})" for n, p in missing])

    # 尝试用 PyQt6 弹确认框；PyQt6 缺失时退回命令行提示
    use_gui = True
    try:
        from PyQt6.QtWidgets import QMessageBox, QDialog, QProgressBar, QLabel, QApplication, QVBoxLayout
        ans = QMessageBox.question(
            None, "缺少依赖包",
            f"以下依赖未安装，是否现在通过 pip 下载并安装？（需联网）\n\n{pkg_list}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ans != QMessageBox.StandardButton.Yes:
            QMessageBox.critical(None, "已取消", "已取消依赖安装，程序将退出。")
            return False
        # 安装过程耗时，弹一个"安装中"进度框
        dlg = QDialog()
        dlg.setWindowTitle("安装依赖")
        lbl = QLabel("正在通过 pip 安装依赖包…\n请等待 1-3 分钟，视网络速度而定。\n\n" + pkg_list)
        bar = QProgressBar(); bar.setRange(0, 0)  # 忙碌动画
        layout = QVBoxLayout(); layout.addWidget(lbl); layout.addWidget(bar); dlg.setLayout(layout)
        dlg.setFixedWidth(480)
        dlg.show(); dlg.raise_()
        QApplication.processEvents()
        dlg._close = dlg.close
    except Exception:
        # PyQt6 缺失，退回命令行
        use_gui = False
        import datetime as _dt
        print("=" * 70)
        print(f"[{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 检测到缺失依赖:")
        print(pkg_list)
        print("=" * 70)
        print("缺少 PyQt6，无法显示图形界面。是否自动下载并安装上述依赖？")
        print("  Y = 安装并继续启动    N = 退出程序")
        try:
            ans = input("> ").strip().upper()
        except EOFError:
            ans = "Y"
        if ans not in ("Y", "YES", ""):
            print("用户取消，程序退出。")
            return False
        print("正在通过 pip 安装依赖包（约 1-3 分钟）…")

    ok, output = install_missing_packages(missing)
    if use_gui:
        try:
            dlg.close()
        except Exception:
            pass
    if not ok:
        msg = f"依赖安装失败：\n\n{output[-2000:]}\n\n请手动执行：\n  {sys.executable} -m pip install {' '.join(p for _, p in missing)}"
        if use_gui:
            try:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.critical(None, "安装失败", msg)
            except Exception:
                print(msg)
        else:
            print(msg)
        return False

    # 安装成功后验证
    still = check_dependencies()
    if still:
        msg = ("安装完成但以下模块仍无法导入（可能需要重启程序）：\n"
               + "\n".join(f"  - {n}" for n, _ in still))
        if use_gui:
            try:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.critical(None, "仍有缺失", msg)
            except Exception:
                print(msg)
        else:
            print(msg)
        return False

    if use_gui:
        try:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(None, "安装完成", "所有依赖已安装成功，程序将继续启动。")
        except Exception:
            pass
    else:
        print("所有依赖已安装成功，程序将继续启动。")
    return True

# ---- 缺失依赖自动检测与安装（必须在 PyQt6/重型库 import 之前）----
# 用 check_dependencies() 判断：若空则跳过；否则调用 confirm_and_install_deps()
# 后者内部有命令行兜底，即使 PyQt6 缺失也能提示用户并安装
try:
    if FROZEN:
        # 冻结构建：自带 PyQt6/pikepdf/pymupdf/xxhash。
        # 这里既不检查也不安装依赖：
        #   - 安装会调用 sys.executable（=本 EXE）→ 递归自启动 → fail-fast 崩溃；
        #   - 检查会 import cv2/numpy，从 onefile 归档解压导入要几十秒，白拖慢启动。
        # 缺可选模块时由对应功能自己提示（色彩水印）。
        _missing_at_startup = []
        _startup_log(">>> 冻结版启动：跳过依赖检查/自安装（可选模块缺失不影响其它功能）")
    else:
        _missing_at_startup = check_dependencies()
    if _missing_at_startup:
        _ok = confirm_and_install_deps()
        if not _ok:
            sys.exit(0)
        # 安装后再次校验
        _still_missing = check_dependencies()
        if _still_missing:
            print("[FATAL] 依赖安装后仍有缺失：", _still_missing)
            sys.exit(1)
except SystemExit:
    raise
except Exception as _e:
    traceback.print_exc()
    print(f"[FATAL] 依赖初始化失败: {_e!r}")
    sys.exit(1)

from PyQt6.QtWidgets import (QApplication, QMainWindow, QPushButton, QVBoxLayout, QHBoxLayout,
                             QWidget, QFileDialog, QLabel, QProgressBar, QMessageBox, QTextEdit,
                             QDialog, QCheckBox, QScrollArea, QFrame, QSpinBox, QLineEdit, QComboBox,
                             QMenu, QScrollBar, QRubberBand, QRadioButton, QButtonGroup, QSlider)
from PyQt6.QtGui import QPixmap, QImage, QTextCursor, QPainter, QPen, QColor, QPalette, QTransform, QBrush
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QEvent, QSize, QRect, QPoint, QRectF

# --- 环境适配 ---
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"

# 重型库(占位)：后台线程延迟加载，加快窗口出现
fitz = None
pikepdf = None
_dw = None
# xxhash 是必需的（用于图片哈希去重），启动时立即导入
import xxhash as xxhash

# ---- 缺失依赖自动检测与安装（已在 PyQt6 import 之前执行过；此处仅做最终校验）----
# 若前面已确认依赖齐全，此处保持空操作

def _load_heavy_libs():
    global fitz, pikepdf, _dw
    import pymupdf as fitz
    import pikepdf
    import pdf_dewatermark as _dw

# --- 多语言配置 ---
TRANSLATIONS = {
    "zh": {
        "title": "Extreme PDF Cleaner - 极速清理工具",
        "open": "📂 载入 PDF",
        "clean": "⚡ 元素水印",
        "img_wm": "🎨 色彩水印",
        "dpi_wm": "🎯 图像水印",
        "dpi_wm_select": "🎯 点击选择水印图片",
        "save": "💾 保存",
        "save_as": "💾 另存为",
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
        "adobe_header": "Adobe Character Watermark",
        "adobe_hint": "Each page has many /Artifact <<Subtype/Watermark>> blocks. All will be removed together.",
        "adobe_count": "Blocks",
        "annot_header": "Annotation Watermark",
        "annot_hint": "Stamp / FreeText / Widget annotations marked as watermark. Will be deleted as objects.",
        "annot_count": "Annotations",
        "ocg_header": "OCG Layer Watermark",
        "ocg_hint": "Optional Content Group layers named as watermark/draft/stamp. Layer will be hidden & removed.",
        "ocg_count": "Layer refs",
        "xobj_header": "Form XObject Watermark",
        "xobj_hint": "Form XObject with /Watermark key in dictionary. Object will be cleared.",
        "xobj_count": "Objects",
        "extgs_header": "ExtGState Alpha Watermark",
        "extgs_hint": "Low-alpha (ca/CA < 0.3) ExtGState entries — likely invisible/semi-transparent watermarks.",
        "extgs_count": "GState refs",
        "type3_header": "Type3 Font Watermark",
        "type3_hint": "Type3 fonts (glyph = custom vector path). Often used for watermark text.",
        "type3_count": "Fonts",
        "nested_header": "Nested Form XObject",
        "nested_hint": "Form XObject referencing another Form (watermark may be nested inside).",
        "nested_count": "Nested Forms",
        "uri_header": "URI Link Annotation",
        "uri_hint": "Invisible URL link annotations (often leftover from removed watermark images).",
        "uri_count": "URI links",
        "pattern_header": "Pattern / Shading",
        "pattern_hint": "Pattern & Shading objects — may be watermark textures or backgrounds.",
        "pattern_count": "Objects",
        "struct_header": "Structure Artifact",
        "struct_hint": "Tagged PDF Artifact nodes (/StructureTreeRoot/S/Artifact).",
        "struct_count": "Artifacts",
        "meta_header": "Metadata / XMP",
        "meta_hint": "Info dict & XMP metadata — contains producer/keywords/hidden provenance.",
        "meta_count": "Fields",
        "outline_header": "Outline Stroke Watermark",
        "outline_hint": "BT..ET blocks drawing only paths (re/S) without text — likely outlined watermark text.",
        "outline_count": "Blocks",
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
        "clean": "⚡ Element Watermark",
        "img_wm": "🎨 Color Watermark",
        "dpi_wm": "🎯 Image Watermark",
        "dpi_wm_select": "🎯 Click a watermark image",
        "save": "💾 Save",
        "save_as": "💾 Save As",
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
        "adobe_header": "Adobe Character Watermark",
        "adobe_hint": "Each page has many /Artifact <<Subtype/Watermark>> blocks. All will be removed together.",
        "adobe_count": "Blocks",
        "annot_header": "Annotation Watermark",
        "annot_hint": "Stamp / FreeText / Widget annotations marked as watermark. Will be deleted as objects.",
        "annot_count": "Annotations",
        "ocg_header": "OCG Layer Watermark",
        "ocg_hint": "Optional Content Group layers named as watermark/draft/stamp. Layer will be hidden & removed.",
        "ocg_count": "Layer refs",
        "xobj_header": "Form XObject Watermark",
        "xobj_hint": "Form XObject with /Watermark key in dictionary. Object will be cleared.",
        "xobj_count": "Objects",
        "extgs_header": "ExtGState Alpha Watermark",
        "extgs_hint": "Low-alpha (ca/CA < 0.3) ExtGState entries — likely invisible/semi-transparent watermarks.",
        "extgs_count": "GState refs",
        "type3_header": "Type3 Font Watermark",
        "type3_hint": "Type3 fonts (glyph = custom vector path). Often used for watermark text.",
        "type3_count": "Fonts",
        "nested_header": "Nested Form XObject",
        "nested_hint": "Form XObject referencing another Form (watermark may be nested inside).",
        "nested_count": "Nested Forms",
        "uri_header": "URI Link Annotation",
        "uri_hint": "Invisible URL link annotations (often leftover from removed watermark images).",
        "uri_count": "URI links",
        "pattern_header": "Pattern / Shading",
        "pattern_hint": "Pattern & Shading objects — may be watermark textures or backgrounds.",
        "pattern_count": "Objects",
        "struct_header": "Structure Artifact",
        "struct_hint": "Tagged PDF Artifact nodes (/StructureTreeRoot/S/Artifact).",
        "struct_count": "Artifacts",
        "meta_header": "Metadata / XMP",
        "meta_hint": "Info dict & XMP metadata — contains producer/keywords/hidden provenance.",
        "meta_count": "Fields",
        "outline_header": "Outline Stroke Watermark",
        "outline_hint": "BT..ET blocks drawing only paths (re/S) without text — likely outlined watermark text.",
        "outline_count": "Blocks",
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

def _pix_hash(pix):
    """Pixel hash used by BOTH analysis and deletion. Must stay identical."""
    if pix.size > 1024 * 1024:  # > 1MB: subsample
        return xxhash.xxh64(pix.samples[::4]).hexdigest()
    return xxhash.xxh64(pix.samples).hexdigest()


def analyze_page(fitz, doc, i):
    """扫描单页：解图片哈希 + 取 bbox + 提取文本块。

    纯函数，可在多线程里对**不同 page 索引**并发调用。
    """
    page = doc[i]
    rect = page.rect
    pw, ph = round(rect.width, 1), round(rect.height, 1)
    page_data = {'index': i, 'size_key': (pw, ph), 'imgs': [], 'texts': []}
    bbox_by_xref = {}
    try:
        for ii in page.get_image_info(xrefs=True):
            xref = ii.get('xref')
            bbox = ii.get('bbox')
            if xref is not None and bbox:
                bbox_by_xref[int(xref)] = tuple(bbox)
    except Exception:
        pass
    for img in page.get_images(full=True):
        try:
            pix = fitz.Pixmap(doc, img[0])
            h = _pix_hash(pix)
            page_data['imgs'].append({
                'hash': h, 'xref': img[0],
                'w': img[2], 'h': img[3],
                'bbox': bbox_by_xref.get(img[0]),
            })
        except Exception as e:
            page_data.setdefault('_errs', []).append(f"Page {i} image error: {e}")
            continue
    blocks = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
    for b in blocks:
        if b["type"] != 0: continue
        for line in b["lines"]:
            spans = line["spans"]
            merged = []
            for sp in spans:
                txt = "".join(ch.get("c", "") for ch in sp.get("chars", [])).strip()
                if not txt:
                    continue
                sz = round(sp.get("size", 0), 1)
                sp_color = sp.get("color")
                if merged and merged[-1][0] == sz and merged[-1][3] == sp_color and abs(merged[-1][4] - (sp.get("bbox") or [0,0,0,0])[1]) < 2:
                    merged[-1][1] += txt
                    b0 = merged[-1][2]
                    b1 = sp.get("bbox") or [0, 0, 0, 0]
                    merged[-1][2] = (min(b0[0], b1[0]), min(b0[1], b1[1]),
                                     max(b0[2], b1[2]), max(b0[3], b1[3]))
                else:
                    b = sp.get("bbox") or (0, 0, 0, 0)
                    merged.append([sz, txt, tuple(round(v, 1) for v in b), sp_color, (b[1] if isinstance(b, (list, tuple)) else 0)])
            for sz, content, bbox, sp_color, _y0 in merged:
                # 单字符文本不在这里丢弃：水印可能是「-」「+」等单字符，
                # 统一交给分组阶段按“字号是否显著大于正文”过滤。
                if not content:
                    continue
                size = sz
                origin = None
                rot = 0.0
                for sp in spans:
                    chs = sp.get("chars") or []
                    if chs and chs[0].get("origin"):
                        origin = tuple(round(v, 1) for v in chs[0]["origin"])
                    if len(chs) >= 2 and chs[0].get("origin") and chs[1].get("origin"):
                        o0, o1 = chs[0]["origin"], chs[1]["origin"]
                        rot = round(math.degrees(math.atan2(o1[1] - o0[1], o1[0] - o0[0])), 1)
                    if origin is not None:
                        break
                page_data['texts'].append({'text': content, 'bbox': bbox,
                                           'size': size, 'origin': origin,
                                           'rot': rot, 'color': sp_color})
    return page_data


def analyze_chunk_worker(file_path, page_indices):
    """兼容旧调用签名。内部转 _scan_pages，避免每页重新 open。"""
    import pymupdf as fitz  # 子进程/惰性调用场景下确保加载
    results, errors = _scan_pages(fitz, file_path, page_indices)
    return results, errors


def rebind_hashes_to_xrefs(path, wanted_hashes, size_hint=None, log=None):
    """按内容哈希在当前文件上重新解析图片 xref。

    为什么需要：Form 拍平后会 save + 重新打开，pikepdf 会重排对象编号
    （实测某水印图 xref 59 变成 31），此时再按分析阶段记下的旧 xref 去
    find_image_objgens 会返回空集 → 图片水印的 Do 引用删除恒为 0（删不掉）。
    按内容哈希重新解析即可恢复正确映射。

    size_hint: 可选的 {(w, h), ...}，先用尺寸过滤，避免对整本扫描件哈希所有图。
    """
    found = set()
    if not wanted_hashes:
        return found
    try:
        import pymupdf as _f
    except ImportError:
        import pymupdf as _f
    try:
        d = _f.open(path)
        for pg in d:
            for img in pg.get_images(full=True):
                try:
                    if size_hint and (int(img[2]), int(img[3])) not in size_hint:
                        continue
                    if _pix_hash(_f.Pixmap(d, img[0])) in wanted_hashes:
                        found.add(int(img[0]))
                except Exception:
                    continue
        d.close()
    except Exception as e:
        if log:
            try:
                log(f">>> image xref rebind failed: {e}")
            except Exception:
                pass
    return found


def _scan_pages(fitz, file_path, page_indices, doc=None):
    """扫描一组页。**每个 worker 线程独立 fitz.open**——PyMuPDF 的 Document
    不是线程安全的，多线程共享同一 doc 会触发内部状态竞争。
    但独立 open 的成本可控：319 页/4 线程只 open 4 次（原来 319 次）。
    """
    from concurrent.futures import ThreadPoolExecutor
    indices = list(page_indices)
    total = len(indices)
    if total == 0:
        return [], []
    # 少于 4 页不值得开线程池
    nthreads = min(4, max(1, total // 4))
    if nthreads <= 1 or total <= 4:
        d = None
        try:
            d = fitz.open(file_path)
            out, errors = [], []
            for i in indices:
                try:
                    out.append(analyze_page(fitz, d, i))
                except Exception as e:
                    errors.append(f"Page {i} general error: {e!r}")
            return out, errors
        except Exception as e:
            return [], [f"File open error: {e!r}"]
        finally:
            if d: d.close()

    def _worker(chunk):
        my_results, my_errors = [], []
        d = None
        try:
            d = fitz.open(file_path)
            for i in chunk:
                try:
                    my_results.append(analyze_page(fitz, d, i))
                except Exception as e:
                    my_errors.append(f"Page {i} general error: {e!r}")
        except Exception as e:
            my_errors.append(f"Worker file open error: {e!r}")
        finally:
            if d: d.close()
        return my_results, my_errors

    # 交错切分：每个 worker 拿 [0::n], [1::n], ... 保持大致顺序、负载更均衡
    chunks = [indices[k::nthreads] for k in range(nthreads)]
    with ThreadPoolExecutor(max_workers=nthreads) as ex:
        all_res, all_err = [], []
        for chunk_res, chunk_errs in ex.map(_worker, chunks):
            all_res.extend(chunk_res)
            all_err.extend(chunk_errs)
    all_res.sort(key=lambda r: r.get('index', 0))
    return all_res, all_err

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
    def __init__(self, img_data, text_blocks, doc, lang="en", scale=1.0, parent=None,
                 adobe_info=None, annot_info=None, ocg_info=None, xobj_info=None,
                 extgs_info=None, type3_info=None, nested_info=None, uri_info=None,
                 pattern_info=None, struct_info=None, meta_info=None, outline_info=None):
        super().__init__(parent)
        self.t = TRANSLATIONS[lang]
        self.setWindowTitle(self.t["dialog_title"])
        self.doc = doc
        self.scale = scale
        self.img_boxes = {}; self.text_line_boxes = []; self.text_cards = []; self._img_frames = []
        self.adobe_info = adobe_info
        self.adobe_cb = None
        self.annot_info = annot_info
        self.annot_cb = None
        self.ocg_info = ocg_info
        self.ocg_layer_cbs = []
        self.xobj_info = xobj_info
        self.xobj_cb = None
        # P1-P3
        self.extgs_info = extgs_info; self.extgs_cb = None
        self.type3_info = type3_info; self.type3_cb = None
        self.nested_info = nested_info; self.nested_cb = None
        self.uri_info = uri_info; self.uri_cb = None
        self.pattern_info = pattern_info; self.pattern_cb = None
        self.struct_info = struct_info; self.struct_cb = None
        self.meta_info = meta_info; self.meta_cb = None
        self.outline_info = outline_info; self.outline_cb = None
        # 每个 cb 是否默认勾选（select_all/none 时尊重此标志；skip 表示不参与）
        self._cb_defaults = {}
        
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
            # 按出现次数降序；候选过多时只展示前 40 条，避免 UI 线程同步解码几百张图卡死
            _MAX_IMG_ROWS = 40
            _sorted_imgs = sorted(
                img_data.items(),
                key=lambda kv: (
                    kv[1].get('count', 0),
                    int(kv[1].get('w') or 0) * int(kv[1].get('h') or 0),
                ),
                reverse=True,
            )
            if len(_sorted_imgs) > _MAX_IMG_ROWS:
                cap_note = QLabel(
                    f"<i>showing top {_MAX_IMG_ROWS} of {len(_sorted_imgs)} by occurrence "
                    f"(watermark images are usually the most repeated)</i>"
                )
                cap_note.setWordWrap(True)
                self.scroll_layout.addWidget(cap_note)
                _sorted_imgs = _sorted_imgs[:_MAX_IMG_ROWS]
            for h, info in _sorted_imgs:
                QApplication.processEvents() # 保持 UI 响应
                frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
                l = QHBoxLayout(frame); l.setContentsMargins(5, 5, 5, 5)
                cb = QCheckBox(""); cb.setChecked(False); self.img_boxes[h] = cb
                l.addWidget(cb)
                _iw = int(info.get('w') or 0)
                _ih = int(info.get('h') or 0)
                lab = None
                try:
                    pix = fitz.Pixmap(self.doc, info['xref'])
                    if pix.n not in (1, 3, 4):
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    # 预览上限 400x400：大图直接缩放，不再跳过
                    max_preview = 400
                    if pix.width > max_preview or pix.height > max_preview:
                        zoom = min(max_preview / pix.width, max_preview / pix.height)
                        pix = fitz.Pixmap(pix, int(pix.width * zoom), int(pix.height * zoom))
                    fmt = QImage.Format.Format_RGB888 if pix.n == 3 else (
                        QImage.Format.Format_RGBA8888 if pix.n == 4 else QImage.Format.Format_Grayscale8
                    )
                    qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy()
                    lab = QLabel()
                    lab._hi = qimg.copy()  # 原始高清(供悬停放大)
                    lab.setPixmap(QPixmap.fromImage(qimg).scaled(int(150*scale), int(80*scale), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    if pix.n and pix.width:
                        _lum = sum(pix.samples[::pix.n]) / float(max(1, len(pix.samples) // pix.n))
                        if _lum >= 235:
                            lab.setStyleSheet("background:#808080;")
                except Exception as e:
                    # 预览失败也要保留可勾选条目，避免候选静默消失
                    print(f"Error loading img preview: {e}")
                    lab = QLabel("⚠ preview failed")
                if lab is None:
                    lab = QLabel("⚠ preview failed")
                lab.setProperty("loc_info", {"page": info['sample_page'], "bbox": info['sample_bbox'], "type": "img",
                                             "pages": info.get('pages', [info['sample_page']]),
                                             "xref": info.get('xref')})
                lab.installEventFilter(self)
                l.addWidget(lab); l.addStretch()
                _dim = f"{_iw}×{_ih}" if _iw and _ih else ""
                l.addWidget(QLabel(
                    f"<span style='{IMG_STAT_STYLE}'>{self.t['count']}: {info['count']}"
                    f"{(' · ' + _dim) if _dim else ''}</span>"
                ))
                self.scroll_layout.addWidget(frame)
                self._img_frames.append(frame)

        # ---- Adobe 字符级水印分组 ----
        if adobe_info and adobe_info.get('bdc_blocks', 0) > 0:
            ADOBE_STYLE = "color: #9b59b6; font-weight: bold; font-size: 9pt;"
            header_adobe = QLabel(f"<b>{self.t['adobe_header']}</b>")
            header_adobe.setStyleSheet("color: #9b59b6;")
            self.scroll_layout.addWidget(header_adobe)

            frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
            l = QHBoxLayout(frame); l.setContentsMargins(5, 5, 5, 5)
            cb = QCheckBox(self.t["adobe_hint"]); cb.setChecked(True)
            self.adobe_cb = cb
            l.addWidget(cb)
            l.addWidget(QLabel(f"<span style='{ADOBE_STYLE}'>{self.t['adobe_count']}: {adobe_info['bdc_blocks']}</span>"))
            self.scroll_layout.addWidget(frame)
            self._img_frames.append(frame)  # 让 select_all/none 也能扫到

        # ---- Annotation 注释水印分组 ----
        if annot_info and annot_info.get('total', 0) > 0:
            ANNOT_STYLE = "color: #00bcd4; font-weight: bold; font-size: 9pt;"
            header_annot = QLabel(f"<b>{self.t['annot_header']}</b>")
            header_annot.setStyleSheet("color: #00bcd4;")
            self.scroll_layout.addWidget(header_annot)

            frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
            l = QHBoxLayout(frame); l.setContentsMargins(5, 5, 5, 5)
            cb = QCheckBox(self.t["annot_hint"]); cb.setChecked(True)
            self.annot_cb = cb
            l.addWidget(cb)
            l.addWidget(QLabel(f"<span style='{ANNOT_STYLE}'>{self.t['annot_count']}: {annot_info['total']}</span>"))
            self.scroll_layout.addWidget(frame)
            self._img_frames.append(frame)

        # ---- OCG 图层水印分组 ----
        if ocg_info and ocg_info.get('total', 0) > 0:
            OCG_STYLE = "color: #26a69a; font-weight: bold; font-size: 9pt;"
            header_ocg = QLabel(f"<b>{self.t['ocg_header']}</b>")
            header_ocg.setStyleSheet("color: #26a69a;")
            self.scroll_layout.addWidget(header_ocg)

            for layer in ocg_info['layers']:
                frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
                l = QHBoxLayout(frame); l.setContentsMargins(5, 5, 5, 5)
                on_flag = " [ON]" if layer.get('is_on') else ""
                cb = QCheckBox(f"{layer['name']}{on_flag}")
                cb.setChecked(True)
                cb.setProperty('layer_name', layer['name'])
                self.ocg_layer_cbs.append(cb)
                l.addWidget(cb)
                l.addWidget(QLabel(f"<span style='{OCG_STYLE}'>{self.t['ocg_count']}: {layer['count']}</span>"))
                self.scroll_layout.addWidget(frame)
                self._img_frames.append(frame)

        # ---- Form 对象头 /Watermark 键分组 ----
        if xobj_info and xobj_info.get('total', 0) > 0:
            XOBJ_STYLE = "color: #ff7043; font-weight: bold; font-size: 9pt;"
            header_xobj = QLabel(f"<b>{self.t['xobj_header']}</b>")
            header_xobj.setStyleSheet("color: #ff7043;")
            self.scroll_layout.addWidget(header_xobj)

            frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
            l = QHBoxLayout(frame); l.setContentsMargins(5, 5, 5, 5)
            cb = QCheckBox(self.t["xobj_hint"]); cb.setChecked(True)
            self.xobj_cb = cb
            l.addWidget(cb)
            l.addWidget(QLabel(f"<span style='{XOBJ_STYLE}'>{self.t['xobj_count']}: {xobj_info['total']}</span>"))
            self.scroll_layout.addWidget(frame)
            self._img_frames.append(frame)

        # ---- 通用：单通道分组 helper ----
        def _add_channel_block(attr_name, info, header_key, hint_key, count_key, color):
            if not info or info.get('total', 0) <= 0:
                return
            style = f"color: {color}; font-weight: bold; font-size: 9pt;"
            hdr = QLabel(f"<b>{self.t[header_key]}</b>")
            hdr.setStyleSheet(f"color: {color};")
            self.scroll_layout.addWidget(hdr)
            frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
            l = QHBoxLayout(frame); l.setContentsMargins(5, 5, 5, 5)
            cb = QCheckBox(self.t[hint_key]); cb.setChecked(True)
            setattr(self, f"{attr_name}_cb", cb)
            l.addWidget(cb)
            l.addWidget(QLabel(f"<span style='{style}'>{self.t[count_key]}: {info['total']}</span>"))
            self.scroll_layout.addWidget(frame)
            self._img_frames.append(frame)

        # ---- P1 通道 ----
        _add_channel_block('extgs', extgs_info, 'extgs_header', 'extgs_hint', 'extgs_count', '#ec407a')
        _add_channel_block('type3', type3_info, 'type3_header', 'type3_hint', 'type3_count', '#ab47bc')
        _add_channel_block('nested', nested_info, 'nested_header', 'nested_hint', 'nested_count', '#5c6bc0')
        # ---- P2 通道 ----
        _add_channel_block('uri', uri_info, 'uri_header', 'uri_hint', 'uri_count', '#607d8b')
        _add_channel_block('pattern', pattern_info, 'pattern_header', 'pattern_hint', 'pattern_count', '#8d6e63')
        _add_channel_block('struct', struct_info, 'struct_header', 'struct_hint', 'struct_count', '#78909c')
        _add_channel_block('outline', outline_info, 'outline_header', 'outline_hint', 'outline_count', '#00897b')
        # ---- P3 通道（元数据） ----
        if meta_info and (meta_info.get('xmp_present') or meta_info.get('info_keys')):
            META_STYLE = "color: #757575; font-weight: bold; font-size: 9pt;"
            header_meta = QLabel(f"<b>{self.t['meta_header']}</b>")
            header_meta.setStyleSheet("color: #757575;")
            self.scroll_layout.addWidget(header_meta)
            frame = QFrame(); frame.setFrameStyle(QFrame.Shape.StyledPanel)
            l = QHBoxLayout(frame); l.setContentsMargins(5, 5, 5, 5)
            cb = QCheckBox(self.t["meta_hint"]); cb.setChecked(False)  # 元数据不默认勾选
            self.meta_cb = cb
            l.addWidget(cb)
            count = (1 if meta_info.get('xmp_present') else 0) + len(meta_info.get('info_keys', {}))
            l.addWidget(QLabel(f"<span style='{META_STYLE}'>{self.t['meta_count']}: {count}</span>"))
            self.scroll_layout.addWidget(frame)
            self._img_frames.append(frame)

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
                    img_lab = QLabel()
                    qimg = self._text_thumb(key[0], info.get("size", 10), info.get('color'), info.get('rot', 0.0))
                    if qimg is not None:
                        img_lab._hi = qimg.copy()  # 原始高清(供悬停放大)
                        img_lab.setPixmap(QPixmap.fromImage(qimg).scaled(int(220*scale), int(60*scale), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    else:
                        img_lab.setText("\u26a0 no preview")
                    # 设置 type 为 txt
                    img_lab.setProperty("loc_info", {"page": info["sample_page"], "bbox": info.get("bbox", (0,0,0,0)), "type": "txt",
                                                     "pages": info.get('pages', [info['sample_page']]), "text": key[0],
                                                     "size": info.get("size", 0), "color": info.get("color"),
                                                     "rot": info.get("rot", 0.0)})
                    img_lab.installEventFilter(self)
                    row_layout.addWidget(img_lab)
                except Exception as e:
                    print(f"Error loading text preview: {e}")
                row_layout.addStretch()
                row_layout.addWidget(QLabel(f"<span style='{TXT_STAT_STYLE}'>{self.t['count']}: {info['count']}</span>"))
                self.text_line_boxes.append({'checkbox': cb, 'content': key[0], 'bbox': info.get('bbox', (0, 0, 0, 0)), 'size': key[1],
                                             'origin': info.get('origin'), 'rot': info.get('rot', 0.0),
                                             'color': info.get('color'),
                                             'pages': info.get('pages') or [info.get('sample_page')],
                                             'origins': info.get('origins') or []})
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
        self._hover_xref = None
        self._hover_text = None
        self._hover_size = 0
        self._hover_color = None
        self._hover_rot = None
        self._zoom_pop = QLabel("")
        self._zoom_pop.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self._zoom_pop.setStyleSheet("background:#ffffff; border:1px solid #888; padding:2px;")
        self._zoom_pop.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._zoom_pop.hide()

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
                self._hover_xref = loc.get("xref")
                self._hover_text = loc.get("text")
                self._hover_size = loc.get("size") or 0
                self._hover_color = loc.get("color")
                self._hover_rot = loc.get("rot")
                self.show_location_on_page(loc["page"], loc["bbox"], loc["type"])
                self._update_hover_hint()
                self._show_zoom(source)
            return True
        # 悬停候选后，滚轮在"该候选出现的各页"间切换预览
        if et == QEvent.Type.Wheel and source is self.location_preview and len(self._hover_pages) > 1:
            delta = event.angleDelta().y()
            n = len(self._hover_pages)
            self._hover_pos = (self._hover_pos + (-1 if delta > 0 else 1)) % n
            pg = self._hover_pages[self._hover_pos]
            located = self._locate_bbox(pg)
            bboxes = self._marker_rects(located)
            self.show_location_on_page(pg, bboxes, self._hover_type)
            self._update_hover_hint()
            event.accept()
            return True
        if et == QEvent.Type.Leave:
            self._hide_zoom()
            return True
        return super().eventFilter(source, event)

    def _locate_bbox(self, page_idx):
        """返回当前候选在指定页的所有实例 bbox 列表。
        判定规则与候选一致：文本(允许同属性span拼接) + 字号 + 颜色 + 角度 全部匹配才圈。"""
        try:
            page = self.doc[page_idx]
            if self._hover_type == "img" and self._hover_xref:
                rects = page.get_image_rects(self._hover_xref)
                if rects:
                    return [(r.x0, r.y0, r.x1, r.y1) for r in rects]
            elif self._hover_type == "txt" and self._hover_text:
                want_size = self._hover_size or 0
                want_color = self._hover_color
                want_rot = self._hover_rot
                out = []
                blocks = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
                for b in blocks:
                    if b["type"] != 0:
                        continue
                    for line in b["lines"]:
                        # 同 size/color 相邻 span 拼成一行文本（与采集端一致）
                        segs = []
                        for sp in line["spans"]:
                            txt = "".join(ch.get("c", "") for ch in sp.get("chars", []))
                            if txt == "":
                                continue
                            sz = round(sp.get("size", 0) or 0, 1)
                            col = sp.get("color")
                            if segs and segs[-1][0] == sz and segs[-1][2] == col:
                                segs[-1][1] += txt
                                bb0 = segs[-1][3]
                                bb1 = sp.get("bbox") or (0, 0, 0, 0)
                                segs[-1][3] = (min(bb0[0], bb1[0]), min(bb0[1], bb1[1]),
                                               max(bb0[2], bb1[2]), max(bb0[3], bb1[3]))
                            else:
                                bb = sp.get("bbox") or (0, 0, 0, 0)
                                segs.append([sz, txt, col, tuple(bb)])
                        for sz, txt, col, bb in segs:
                            txt = txt.replace(" ", "").strip()  # 与采集端一致：去空格
                            if txt != self._hover_text.replace(" ", "").strip():
                                continue
                            if want_size and abs(sz - want_size) > max(2.0, want_size * 0.25):
                                continue  # 字号不匹配不圈
                            if want_color is not None and col != want_color:
                                continue  # 颜色不匹配不圈
                            # 角度校验：用前两字符原点算基线角
                            rot = 0.0
                            chs = [ch for sp in line["spans"] for ch in sp.get("chars", [])]
                            if len(chs) >= 2 and chs[0].get("origin") and chs[1].get("origin"):
                                o0, o1 = chs[0]["origin"], chs[1]["origin"]
                                rot = round(math.degrees(math.atan2(o1[1] - o0[1], o1[0] - o0[0])), 1)
                            if want_rot is not None and abs(rot - want_rot) > 3.0:
                                continue  # 角度不匹配不圈
                            out.append((bb[0], bb[1], bb[2], bb[3]))
                return out  # 无匹配 -> 空列表(不画圈)
        except Exception:
            pass
        return [self._hover_bbox]
    def _marker_rects(self, located_list):
        """对每个实例 bbox 生成固定尺寸圈(尺寸=样本候选尺寸)。"""
        try:
            sb = self._hover_bbox or (0, 0, 0, 0)
            w = sb[2] - sb[0]
            h = sb[3] - sb[1]
            out = []
            for located in located_list:
                cx = (located[0] + located[2]) / 2
                cy = (located[1] + located[3]) / 2
                out.append((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
            return out
        except Exception:
            return located_list
    def _update_hover_hint(self):
        if len(self._hover_pages) > 1:
            self.hover_hint.setText(
                f"🖱 滚轮切换该候选出现的页面（{self._hover_pos + 1}/{len(self._hover_pages)}）")
        else:
            self.hover_hint.setText("")

    def show_location_on_page(self, page_idx, bboxes, mark_type):
        """渲染页面并给所有匹配实例画圈。bboxes 可为单个 tuple 或列表。"""
        try:
            page = self.doc[page_idx]
            if isinstance(bboxes, tuple):
                bboxes = [bboxes]
            view_w, view_h = self.location_preview.width() - 20, self.location_preview.height() - 20
            margin = 24
            zoom = min((view_w - 2 * margin) / page.rect.width,
                       (view_h - 2 * margin) / page.rect.height)
            zoom = max(0.05, zoom)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            page_img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
            canvas = QPixmap(pix.width + 2 * margin, pix.height + 2 * margin)
            canvas.fill(Qt.GlobalColor.white)
            painter = QPainter(canvas)
            painter.drawImage(margin, margin, page_img)
            color = QColor(231, 76, 60) if mark_type == "img" else QColor(52, 152, 219)
            painter.setPen(QPen(color, 2, Qt.PenStyle.SolidLine))
            padding = 6
            for bbox in bboxes:
                target = fitz.Rect(bbox)
                x0 = target.x0 * zoom + margin
                y0 = target.y0 * zoom + margin
                x1 = target.x1 * zoom + margin
                y1 = target.y1 * zoom + margin
                er = QRect(int(x0 - padding), int(y0 - padding),
                           int((x1 - x0) + 2 * padding), int((y1 - y0) + 2 * padding))
                painter.drawEllipse(er)
            painter.end()
            pw, ph = self.location_preview.width() - 20, self.location_preview.height() - 20
            if canvas.width() > pw or canvas.height() > ph:
                canvas = canvas.scaled(pw, ph, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            self.location_preview.setPixmap(canvas)
        except Exception as e:
            self.location_preview.setText(f"Preview error: 742")
    def _crop_content(self, q, pad=8):
        """把 QImage 内容(非白)紧贴裁边, 去掉四周空白; 失败返回原图。"""
        try:
            w, h = q.width(), q.height()
            minx=miny=10**9; maxx=maxy=-1
            for y in range(0, h, 2):
                for x in range(0, w, 2):
                    c = q.pixel(x, y)
                    r=(c>>16)&255; g=(c>>8)&255; b=c&255
                    if r<240 or g<240 or b<240:
                        if x<minx:minx=x
                        if x>maxx:maxx=x
                        if y<miny:miny=y
                        if y>maxy:maxy=y
            if maxx < 0:
                return q
            x0=max(0,minx-pad); y0=max(0,miny-pad)
            x1=min(w,maxx+pad); y1=min(h,maxy+pad)
            return q.copy(x0, y0, x1-x0, y1-y0)
        except Exception:
            return q

    def _text_thumb(self, text, size, color=None, rot=0.0):
        """把候选文字单独渲染(保留旋转角度；白底/浅色水印把背景转浅灰)。
        旋转在 Qt 侧用 QPixmap.transformed 完成——自动扩展画布，不会裁剪。"""
        try:
            import pymupdf as _f
            size = max(8.0, float(size or 10))
            n = max(1, len(text))
            w = int(n * size * 1.25) + 120  # 宽度多留，左右都有空间
            h = int(size * 3.2) + 40  # 高度多留，避免 fitz 渲染时底部/顶部被页面裁掉
            ink = (0.0, 0.0, 0.0)
            gray_bg = False
            if isinstance(color, int):
                r8 = (color >> 16) & 255
                g8 = (color >> 8) & 255
                b8 = color & 255
                ink = (r8 / 255.0, g8 / 255.0, b8 / 255.0)
                lum = 0.299 * r8 + 0.587 * g8 + 0.114 * b8
                if lum >= 220:  # 偏白水印 -> 浅色字看不清，加灰底
                    gray_bg = True
            td = _f.open()
            pg = td.new_page(width=max(50, w), height=max(40, h))
            # 精确居中：水平按文本估算宽度居中，垂直按行高中点
            tw = n * size * 1.05  # 文本近似宽度
            x0 = max(20.0, (pg.rect.width - tw) / 2)
            y0 = pg.rect.height / 2 + size * 0.35
            pg.insert_text(_f.Point(x0, y0), text, fontsize=size,
                           fontname="china-s", color=ink)
            pix = pg.get_pixmap(matrix=_f.Matrix(1.5, 1.5))
            q = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
            td.close()
            rot = float(rot or 0.0)
            if abs(rot) > 0.5:
                # 手动旋转：内容居中绘制到扩展画布，保证任何角度都不被裁
                pm = QPixmap.fromImage(q)
                tr = QTransform().rotate(rot)
                br = tr.mapRect(QRectF(0, 0, pm.width(), pm.height()))
                pad = 40  # 大留白：旋转后内容绝不会触边
                out = QPixmap(int(br.width()) + 2 * pad, int(br.height()) + 2 * pad)
                out.fill(Qt.GlobalColor.white)
                p = QPainter(out)
                p.translate(out.width() / 2, out.height() / 2)
                p.rotate(rot)
                p.drawPixmap(-pm.width() // 2, -pm.height() // 2, pm)
                p.end()
                q = out.toImage()
                # 旋转后裁边：清掉旋转产生的空白；留足 padding 避免边缘字母被裁
                q = self._crop_content(q, pad=70)
            if gray_bg:
                # 快速：半透明深灰遮罩叠加，背景变灰、浅色字仍可见（一次性合成，非逐像素）
                q = q.convertToFormat(QImage.Format.Format_ARGB32)
                ov = QPixmap(q.size())
                ov.fill(Qt.GlobalColor.transparent)
                p = QPainter(ov)
                p.drawImage(0, 0, q)
                p.fillRect(ov.rect(), QColor(90, 90, 90, 110))  # 半透明灰
                p.end()
                q = ov.toImage().convertToFormat(QImage.Format.Format_RGB888)
            # 内容裁边：去掉四周空白，让水印占满；留 padding 防边缘字母被裁
            q = self._crop_content(q, pad=48)
            return q
        except Exception:
            return None

    def _show_zoom(self, source):
        """悬停小预览时，在旁边弹一个放大图（不拦截鼠标, 避开小图区域防闪烁）。
        尺寸以屏幕可用区域为上限，避免放大图超出屏幕。"""
        hi = getattr(source, '_hi', None)
        src_img = hi if (hi is not None and not hi.isNull()) else None
        pm = QPixmap.fromImage(src_img) if src_img is not None else source.pixmap()
        if pm is None or pm.isNull():
            return
        from PyQt6.QtGui import QCursor
        c = QCursor.pos()
        # 用鼠标所在屏幕(多屏正确)，availableGeometry 已排除任务栏
        scr = QApplication.screenAt(c).availableGeometry() if QApplication.screenAt(c) else \
              QApplication.primaryScreen().availableGeometry()
        max_w = int(scr.width() * 0.6)    # 最多占 60% 可用屏宽
        max_h = int(scr.height() * 0.75)  # 最多占 75% 可用屏高(留边避免贴任务栏)
        # 期望宽度 340；但不超过 max_w/max_h，也不缩小原图
        target_w = min(340, max_w)
        factor = max(1.0, target_w / max(1, pm.width()))
        # 高度超限则按高度再收
        if pm.height() * factor > max_h:
            factor = min(factor, max_h / max(1, pm.height()))
        big = pm.scaled(int(pm.width() * factor), int(pm.height() * factor),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
        # 最终兜底：仍超屏则压到屏内
        if big.width() > max_w or big.height() > max_h:
            big = big.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        self._zoom_pop.setPixmap(big)
        self._zoom_pop.adjustSize()
        # 优先放在鼠标上方; 若放不下(会盖住小图)则放下方
        x = c.x() + 16
        y = c.y() - self._zoom_pop.height() - 12
        if y < scr.top() or (c.y() - 4 <= y + self._zoom_pop.height() <= c.y() + 4):
            y = c.y() + 20  # 下方
        x = max(scr.left(), min(x, scr.right() - self._zoom_pop.width()))
        y = max(scr.top(), min(y, scr.bottom() - self._zoom_pop.height()))
        self._zoom_pop.move(x, y)
        self._zoom_pop.show()
        self._zoom_pop.raise_()
    def _hide_zoom(self):
        self._zoom_pop.hide()

    def select_all(self):
        for cb in self.img_boxes.values(): cb.setChecked(True)
        for item in self.text_line_boxes: item['checkbox'].setChecked(True)
        for attr in ['adobe_cb', 'annot_cb', 'xobj_cb', 'extgs_cb', 'type3_cb', 'nested_cb',
                     'uri_cb', 'pattern_cb', 'struct_cb', 'outline_cb', 'meta_cb']:
            obj = getattr(self, attr, None)
            if obj is not None:
                obj.setChecked(True)
        for cb in self.ocg_layer_cbs: cb.setChecked(True)
    def select_none(self):
        for cb in self.img_boxes.values(): cb.setChecked(False)
        for item in self.text_line_boxes: item['checkbox'].setChecked(False)
        for attr in ['adobe_cb', 'annot_cb', 'xobj_cb', 'extgs_cb', 'type3_cb', 'nested_cb',
                     'uri_cb', 'pattern_cb', 'struct_cb', 'outline_cb', 'meta_cb']:
            obj = getattr(self, attr, None)
            if obj is not None:
                obj.setChecked(False)
        for cb in self.ocg_layer_cbs: cb.setChecked(False)
    def filter_items(self, text):
        # 忽略所有空白；输入后连图片行一起隐藏
        q = "".join(text.lower().split())
        for f in self._img_frames:
            f.setVisible(q == "")
        for frame, content in self.text_cards:
            c = "".join(content.split())
            frame.setVisible(q in c)
    def get_selection(self):
        """返回 dict：{'imgs': [...], 'txts': [...], 'adobe': bool, 'annot': bool, 'ocg': [...], 'xobj': bool,
                       'extgs': bool, 'type3': bool, 'nested': bool, 'uri': bool, 'pattern': bool,
                       'struct': bool, 'meta': bool, 'outline': bool}
        """
        imgs = [h for h, cb in self.img_boxes.items() if cb.isChecked()]
        txts = [{'text': i['content'], 'bbox': i['bbox'], 'size': i['size'],
                 'origin': i.get('origin'), 'rot': i.get('rot', 0.0),
                 'color': i.get('color'),
                 'pages': i.get('pages') or [],
                 'origins': i.get('origins') or []}
                for i in self.text_line_boxes if i['checkbox'].isChecked()]
        return {
            'imgs': imgs,
            'txts': txts,
            'adobe': bool(self.adobe_cb and self.adobe_cb.isChecked()),
            'annot': bool(self.annot_cb and self.annot_cb.isChecked()),
            'ocg': [cb.property('layer_name') for cb in self.ocg_layer_cbs if cb.isChecked()],
            'xobj': bool(self.xobj_cb and self.xobj_cb.isChecked()),
            # P1 通道
            'extgs': bool(getattr(self, 'extgs_cb', None) and self.extgs_cb.isChecked()),
            'type3': bool(getattr(self, 'type3_cb', None) and self.type3_cb.isChecked()),
            'nested': bool(getattr(self, 'nested_cb', None) and self.nested_cb.isChecked()),
            'uri': bool(getattr(self, 'uri_cb', None) and self.uri_cb.isChecked()),
            'pattern': bool(getattr(self, 'pattern_cb', None) and self.pattern_cb.isChecked()),
            'struct': bool(getattr(self, 'struct_cb', None) and self.struct_cb.isChecked()),
            'meta': bool(getattr(self, 'meta_cb', None) and self.meta_cb.isChecked()),
            'outline': bool(getattr(self, 'outline_cb', None) and self.outline_cb.isChecked()),
        }

    def get_apply_all(self):
        return self.apply_all_cb.isChecked()

# --- 2.9 图像层水印去除对话框（P4：整页位图内的彩色广告水印） ---
class ImageWmDialog(QDialog):
    """让用户选择水印颜色和允许差异阈值，用于去除扫描 PDF 位图里的彩色水印。

    算法思路（用户提出）：正文字体一般是黑色/灰色（三通道近似相等），
    其他颜色基本就是水印颜色。因此把偏离灰度轴超过阈值的像素拉回灰度轴，
    再 inpaint 补齐残影。

    新增：跨页方差模式（水印每页相同时最准）
    """
    def __init__(self, parent=None, current_channel="sat", current_fill="inpaint", current_threshold=6, scale=1.0, current_mode="color"):
        _dbg('ImageWmDialog.__init__ ENTER (channel=%s, fill=%s, threshold=%s, scale=%s, mode=%s)', current_channel, current_fill, current_threshold, scale, current_mode)
        super().__init__(parent)
        self.scale = scale
        self.setWindowTitle("图像水印去除 - 设置参数")
        self.setModal(True)
        _dbg('ImageWmDialog setWindowTitle/setModal 完成')
        self.setMinimumWidth(int(480 * scale))
        _dbg('ImageWmDialog setMinimumWidth(%s) 完成' % (480 * scale))

        layout = QVBoxLayout(self)

        # 说明
        info = QLabel(
            "针对【整页扫描位图里的彩色水印】（如广告引流文字）。\n"
            "\n"
            "颜色阈值模式：正文字体通常是黑色或灰色（三通道近似相等），\n"
            "偏离灰度轴超过阈值的像素视为水印，去色后 inpaint 补齐。\n"
            "适合：整页是位图、水印是红色/彩色文字、正文是黑白。\n"
            "不适合：正文本身是彩色（会被去色）。\n"
            "\n"
            "跨页方差模式（推荐）：水印每页相同（位置、尺寸、颜色一致），\n"
            "但正文每页不同。跨页方差低的像素=水印，方差高的像素=正文。\n"
            "不会误删彩色正文，水印颜色任意（红/蓝/绿/灰都行）。"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # 模式选择
        layout.addWidget(QLabel("水印识别模式："))
        self.bgrp_mode = QButtonGroup(self)
        self.radio_variance = QRadioButton("跨页方差（推荐，水印每页相同时最准）")
        self.radio_color = QRadioButton("颜色阈值（单页分析，快，需彩色水印）")
        if current_mode == "variance":
            self.radio_variance.setChecked(True)
        else:
            self.radio_color.setChecked(True)
        for rb in [self.radio_variance, self.radio_color]:
            self.bgrp_mode.addButton(rb)
            layout.addWidget(rb)

        # 颜色识别方式（仅颜色阈值模式）
        self.color_section = QVBoxLayout()
        self.color_section.addWidget(QLabel("颜色识别方式："))
        self.bgrp = QButtonGroup(self)
        self.radio_r = QRadioButton("R 通道优先（红色水印，R 比 G/B 高）")
        self.radio_b = QRadioButton("B 通道优先（蓝色水印，B 比 R/G 高）")
        self.radio_g = QRadioButton("G 通道优先（绿色水印，G 比 R/B 高）")
        self.radio_sat = QRadioButton("最大通道差（任意彩色，通用，推荐）")
        # 根据 current_channel 设置默认
        _ch_map = {"r": self.radio_r, "b": self.radio_b, "g": self.radio_g, "sat": self.radio_sat}
        (_ch_map.get(current_channel) or self.radio_sat).setChecked(True)
        for rb in [self.radio_r, self.radio_b, self.radio_g, self.radio_sat]:
            self.bgrp.addButton(rb)
            self.color_section.addWidget(rb)

        # 阈值（仅颜色阈值模式）
        self.color_section.addLayout(QHBoxLayout())  # 空行
        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel("允许差异阈值（≥此值视为水印）："))
        self.threshold_spin = QSpinBox(); self.threshold_spin.setRange(2, 40); self.threshold_spin.setValue(current_threshold)
        self.threshold_slider = QSlider(Qt.Orientation.Horizontal); self.threshold_slider.setRange(2, 40)
        self.threshold_slider.setValue(current_threshold)
        self.threshold_slider.setFixedWidth(200)
        self.threshold_slider.valueChanged.connect(self.threshold_spin.setValue)
        self.threshold_spin.valueChanged.connect(self.threshold_slider.setValue)
        thr_row.addWidget(QLabel("阈值:"))
        thr_row.addWidget(self.threshold_spin)
        thr_row.addWidget(self.threshold_slider)
        thr_row.addStretch()
        self.color_section.addLayout(thr_row)
        self.threshold_hint = QLabel("提示：值越小越敏感（可能误删彩色正文）；值越大越保守（可能漏删浅水印）。推荐 6-10。")
        self.threshold_hint.setWordWrap(True)
        self.threshold_hint.setStyleSheet("color: gray; font-size: 9pt;")
        self.color_section.addWidget(self.threshold_hint)

        # 方差阈值（仅跨页方差模式）
        self.variance_section = QVBoxLayout()
        var_row = QHBoxLayout()
        var_row.addWidget(QLabel("方差阈值（≤此值视为水印）："))
        self.variance_spin = QSpinBox(); self.variance_spin.setRange(10, 2000); self.variance_spin.setValue(200)
        self.variance_slider = QSlider(Qt.Orientation.Horizontal); self.variance_slider.setRange(10, 2000)
        self.variance_slider.setValue(200)
        self.variance_slider.setFixedWidth(200)
        self.variance_slider.valueChanged.connect(self.variance_spin.setValue)
        self.variance_spin.valueChanged.connect(self.variance_slider.setValue)
        var_row.addWidget(QLabel("方差:"))
        var_row.addWidget(self.variance_spin)
        var_row.addWidget(self.variance_slider)
        var_row.addStretch()
        self.variance_section.addLayout(var_row)
        self.variance_hint = QLabel("提示：值越大越保守（只删最稳定的水印）；值越小越敏感（可能误删正文）。推荐 100-500。")
        self.variance_hint.setWordWrap(True)
        self.variance_hint.setStyleSheet("color: gray; font-size: 9pt;")
        self.variance_section.addWidget(self.variance_hint)

        # 填充方式
        layout.addWidget(QLabel("残影处理："))
        self.bgrp2 = QButtonGroup(self)
        self.radio_precise = QRadioButton("精确保字（正文清晰，只填补背景，推荐）")
        self.radio_inpaint = QRadioButton("去色 + Inpaint 填补（通用，稍慢）")
        self.radio_ns = QRadioButton("去色 + Navier-Stokes（保边缘，细节好）")
        self.radio_blur = QRadioButton("去色 + 模糊填充（快，可能有轻微残影）")
        self.radio_color_only = QRadioButton("仅去色（最快，会有浅灰残影）")
        # 根据 current_fill 设置默认
        _fill_map = {"precise": self.radio_precise, "inpaint": self.radio_inpaint,
                     "ns": self.radio_ns, "blur": self.radio_blur, "none": self.radio_color_only}
        (_fill_map.get(current_fill) or self.radio_precise).setChecked(True)
        for rb in [self.radio_precise, self.radio_inpaint, self.radio_ns, self.radio_blur, self.radio_color_only]:
            self.bgrp2.addButton(rb)
            layout.addWidget(rb)

        # 模式切换：显示/隐藏相应控件
        def on_mode_changed(mode):
            show_var = (mode == "variance")
            show_col = (mode == "color")
            # 颜色相关控件
            for i in range(self.color_section.count()):
                item = self.color_section.itemAt(i)
                w = item.widget() if item else None
                if w:
                    w.setVisible(show_col)
            # 方差阈值控件
            for i in range(self.variance_section.count()):
                item = self.variance_section.itemAt(i)
                w = item.widget() if item else None
                if w:
                    w.setVisible(show_var)
            # 重新布局
            self.layout().invalidate()

        if current_mode == "variance":
            self.radio_variance.setChecked(True)
            on_mode_changed("variance")
        else:
            self.radio_color.setChecked(True)
            on_mode_changed("color")

        self.radio_variance.toggled.connect(lambda checked: on_mode_changed("variance" if checked else "color"))
        self.radio_color.toggled.connect(lambda checked: on_mode_changed("color" if checked else "variance"))

        # 添加各 section 到主布局
        layout.addLayout(self.color_section)
        layout.addLayout(self.variance_section)

        # 按钮
        btns = QHBoxLayout()
        btns.addStretch()
        self.btn_ok = QPushButton("开始处理"); self.btn_ok.setDefault(True)
        self.btn_cancel = QPushButton("取消")
        btns.addWidget(self.btn_ok)
        btns.addWidget(self.btn_cancel)
        layout.addLayout(btns)
        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

    def get_settings(self):
        if self.radio_variance.isChecked():
            mode = "variance"
            variance_threshold = self.variance_spin.value()
        else:
            mode = "color"
            variance_threshold = 0
        if self.radio_r.isChecked():
            channel = "r"
        elif self.radio_b.isChecked():
            channel = "b"
        elif self.radio_g.isChecked():
            channel = "g"
        else:
            channel = "sat"
        if self.radio_precise.isChecked():
            fill = "precise"
        elif self.radio_inpaint.isChecked():
            fill = "inpaint"
        elif self.radio_ns.isChecked():
            fill = "ns"
        elif self.radio_blur.isChecked():
            fill = "blur"
        else:
            fill = "none"
        return {
            "mode": mode,
            "channel": channel,
            "fill": fill,
            "threshold": self.threshold_spin.value(),
            "variance_threshold": variance_threshold,
        }

# --- 3. 后台清理工作线程 ---
class MasterWorker(QThread):
    progress = pyqtSignal(int)
    log_signal = pyqtSignal(str)
    need_confirm = pyqtSignal(dict, dict)
    done = pyqtSignal(object, str)  # (fitz_doc, out_path)
    failed = pyqtSignal()

    def __init__(self, file_path, ratio_threshold=30):
        super().__init__()
        self.file_path = file_path
        self.ratio_threshold = ratio_threshold / 100.0
        self.confirmed_hashes = []; self.confirmed_texts = []
        self._confirmed_selection = {}      # dialog.get_selection() 返回的完整 dict
        self.confirmed_adobe = False       # Adobe 字符级水印
        self.confirmed_annot = False       # Annotation 注释水印
        self.confirmed_ocg_layers = []     # OCG 图层名列表
        self.confirmed_xobj = False        # Form 对象头 /Watermark 键
        # 检测结果（弹窗用）
        self.adobe_info = None
        self.annot_info = None
        self.ocg_info = None
        self.xobj_info = None
        self.is_confirmed = False
        self.stop_flag = False            # 取消标志
        self.extra_keywords = []          # 设置里的手动关键词
        self.batch_files = []             # 批量模式文件列表（空 = 单文件）
        self.apply_all_requested = False  # 确认弹窗勾选"应用到全部"
        self.apply_all_confirms = None    # 批量复用的确认项 (hashes, texts, adobe)

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
                self._confirmed_selection = {}
                self.confirmed_adobe = False
                self.adobe_info = None
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
                    self.done.emit(fitz.open(outputs[-1]), outputs[-1])
                except Exception:
                    self.done.emit(None, "")
            elif self.stop_flag or not outputs:
                self.failed.emit()
            if not outputs and not self.stop_flag:
                self.log_signal.emit(">>> No watermark found or analysis returned no candidates - aborting")
            elif self.stop_flag or not outputs:
                self.log_signal.emit(">>> Failed or stopped")
        except Exception as e:
            import traceback as _tb
            tb = _tb.format_exc()
            self.log_signal.emit(f"Error: {e}")
            for line in tb.splitlines()[1:]:
                self.log_signal.emit(f"  {line}")
            self.failed.emit()

    def _process_one(self, fpath, fi, nfiles):
        """处理单个文件：分析 → 确认 → 清理 → 复检。返回输出路径或 None。"""
        if _dw is None or fitz is None or pikepdf is None:
            _load_heavy_libs()
        self.log_signal.emit(">>> Starting analysis...")
        doc = fitz.open(fpath)
        total = len(doc)
        doc.close()  # 提前关闭，释放内存；扫描线程各自 open
        import gc; gc.collect()
        cpu_count = max(1, (os.cpu_count() or 4) - 1)
        chunk_size = max(1, total // cpu_count)
        ranges = [list(range(i, min(i + chunk_size, total))) for i in range(0, total, chunk_size)]
        self.log_signal.emit(f">>> PDF loaded: {total} pages. Thread-pool scan (4 workers, {cpu_count} cores available).")
        all_page_results = []
        # 线程池并发扫描：每 worker 独立 fitz.open 一次（避免 319 次重复 open）。
        # Windows 上 ProcessPoolExecutor + PyMuPDF 死锁，改用线程池 + 每 worker 各自 doc。
        # 进度以批为单位上报，避免 319 次 log 淹没文件。
        SCAN_BATCH = 20
        page_list = list(range(total))
        for base in range(0, total, SCAN_BATCH):
            if self.stop_flag:
                return None
            batch = page_list[base:base + SCAN_BATCH]
            try:
                res, errs = _scan_pages(fitz, fpath, batch)
                all_page_results.extend(res)
                for e in errs:
                    self.log_signal.emit(f"Worker Warning: {e}")
            except Exception as e:
                self.log_signal.emit(f">>> Batch scan error: {e!r}")
            done = min(base + SCAN_BATCH, total)
            pct = int(done / total * 80)
            self.progress.emit(pct)
            self.log_signal.emit(f">>> Scanning progress: {int(100*done/total)}%")

        self.log_signal.emit(">>> Grouping candidates (no per-xref get_image_rects)...")
        size_groups = {}
        for data in all_page_results:
            size_groups.setdefault(data['size_key'], []).append(data)

        final_img_candidates = {}; final_txt_candidates = {}
        pages_by_hash = {}
        pages_by_tk = {}
        for size_key, pages in size_groups.items():
            group_count = len(pages)
            # ratio_threshold 已在 __init__ 除过 100，这里不要再 /100
            threshold = max(2, int(group_count * self.ratio_threshold))
            # 组内主流字号（按出现次数），用于过滤正文里的单字符（. - 1 等）
            _size_hist = {}
            for p in pages:
                for _t in p['texts']:
                    try:
                        _s = round(float(_t.get('size') or 0.0), 1)
                    except Exception:
                        continue
                    _size_hist[_s] = _size_hist.get(_s, 0) + 1
            dominant_size = max(_size_hist.items(), key=lambda kv: kv[1])[0] if _size_hist else 0.0
            img_counts = {}; txt_counts = {}
            for p in pages:
                unique_hashes = set(img['hash'] for img in p['imgs'])
                for h in unique_hashes:
                    pages_by_hash.setdefault(h, []).append(p['index'])
                    img_counts[h] = img_counts.get(h, 0) + 1
                for img in p['imgs']:
                    h = img['hash']
                    if h not in final_img_candidates:
                        bbox = img.get('bbox') or (0.0, 0.0, 0.0, 0.0)
                        final_img_candidates[h] = {
                            'xref': img['xref'], 'count': 0,
                            'sample_page': p['index'],
                            'sample_bbox': tuple(bbox),
                            'w': img.get('w') or 0,
                            'h': img.get('h') or 0,
                            'xrefs': set(),
                        }
                    final_img_candidates[h]['xrefs'].add(img['xref'])
                for t in p['texts']:
                    _txt = str(t.get('text') or '')
                    if len(_txt) <= 1:
                        # 单字符候选：只有字号显著大于正文主流字号才保留
                        try:
                            _tsize = float(t.get('size') or 0.0)
                        except Exception:
                            _tsize = 0.0
                        if dominant_size and _tsize < dominant_size * 1.3:
                            continue
                    # 尺寸+颜色+角度 严格一致才算同一候选；角度归一到最近 5°(避免 0.2° 舍入拆开)
                    rot_k = round(round(t.get('rot', 0.0) / 5.0) * 5.0, 1)
                    tk = (t['text'], t['size'], t.get('color'), rot_k, size_key)
                    pages_by_tk.setdefault(tk, []).append(p['index'])
                    txt_counts[tk] = txt_counts.get(tk, 0) + 1
                    if tk not in final_txt_candidates:
                        final_txt_candidates[tk] = {'sample_page': p['index'], 'count': 0,
                                                    'bbox': t['bbox'],
                                                    'origin': t.get('origin'),
                                                    'rot': t.get('rot', 0.0), 'color': t.get('color'),
                                                    'origins': []}
                    if t.get('origin'):
                        final_txt_candidates[tk]['origins'].append(t['origin'])
            for h, count in img_counts.items():
                if count >= threshold and h in final_img_candidates:
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
        self.log_signal.emit(
            f">>> Grouping done: {len(final_img_candidates)} image, {len(final_txt_candidates)} text"
        )

        # ---- Adobe 字符级水印检测 ----
        adobe_info = None
        try:
            self.log_signal.emit(">>> Detecting Adobe character watermarks...")
            self.progress.emit(82)
            _pdf_probe = pikepdf.open(fpath)
            adobe_info = _dw.detect_adobe_watermarks(_pdf_probe)
            _pdf_probe.close()
            if adobe_info['bdc_blocks'] > 0:
                self.log_signal.emit(
                    f">>> Adobe character watermark detected: {adobe_info['bdc_blocks']} BDC/EMC blocks across {adobe_info['page_count']} pages"
                )
        except Exception as e:
            self.log_signal.emit(f">>> Adobe detection skipped: {e}")
            adobe_info = None
        self.adobe_info = adobe_info

        # ---- P0/P1/P2/P3 通道检测（复用同一个 pikepdf 句柄，减少开销）----
        self.annot_info = None
        self.ocg_info = None
        self.xobj_info = None
        self.extgs_info = None
        self.type3_info = None
        self.nested_info = None
        self.uri_info = None
        self.pattern_info = None
        self.struct_info = None
        self.meta_info = None
        self.outline_info = None
        try:
            _pdf_probe = pikepdf.open(fpath)
            self.log_signal.emit(">>> Detecting additional watermark channels...")
            self.progress.emit(85)

            def _run(label, fn):
                if self.stop_flag:
                    return None
                t0 = time.time()
                self.log_signal.emit(f">>>   {label}...")
                try:
                    r = fn(_pdf_probe)
                    self.log_signal.emit(f">>>   {label} done ({time.time()-t0:.1f}s)")
                    return r
                except Exception as e:
                    self.log_signal.emit(f">>>   {label} skipped: {e!r}")
                    return None

            # P0: Annotation
            self.annot_info = _run("Annotation", _dw.detect_annotation_watermarks) or {"total": 0, "subtypes": {}}
            if self.annot_info['total'] > 0:
                self.log_signal.emit(f">>> Annotation watermark: {self.annot_info['total']} annotations ({self.annot_info['subtypes']})")

            # P0: OCG
            self.ocg_info = _run("OCG", _dw.detect_ocg_watermarks) or {"total": 0, "layers": []}
            if self.ocg_info['total'] > 0:
                self.log_signal.emit(f">>> OCG layer watermark: {len(self.ocg_info['layers'])} layer(s), {self.ocg_info['total']} stream refs")

            # P0: Form XObject /Watermark key
            self.xobj_info = _run("Form XObject", _dw.detect_xobject_watermark_key) or {"total": 0}
            if self.xobj_info['total'] > 0:
                self.log_signal.emit(f">>> Form XObject /Watermark key: {self.xobj_info['total']} objects")

            # P1: ExtGState alpha
            self.extgs_info = _run("ExtGState", _dw.detect_extgstate_alpha_watermarks) or {"total": 0}
            if self.extgs_info['total'] > 0:
                self.log_signal.emit(f">>> ExtGState alpha: {self.extgs_info['total']} low-alpha gstate entries")

            # P1: Type3 字体
            self.type3_info = _run("Type3", _dw.detect_type3_fonts) or {"total": 0}
            if self.type3_info['total'] > 0:
                self.log_signal.emit(f">>> Type3 fonts: {self.type3_info['total']} fonts")

            # P1: 嵌套 Form
            self.nested_info = _run("Nested Form", _dw.detect_nested_form_xobjects) or {"total": 0}
            if self.nested_info['total'] > 0:
                self.log_signal.emit(f">>> Nested Form XObject: {self.nested_info['total']} nested refs")

            # P2: URI 链接注释
            self.uri_info = _run("URI", _dw.detect_uri_link_annotations) or {"total": 0}
            if self.uri_info['total'] > 0:
                self.log_signal.emit(f">>> URI link annotations: {self.uri_info['total']} links")

            # P2: Pattern/Shading
            self.pattern_info = _run("Pattern/Shading", _dw.detect_pattern_shading) or {"total": 0, "patterns": 0, "shadings": 0}
            if self.pattern_info['total'] > 0:
                self.log_signal.emit(f">>> Pattern/Shading: {self.pattern_info['patterns']} patterns + {self.pattern_info['shadings']} shadings")

            # P2: 结构 Artifact
            self.struct_info = _run("Structure", _dw.detect_structure_artifacts) or {"total": 0}
            if self.struct_info['total'] > 0:
                self.log_signal.emit(f">>> Structure Artifact nodes: {self.struct_info['total']}")

            # P2: 轮廓描边水印
            self.outline_info = _run("Outline stroke", _dw.detect_outline_stroke_watermarks) or {"total": 0}
            if self.outline_info['total'] > 0:
                self.log_signal.emit(f">>> Outline stroke watermarks: {self.outline_info['total']} BT..ET blocks")

            # P3: 元数据
            self.meta_info = _run("Metadata", _dw.detect_metadata_watermark) or {"xmp_present": False, "info_keys": {}}
            meta_cnt = (1 if self.meta_info.get('xmp_present') else 0) + len(self.meta_info.get('info_keys', {}))
            if meta_cnt > 0:
                self.log_signal.emit(f">>> Metadata: {meta_cnt} fields (XMP={'yes' if self.meta_info['xmp_present'] else 'no'})")

            _pdf_probe.close()
            self.progress.emit(90)
        except Exception as e:
            import traceback
            self.log_signal.emit(f">>> P0-P3 detection error: {e!r}")
            for line in traceback.format_exc().splitlines()[1:]:
                self.log_signal.emit(f"    {line}")

        # 分析结果汇总
        self.log_signal.emit(
            f">>> Analysis done: {len(final_img_candidates)} image, "
            f"{len(final_txt_candidates)} text, "
            f"{adobe_info['bdc_blocks'] if adobe_info else 0} Adobe blocks, "
            f"{getattr(self, 'annot_info', None) and self.annot_info['total'] or 0} annotations, "
            f"{getattr(self, 'ocg_info', None) and self.ocg_info['total'] or 0} OCG refs, "
            f"{getattr(self, 'xobj_info', None) and self.xobj_info['total'] or 0} Form XObjects "
            f"(threshold={self.ratio_threshold*100:.0f}%)"
        )

        # 所有通道都为空：无水印可清理
        no_img = not final_img_candidates
        no_txt = not final_txt_candidates
        no_adobe = (adobe_info is None) or adobe_info['bdc_blocks'] == 0
        no_annot = (self.annot_info is None) or self.annot_info['total'] == 0
        no_ocg = (self.ocg_info is None) or self.ocg_info['total'] == 0
        no_xobj = (self.xobj_info is None) or self.xobj_info['total'] == 0
        no_extgs = (self.extgs_info is None) or self.extgs_info['total'] == 0
        no_type3 = (self.type3_info is None) or self.type3_info['total'] == 0
        no_nested = (self.nested_info is None) or self.nested_info['total'] == 0
        no_uri = (self.uri_info is None) or self.uri_info['total'] == 0
        no_pattern = (self.pattern_info is None) or self.pattern_info['total'] == 0
        no_struct = (self.struct_info is None) or self.struct_info['total'] == 0
        no_meta = (self.meta_info is None) or not (self.meta_info.get('xmp_present') or self.meta_info.get('info_keys'))
        no_outline = (self.outline_info is None) or self.outline_info['total'] == 0
        if all([no_img, no_txt, no_adobe, no_annot, no_ocg, no_xobj,
                no_extgs, no_type3, no_nested, no_uri, no_pattern, no_struct, no_meta, no_outline]):
            self.log_signal.emit(
                ">>> No watermark candidates found. Possible reasons:"
            )
            self.log_signal.emit(
                "  - PDF has no text layer and images are not repeated across pages"
            )
            self.log_signal.emit(
                "  - Watermark ratio threshold too high (lower it in Settings)"
            )
            self.log_signal.emit(
                "  - Watermark is burned into scan images (needs image repair tools)"
            )
            return None

        # 确认环节：批量且已有复用确认项时跳过弹窗
        if self.apply_all_confirms is not None:
            confirmed = dict(self.apply_all_confirms)
            self.log_signal.emit(">>> Applying previous selections to this file...")
        else:
            self.log_signal.emit(">>> Waiting for user confirmation...")
            self.need_confirm.emit(final_img_candidates, final_txt_candidates)
            while not self.is_confirmed and not self.stop_flag:
                self.msleep(50)
            if self.stop_flag:
                doc.close()
                return None
            # 从 worker 属性读回用户勾选（ask_user 已把 dict 写回 worker）
            confirmed = dict(self._confirmed_selection)
            if self.apply_all_requested and nfiles > 1:
                self.apply_all_confirms = dict(confirmed)

        ic = confirmed.get('imgs', [])
        tc = confirmed.get('txts', [])
        adobe_conf = bool(confirmed.get('adobe', False))
        annot_conf = bool(confirmed.get('annot', False))
        ocg_conf_layers = list(confirmed.get('ocg', []))
        xobj_conf = bool(confirmed.get('xobj', False))
        extgs_conf = bool(confirmed.get('extgs', False))
        type3_conf = bool(confirmed.get('type3', False))
        nested_conf = bool(confirmed.get('nested', False))
        uri_conf = bool(confirmed.get('uri', False))
        pattern_conf = bool(confirmed.get('pattern', False))
        struct_conf = bool(confirmed.get('struct', False))
        meta_conf = bool(confirmed.get('meta', False))
        outline_conf = bool(confirmed.get('outline', False))

        self.log_signal.emit(">>> Applying cleaning process...")
        keywords = [c['text'].encode('utf-8') for c in tc] + \
                   [k.encode('utf-8') for k in self.extra_keywords]
        # 用分析阶段记下的 xref，禁止再算一遍哈希（大图分析用 samples[::4]，
        # 旧删除路径用完整 samples，哈希对不上 → 勾了却删不掉）
        ic_set = set(ic)
        # 勾选图片候选的尺寸集合：用于哈希重定位与残留校验时先按尺寸过滤，避免全量哈希
        _size_hint = set()
        for _h, _info in final_img_candidates.items():
            if _h in ic_set:
                try:
                    _size_hint.add((int(_info.get('w') or 0), int(_info.get('h') or 0)))
                except Exception:
                    pass
        confirmed_xrefs = set()
        for h, info in final_img_candidates.items():
            if h in ic_set:
                xs = info.get('xrefs') or ([info['xref']] if info.get('xref') else [])
                confirmed_xrefs.update(xs)
        self.log_signal.emit(
            f">>> Image delete: {len(ic_set)} hashes → {len(confirmed_xrefs)} xrefs"
        )
        self.progress.emit(30)

        try:
            pdf = pikepdf.open(fpath)
        except Exception:
            # 加密等无法直接打开时，退回 fitz 转存后再处理（尽力而为）
            tmp_imgs = os.path.join(tempfile.gettempdir(),
                                    f"__wm_imgs_{uuid.uuid4().hex}_{os.path.basename(fpath)}")
            doc.save(tmp_imgs, garbage=4, deflate=True)
            try:
                doc.close()
            except Exception:
                pass
            pdf = pikepdf.open(tmp_imgs)
        else:
            try:
                doc.close()
            except Exception:
                pass

        # Form XObject 拍平：不依赖页面上的 /Do，直接把页面级 Form 并入页面主内容流。
        # 这样原先藏在 /Resources/XObject 里的文本/路径/图片都能被后续文本和几何删除看见。
        if tc or confirmed_xrefs:
            try:
                from form_flatten import flatten_all_page_forms
                flatten_n = flatten_all_page_forms(pdf)
                if flatten_n:
                    flatten_tmp = os.path.join(
                        tempfile.gettempdir(),
                        f"__wm_flat_{uuid.uuid4().hex}_{os.path.basename(fpath)}",
                    )
                    pdf.save(flatten_tmp)
                    pdf.close()
                    pdf = pikepdf.open(flatten_tmp)
                    fpath = flatten_tmp
                    self.log_signal.emit(f">>> Form XObject flattened: {flatten_n} page-level Form(s)")
            except Exception as e:
                self.log_signal.emit(f">>> Form XObject flatten failed: {e}")

        removed_total = 0
        # 文本水印删除：**只删内容流里的文本绘制操作符（Tj/TJ）**，CID 感知。
        # 明文串与十六进制 CID 都会按字体 ToUnicode 解码后与候选文本比对，
        # 命中才删除该操作符，其余内容（正文）一字节不动。
        # 绝不使用 add_redact_annot / apply_redactions：旋转文本的轴对齐 bbox
        # 会覆盖整页（实测 43%），红化会把框内正文一起删除。正文必须 100% 保留。
        if tc:
            try:
                self.log_signal.emit(">>> Text watermark delete via content-stream text operators...")
                self.progress.emit(30)
                try:
                    import text_stream_remover as _tsr
                except Exception as _imp_err:
                    _tsr = None
                    self.log_signal.emit(f">>> text_stream_remover unavailable: {_imp_err}")
                if _tsr is not None:
                    # 保险丝：删除前后逐页统计正文字符数，实际损失远超预期即回滚
                    def _page_char_counts(p):
                        counts = []
                        try:
                            _d = fitz.open(p)
                            for _pg in _d:
                                counts.append(len(_pg.get_text()))
                            _d.close()
                        except Exception:
                            counts = []
                        return counts

                    pre_counts = _page_char_counts(fpath)
                    removed_ops, hits, unmatched, removed_chars = _tsr.remove_candidate_text(
                        pdf, tc, log=self.log_signal.emit
                    )
                    removed_total = removed_ops
                    self.log_signal.emit(
                        f">>> Text watermark operators removed: {removed_ops} "
                        f"(expected ~{removed_chars} chars; "
                        f"{len(tc) - len(unmatched)}/{len(tc)} candidates matched)"
                    )
                    for u in (unmatched or [])[:10]:
                        self.log_signal.emit(
                            f">>> WARNING candidate not found in content stream, left untouched: {str(u)[:70]}"
                        )
                    # 落盘，后续步骤基于新工作副本
                    base_tmp = os.path.join(
                        tempfile.gettempdir(),
                        f"__wm_txtstream_{uuid.uuid4().hex}_{os.path.basename(fpath)}",
                    )
                    pdf.save(base_tmp)
                    pdf.close()

                    # 保险丝校验
                    post_counts = _page_char_counts(base_tmp)
                    actual_loss = 0
                    if pre_counts and len(pre_counts) == len(post_counts):
                        actual_loss = sum(max(0, a - b) for a, b in zip(pre_counts, post_counts))
                    allowed = removed_chars * 2 + 200
                    if post_counts and actual_loss > allowed:
                        self.log_signal.emit(
                            f">>> ERROR text loss {actual_loss} chars exceeds expected "
                            f"{removed_chars} (limit {allowed}) — REVERTED to keep original text"
                        )
                        try:
                            os.remove(base_tmp)
                        except Exception:
                            pass
                        removed_total = 0
                    else:
                        fpath = base_tmp
                    pdf = pikepdf.open(fpath)
                self.progress.emit(60)
            except Exception as e:
                self.log_signal.emit(f">>> Text watermark delete failed: {e}")
                try:
                    pdf = pikepdf.open(fpath)
                except Exception:
                    pass
        else:
            self.log_signal.emit(">>> Text watermark skip: no confirmed text candidates.")
            self.progress.emit(60)

        geo_removed = 0
        geo_targets = []
        # 保持变量以兼容后续日志与统计

        # Form 兜底：用户勾选即删
        form_removed = 0
        for c in tc:
            if not c.get('text'):
                continue
            try:
                form_removed += _dw.find_and_remove_form(pdf, fpath, c['text'])
            except Exception:
                pass

        img_removed = 0
        if ic_set:
            # 拍平/保存后对象编号会变（实测 59 → 31），旧 xref 失效会让删除恒为 0。
            # 按内容哈希在当前工作副本上重新解析 xref。
            _reb = rebind_hashes_to_xrefs(fpath, ic_set, _size_hint, log=self.log_signal.emit)
            if _reb and _reb != confirmed_xrefs:
                self.log_signal.emit(
                    f">>> Image xrefs rebound by hash: {sorted(confirmed_xrefs)[:6]} -> {sorted(_reb)[:6]}"
                )
                confirmed_xrefs = _reb
            if confirmed_xrefs:
                img_cand = _dw.find_image_objgens(pdf, confirmed_xrefs)
                img_removed = _dw.remove_image_watermarks(pdf, img_cand)
                if img_removed == 0 and img_cand:
                    # 勾了但一条 Do 都没删：给出可执行的解释（而不是静默 0）
                    _sm = {}
                    try:
                        if hasattr(_dw, 'find_smask_parents'):
                            _sm = _dw.find_smask_parents(pdf, img_cand)
                    except Exception:
                        _sm = {}
                    if _sm:
                        self.log_signal.emit(
                            ">>> WARNING 勾选的图片是其它图片的 /SMask（软掩码），它不被 /Do 绘制，"
                            "无法按删除引用的方式移除。请改选对应的彩色图，或用『图像水印』按 DPI 删除。"
                        )
                    else:
                        self.log_signal.emit(
                            ">>> WARNING 勾选的图片在页面/Form 内容流里没有绘制引用"
                            "（可能是内联图像或仅作掩码），本次未删除。"
                        )

        # ---- Adobe 字符级水印删除（用户勾选时）----
        adobe_bdc_removed = 0
        adobe_xobj_removed = 0
        if adobe_conf and adobe_info and adobe_info['bdc_blocks'] > 0:
            try:
                res = _dw.remove_adobe_watermarks(pdf)
                adobe_bdc_removed = res['bdc_removed']
                adobe_xobj_removed = res['xobj_removed']
                self.log_signal.emit(
                    f">>> Adobe watermark removed: {adobe_bdc_removed} BDC blocks, {adobe_xobj_removed} XObject refs"
                )
            except Exception as e:
                self.log_signal.emit(f">>> Adobe removal error: {e}")

        # ---- P0: Annotation 注释水印删除 ----
        annot_removed = 0
        if annot_conf and self.annot_info and self.annot_info['total'] > 0:
            try:
                annot_removed = _dw.remove_annotation_watermarks(pdf)
                self.log_signal.emit(f">>> Annotation watermark removed: {annot_removed} annotations")
            except Exception as e:
                self.log_signal.emit(f">>> Annotation removal error: {e}")

        # ---- P0: OCG 图层水印删除 ----
        ocg_bdc_removed = 0
        ocg_layers_removed = 0
        if ocg_conf_layers and self.ocg_info:
            try:
                res = _dw.remove_ocg_watermarks(pdf, layer_names=ocg_conf_layers)
                ocg_bdc_removed = res['bdc_removed']
                ocg_layers_removed = res['layers_removed']
                self.log_signal.emit(
                    f">>> OCG layer watermark removed: {ocg_bdc_removed} BDC blocks, {ocg_layers_removed} layers"
                )
            except Exception as e:
                self.log_signal.emit(f">>> OCG removal error: {e}")

        # ---- P0: Form XObject /Watermark 键删除 ----
        xobj_removed = 0
        if xobj_conf and self.xobj_info and self.xobj_info['total'] > 0:
            try:
                xobj_removed = _dw.remove_xobject_watermark_key(pdf)
                self.log_signal.emit(f">>> Form XObject /Watermark key removed: {xobj_removed} objects")
            except Exception as e:
                self.log_signal.emit(f">>> XObject removal error: {e}")

        # ---- P1: ExtGState alpha 删除 ----
        extgs_removed = 0
        if extgs_conf and self.extgs_info and self.extgs_info['total'] > 0:
            try:
                extgs_removed = _dw.remove_extgstate_alpha_watermarks(pdf, self.extgs_info['gstate_names'])
                self.log_signal.emit(f">>> ExtGState alpha removed: {extgs_removed} gstate refs")
            except Exception as e:
                self.log_signal.emit(f">>> ExtGState removal error: {e}")

        # ---- P1: Type3 字体（暂只报告，删除会破坏文本渲染，需人工确认）----
        if type3_conf and self.type3_info and self.type3_info['total'] > 0:
            self.log_signal.emit(f">>> Type3 font removal skipped (would break text rendering, remove manually)")

        # ---- P1: 嵌套 Form（不直接删，只报告）----
        if nested_conf and self.nested_info and self.nested_info['total'] > 0:
            self.log_signal.emit(f">>> Nested Form: {self.nested_info['total']} detected (report only, no removal)")

        # ---- P2: URI 链接注释删除 ----
        uri_removed = 0
        if uri_conf and self.uri_info and self.uri_info['total'] > 0:
            try:
                uri_removed = _dw.remove_uri_link_annotations(pdf)
                self.log_signal.emit(f">>> URI link annotations removed: {uri_removed}")
            except Exception as e:
                self.log_signal.emit(f">>> URI removal error: {e}")

        # ---- P2: Pattern/Shading（不直接删，可能影响正文）----
        if pattern_conf and self.pattern_info and self.pattern_info['total'] > 0:
            self.log_signal.emit(f">>> Pattern/Shading: {self.pattern_info['total']} detected (report only, no removal)")

        # ---- P2: 结构 Artifact（不直接删，可能影响可访问性）----
        if struct_conf and self.struct_info and self.struct_info['total'] > 0:
            self.log_signal.emit(f">>> Structure Artifact: {self.struct_info['total']} detected (report only, no removal)")

        # ---- P2: 轮廓描边（不直接删，可能误删正文）----
        if outline_conf and self.outline_info and self.outline_info['total'] > 0:
            self.log_signal.emit(f">>> Outline stroke: {self.outline_info['total']} detected (report only, no removal)")

        # ---- P3: 元数据删除 ----
        meta_removed = 0
        if meta_conf and self.meta_info and (self.meta_info.get('xmp_present') or self.meta_info.get('info_keys')):
            try:
                meta_removed = _dw.remove_metadata_watermark(pdf)
                self.log_signal.emit(f">>> Metadata removed: {meta_removed} fields")
            except Exception as e:
                self.log_signal.emit(f">>> Metadata removal error: {e}")

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
            # 1 字符关键词（如 '-'）会命中正文里普通连字符 → 复检时跳过，避免假报残留
            _resid_kw = [k for k in keywords if len(k) >= 2]
            for pg in chk:
                t = pg.get_text()
                if _resid_kw and any(k.decode('utf-8', 'replace').lower() in t.lower() for k in _resid_kw):
                    resid += 1
            # 残留校验按内容哈希：保存会重排对象编号，按 xref 编号比对会产生假残留
            left_imgs = []
            if ic_set:
                for pg in chk:
                    for g in pg.get_images(full=True):
                        try:
                            if _size_hint and (int(g[2]), int(g[3])) not in _size_hint:
                                continue
                            if _pix_hash(fitz.Pixmap(chk, g[0])) in ic_set:
                                left_imgs.append(g[0])
                        except Exception:
                            continue
            chk.close()
        except Exception:
            pass
        self.log_signal.emit(
            f">>> 文本水印块删除: {removed_total} 个(关键词)；几何删除: {geo_removed} 个；"
            f"图片水印 Do 删除: {img_removed} 个；"
            f"Adobe 字符级: {adobe_bdc_removed} 块；"
            f"Annotation: {annot_removed} 个；OCG: {ocg_bdc_removed} 块/{ocg_layers_removed} 层；"
            f"Form XObj: {xobj_removed} 个；ExtGState: {extgs_removed} 个；"
            f"URI: {uri_removed} 个；元数据: {meta_removed} 项；"
            f"权限限制已移除"
        )
        if resid == 0 and not left_imgs:
            self.log_signal.emit(">>> Verify passed: no residual")
        else:
            self.log_signal.emit(f">>> Verify warning: text residual {resid} pages, image residual {len(left_imgs)}")
        return out_path

# --- 3.9 图像层水印去除工作线程（P4：整页位图里的彩色广告水印） ---
class ImageWmWorker(QThread):
    """P4 图像层水印去除工作线程。

    针对整页扫描位图内的彩色水印（如广告引流文字），水印和正文混合在同一张 RGB 位图里。
    原理：正文字符通常是黑色/灰色（三通道近似相等），偏离灰度轴超过阈值的像素视为水印。

    两步算法（SKILL 验证配方）：
      Step 1: 去色 - mask 内三通道拉平为平均灰度
      Step 2: inpaint - 用 TELEA 填补残影（避免只去色留灰色残影）

    关键：用 pikepdf 逐 xref 换图，保留 SMask / 内容流 / 页面对象完整。
    不用 fitz 渲染整页（会丢透明层）。
    """
    progress = pyqtSignal(int)
    log_signal = pyqtSignal(str)
    done = pyqtSignal(str, str)  # (out_path, status)
    failed = pyqtSignal(str)

    def __init__(self, file_path, settings, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.settings = settings
        self.stop_flag = False

    def run(self):
        import gc, time
        # cv2/numpy 必须在 try 内导入：缺包时抛出的异常若穿过 QThread.run()
        # 会让 PyQt 触发 qFatal → 进程直接 abort（窗口版看不到任何报错）。
        try:
            import cv2
            import numpy as np
        except Exception as _imp_err:
            self.log_signal.emit(
                f">>> 色彩水印需要 opencv-python 与 numpy，当前环境缺少：{_imp_err}"
            )
            self.failed.emit(
                "缺少 opencv-python-headless / numpy，色彩水印不可用。\n"
                "（源码运行：pip install opencv-python-headless numpy；"
                "或使用包含这两个库的完整打包版本）"
            )
            return
        try:
            ch = self.settings['channel']
            fill = self.settings['fill']
            th = int(self.settings['threshold'])
            self.log_signal.emit(f">>> P4 图像水印去除: {os.path.basename(self.file_path)}")
            self.log_signal.emit(f">>>   channel={ch}, fill={fill}, threshold={th}")

            doc = pikepdf.open(self.file_path)
            # 用 fitz 读像素（fitz.Pixmap(doc, xref) 最简洁）
            fitz_doc = fitz.open(self.file_path)
            n_pages = len(doc.pages)
            proc = 0
            skip = 0
            
            # 跨页方差模式：收集所有页面的灰度图，用于计算方差
            # 水印每页相同 → 方差≈0；正文每页不同 → 方差大
            page_gray_list = []
            page_info_list = []  # 保存每页的 (pidx, name, val, w, h, xref, img)
            
            self.log_signal.emit(f">>> 第一轮：收集所有页面图像...")
            
            for pidx in range(n_pages):
                if self.stop_flag:
                    doc.close(); fitz_doc.close()
                    self.failed.emit("cancelled")
                    return
                self.progress.emit(int(pidx * 50 / n_pages))  # 前50%是收集
                page = doc.pages[pidx]
                try:
                    res = page.get('/Resources', None)
                    xobj = res.get('/XObject', None) if res is not None else None
                    if xobj is None:
                        continue
                except Exception:
                    continue

                for name, val in xobj.items():
                    try:
                        sub = val.get('/Subtype', None)
                        if sub is None or str(sub) != '/Image':
                            continue
                        w = int(val.get('/Width', 0))
                        h = int(val.get('/Height', 0))
                        if w * h < 500:
                            continue
                        cs = val.get('/ColorSpace', None)
                        cs_str = str(cs) if cs is not None else ''
                        # 只处理 DeviceRGB（其他颜色空间不识别）
                        if 'DeviceRGB' not in cs_str:
                            continue
                    except Exception:
                        continue

                    # 定位 fitz xref（pikepdf objgen 可能是 tuple 或 object）
                    og = val.objgen
                    xref = og[0] if isinstance(og, tuple) else og.obj
                    try:
                        pix = fitz.Pixmap(fitz_doc, xref)
                    except Exception:
                        continue

                    # 读原始 RGB 字节
                    try:
                        if pix.n == 3 and not pix.alpha:
                            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, 3).copy()
                        elif pix.n == 1:
                            # 灰度图无彩色水印
                            continue
                        else:
                            continue
                    except Exception:
                        continue

                    # 计算灰度图（用于跨页方差）
                    gray = ((img[..., 0].astype(np.int16) + img[..., 1].astype(np.int16) + img[..., 2].astype(np.int16)) / 3).astype(np.uint8)
                    page_gray_list.append(gray)
                    page_info_list.append((pidx, name, val, w, h, xref, img))
            
            self.log_signal.emit(f">>> 收集完成：{len(page_gray_list)} 张图像")
            
            # 计算跨页方差（如果有多页）
            variance_map = None
            if len(page_gray_list) >= 2:
                self.log_signal.emit(f">>> 计算跨页方差...")
                try:
                    # 检查所有图像尺寸是否一致
                    ref_h, ref_w = page_gray_list[0].shape[:2]
                    all_same_size = all(img.shape[0] == ref_h and img.shape[1] == ref_w for img in page_gray_list)
                    
                    if all_same_size:
                        # 堆叠所有页面的灰度图，计算每个位置的方差
                        stacked = np.stack(page_gray_list, axis=0)  # (n_pages, h, w)
                        variance_map = np.var(stacked, axis=0).astype(np.float32)  # (h, w)
                        self.log_signal.emit(f">>> 方差范围: {variance_map.min():.1f} - {variance_map.max():.1f}")
                        del stacked  # 释放内存
                    else:
                        # 尺寸不一致，无法计算跨页方差
                        sizes = set((img.shape[0], img.shape[1]) for img in page_gray_list)
                        self.log_signal.emit(f">>> 图像尺寸不一致 {sizes}，无法计算跨页方差")
                except Exception as e:
                    self.log_signal.emit(f">>> 方差计算失败: {e}，降级到单页模式")
            elif len(page_gray_list) == 1:
                self.log_signal.emit(f">>> 只有1页，无法计算跨页方差，使用单页模式")
            
            # 第二轮：处理每页图像
            self.log_signal.emit(f">>> 第二轮：处理图像...")
            for i, (pidx, name, val, w, h, xref, img) in enumerate(page_info_list):
                if self.stop_flag:
                    doc.close(); fitz_doc.close()
                    self.failed.emit("cancelled")
                    return
                self.progress.emit(int(50 + i * 50 / len(page_info_list)))
                
                # 构造 mask（按用户选择的通道）
                r = img[..., 0].astype(np.int16)
                g = img[..., 1].astype(np.int16)
                b = img[..., 2].astype(np.int16)
                if ch == "r":
                    mask = ((r - g) >= th) & ((r - b) >= th)
                elif ch == "b":
                    mask = ((b - r) >= th) & ((b - g) >= th)
                elif ch == "g":
                    mask = ((g - r) >= th) & ((g - b) >= th)
                else:  # sat
                    mx = np.maximum(np.maximum(r, g), b)
                    mn = np.minimum(np.minimum(r, g), b)
                    mask = (mx - mn) >= th

                n_mask = int(mask.sum())
                ratio = n_mask / mask.size
                self.log_signal.emit(f"  Page {pidx+1} {name} ({w}x{h}): {n_mask} colored ({ratio*100:.3f}%)")

                if n_mask == 0:
                    skip += 1
                    continue
                if ratio < 0.001:  # 太少的彩色像素，跳过避免误伤
                    self.log_signal.emit(f"    -> skip (ratio < 0.1%)")
                    skip += 1
                    continue

                # 步骤 1: 去色
                mean = ((r + g + b) / 3).astype(np.uint8)
                gray_rgb = np.stack([mean, mean, mean], axis=-1)
                result = np.where(mask[..., None].astype(bool), gray_rgb, img)

                # 步骤 2: 根据 fill 模式处理残影
                if fill in ("inpaint", "ns", "precise"):
                    mask_u8 = (mask.astype(np.uint8))
                    kernel = np.ones((3, 3), np.uint8)
                    mask_d = cv2.dilate(mask_u8, kernel, iterations=1)
                    bgr = cv2.cvtColor(result, cv2.COLOR_RGB2BGR)
                    if fill == "inpaint":
                        inpainted = cv2.inpaint(bgr, mask_d, 5, cv2.INPAINT_TELEA)
                        result = cv2.cvtColor(inpainted, cv2.COLOR_BGR2RGB)
                    elif fill == "ns":
                        # Navier-Stokes：沿等照线传播，比 Telea 更保边缘，文本细节更好
                        inpainted = cv2.inpaint(bgr, mask_d, 3, cv2.INPAINT_NS)
                        result = cv2.cvtColor(inpainted, cv2.COLOR_BGR2RGB)
                    elif fill == "precise":
                        # 精确保字 — 跨页方差版：
                        #   1. 水印每页相同 → 方差≈0；正文每页不同 → 方差大
                        #   2. 用方差区分：方差小=水印背景→白色，方差大=正文→黑色
                        #   3. 不用 inpaint，只修改像素颜色
                        
                        if variance_map is not None:
                            # 有跨页方差：用方差区分
                            # 方差阈值：小于此值视为水印（每页相同），大于此值视为正文（每页不同）
                            # 经验值：水印方差通常 < 50，正文方差通常 > 100
                            variance_thresh = 50.0
                            
                            # 在水印区域内，用方差区分
                            text_mask = (variance_map > variance_thresh) & mask
                            bg_mask = mask & (~text_mask)
                            
                            self.log_signal.emit(f"    -> 方差阈值={variance_thresh}, 正文={text_mask.sum()}, 背景={bg_mask.sum()}")
                        else:
                            # 只有1页，无法计算方差，用 Otsu 降级
                            lum_orig = mean  # uint8，原图灰度
                            lum_in_mask = lum_orig[mask]
                            if len(lum_in_mask) > 10:
                                ret, _ = cv2.threshold(lum_in_mask, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                                thresh_val = float(ret)
                            else:
                                thresh_val = 128.0
                            text_mask = (lum_orig < thresh_val) & mask
                            bg_mask = mask & (~text_mask)
                            self.log_signal.emit(f"    -> 单页降级 Otsu 阈值={thresh_val:.1f}, 正文={text_mask.sum()}, 背景={bg_mask.sum()}")
                        
                        # 应用结果：正文 → 纯黑，背景 → 纯白
                        result = result.copy()
                        if int(bg_mask.sum()) > 0:
                            result[bg_mask] = np.array([255, 255, 255], dtype=np.uint8)
                        if int(text_mask.sum()) > 0:
                            result[text_mask] = np.array([0, 0, 0], dtype=np.uint8)
                elif fill == "blur":
                    blur = cv2.GaussianBlur(result, (21, 21), 0)
                    result = np.where(mask[..., None].astype(bool), blur, result)
                # fill == "none": 仅去色

                # 写回 pikepdf（关键：不压缩，让 doc.save 的 compress_streams 处理）
                # SKILL 警告：不要用 filter=FlateDecode + decode_parms={}，会导致全黑
                try:
                    val['/Filter'] = pikepdf.Name.None_
                    val['/DecodeParms'] = pikepdf.Dictionary()
                except Exception:
                    pass
                try:
                    val.write(result.tobytes(), filter=None, decode_parms=None)
                    proc += 1
                    self.log_signal.emit(f"    -> replaced ({result.tobytes().__len__()} bytes)")
                except Exception as e:
                    self.log_signal.emit(f"    -> write FAIL: {e}")

            self.log_signal.emit(f">>> 处理完成: {proc} 张图替换, {skip} 张跳过")

            # 输出路径：保存到系统临时目录（不污染用户工作目录）
            import tempfile
            base, ext = os.path.splitext(os.path.basename(self.file_path))
            out_path = os.path.join(tempfile.gettempdir(), base + "_imgwm" + ext)
            tmp_path = out_path + ".tmp"

            # 先关 fitz（只读像素用），保留 pikepdf doc 里的修改
            try: fitz_doc.close()
            except Exception: pass
            gc.collect()

            # 保存修改后的 doc 到 tmp
            self.log_signal.emit(">>> 保存中...")
            doc.save(tmp_path, compress_streams=True,
                     fix_metadata_version=False,
                     object_stream_mode=pikepdf.ObjectStreamMode.generate)
            doc.close()
            gc.collect()
            time.sleep(0.3)

            # 用 os.replace 保存到目标路径（能覆盖已存在的同名文件）
            try:
                os.replace(tmp_path, out_path)
                self.log_signal.emit(f">>> Saved: {out_path} ({os.path.getsize(out_path):,} bytes)")
                self.progress.emit(100)
                self.done.emit(out_path, "ok")
            except Exception as e:
                self.log_signal.emit(f">>> SAVE FAIL: {e}")
                self.failed.emit(f"保存失败: {e}")

        except Exception as ex:
            import traceback
            tb = traceback.format_exc()
            self.log_signal.emit(f">>> ERROR: {ex}")
            self.log_signal.emit(tb)
            self.failed.emit(f"{ex}")

# --- 3.10 按 DPI 去水印：选图后弹出匹配策略对话框 ---
class DpiMatchDialog(QDialog):
    """选图后弹出：让用户选匹配策略（DPI/尺寸）+ 删除范围（全 PDF/当前页）。"""
    def __init__(self, parent=None, target=None, hits_count=1, scale=1.0, doc=None, page_idx=0):
        _dbg('DpiMatchDialog.__init__ ENTER (target=%s, hits=%s, scale=%s)', type(target).__name__ if target else None, hits_count, scale)
        super().__init__(parent)
        self.scale = scale
        self.setWindowTitle("按 DPI 去水印 - 选择匹配策略")
        self.setModal(True)
        self.target = target or {}
        self.hits_count = hits_count
        self.setMinimumWidth(int(520 * scale))
        _dbg('DpiMatchDialog basic props 完成')

        layout = QVBoxLayout(self)

        # 选中图片信息
        info = QLabel(
            f"<b>选中图片信息</b><br>"
            f"尺寸：{self.target.get('w', '?')} × {self.target.get('h', '?')} 像素<br>"
            f"DPI：{self.target.get('dpi', '?')}<br>"
            f"渲染面积：{self.target.get('area', 0):.0f} pt²<br>"
            f"重叠图片数：{hits_count}"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)

        # 选中图片预览：解码水印元素本身的像素（不含正文）
        layout.addWidget(QLabel("<b>选中图片预览：</b>"))
        preview_label = QLabel("（预览生成中...）")
        preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_label.setMinimumSize(int(300 * scale), int(180 * scale))
        preview_label.setStyleSheet(
            "border: 2px dashed #4a9eff; background: #f4f6f8; border-radius: 5px;"
        )
        layout.addWidget(preview_label)
        try:
            if doc is not None and 'xref' in self.target:
                import pymupdf as _fitz
                from PIL import Image
                # 解码水印元素本身（XObject 像素），而非在 PDF 页面内的截图
                xref = self.target['xref']
                smask_xref = self.target.get('smask', 0) or 0
                # 解码颜色像素
                pix_color = _fitz.Pixmap(doc, xref)
                if pix_color.n not in (1, 3, 4):
                    pix_color = _fitz.Pixmap(_fitz.csRGB, pix_color)
                ow, oh = pix_color.width, pix_color.height
                # 限制解码尺寸，避免超大 XObject 占满内存
                max_px = int(700 * scale)
                if ow > max_px or oh > max_px:
                    zoom = min(max_px / ow, max_px / oh)
                    ow, oh = int(ow * zoom), int(oh * zoom)
                    pix_color = _fitz.Pixmap(pix_color, ow, oh)
                if smask_xref:
                    # 带透明通道：用 PIL 组合 RGB + alpha，再合成到白底
                    try:
                        pix_alpha = _fitz.Pixmap(doc, smask_xref)
                        if pix_alpha.width == ow and pix_alpha.height == oh:
                            col_mode = 'RGB' if pix_color.n == 3 else (
                                'RGBA' if pix_color.n == 4 else 'L'
                            )
                            color_img = Image.frombytes(col_mode, (ow, oh), pix_color.samples)
                            alpha_img = Image.frombytes('L', (ow, oh), pix_alpha.samples)
                            if color_img.mode == 'L':
                                color_img = color_img.convert('RGB')
                            if color_img.mode == 'RGB':
                                color_img = color_img.convert('RGBA')
                            r, g, b = color_img.split()[:3]
                            color_img = Image.merge('RGBA', (r, g, b, alpha_img))
                            bg = Image.new('RGBA', (ow, oh), (255, 255, 255, 255))
                            bg.alpha_composite(color_img)
                            bg_rgb = bg.convert('RGB')
                            qimg = QImage(
                                bg_rgb.tobytes('raw', 'RGB'), ow, oh, 3 * ow,
                                QImage.Format.Format_RGB888
                            ).copy()
                            preview_label.setPixmap(
                                QPixmap.fromImage(qimg).scaled(
                                    int(440 * scale), int(280 * scale),
                                    Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation
                                )
                            )
                        else:
                            # smask 尺寸不匹配，退化为纯颜色显示
                            qimg = QImage(pix_color.samples, ow, oh, pix_color.stride,
                                          QImage.Format.Format_RGB888 if pix_color.n == 3 else
                                          QImage.Format.Format_Grayscale8).copy()
                            preview_label.setPixmap(
                                QPixmap.fromImage(qimg).scaled(
                                    int(440 * scale), int(280 * scale),
                                    Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation
                                )
                            )
                    except Exception:
                        qimg = QImage(pix_color.samples, ow, oh, pix_color.stride,
                                      QImage.Format.Format_RGB888 if pix_color.n == 3 else
                                      QImage.Format.Format_Grayscale8).copy()
                        preview_label.setPixmap(
                            QPixmap.fromImage(qimg).scaled(
                                int(440 * scale), int(280 * scale),
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation
                            )
                        )
                else:
                    # 无透明通道：直接显示颜色像素
                    fmt = QImage.Format.Format_RGB888 if pix_color.n == 3 else (
                        QImage.Format.Format_RGBA8888 if pix_color.n == 4 else
                        QImage.Format.Format_Grayscale8
                    )
                    qimg = QImage(pix_color.samples, ow, oh, pix_color.stride, fmt).copy()
                    preview_label.setPixmap(
                        QPixmap.fromImage(qimg).scaled(
                            int(440 * scale), int(280 * scale),
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation
                        )
                    )
        except Exception as _e:
            preview_label.setText(f"（预览失败：{_e}）")
        _dbg('DpiMatchDialog preview 完成')

        # 匹配策略
        layout.addWidget(QLabel("<b>匹配策略：</b>"))
        self.bgrp = QButtonGroup(self)
        self.radio_dpi = QRadioButton("按 DPI 匹配（默认，匹配同 DPI 的图，如 130-170 范围）")
        self.radio_size = QRadioButton("按精确尺寸匹配（只删同尺寸图，最保守）")
        self.radio_dpi.setChecked(True)  # 默认 DPI
        for rb in [self.radio_dpi, self.radio_size]:
            self.bgrp.addButton(rb)
            layout.addWidget(rb)

        # 删除范围
        layout.addWidget(QLabel("<b>删除范围：</b>"))
        self.bgrp2 = QButtonGroup(self)
        self.radio_all = QRadioButton("全 PDF（推荐，水印通常跨多页）")
        self.radio_page = QRadioButton("只当前页（保守）")
        self.radio_all.setChecked(True)  # 默认全 PDF
        for rb in [self.radio_all, self.radio_page]:
            self.bgrp2.addButton(rb)
            layout.addWidget(rb)

        # 提示
        hint = QLabel(
            "<i>说明：DPI 匹配会误删同 DPI 的其他图片（如低分辨率正文插图）；<br>"
            "尺寸匹配最保守，但同 DPI 水印在不同页可能被切成不同尺寸（如老妖例会漏删）。<br>"
            "推荐先试 DPI 匹配，看预览效果，如误删再改尺寸匹配。</i>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 9pt;")
        layout.addWidget(hint)

        # 按钮
        btns = QHBoxLayout()
        btns.addStretch()
        self.btn_ok = QPushButton("开始匹配并删除"); self.btn_ok.setDefault(True)
        self.btn_cancel = QPushButton("取消")
        btns.addWidget(self.btn_ok)
        btns.addWidget(self.btn_cancel)
        layout.addLayout(btns)
        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

    def get_match_mode(self):
        return "dpi" if self.radio_dpi.isChecked() else "size"

    def get_scope(self):
        return "all" if self.radio_all.isChecked() else "page"


# --- 3.11 按 DPI 去水印：后台线程 ---
class DpiWmWorker(QThread):
    """按 DPI/尺寸匹配的图片 xref 列表，从内容流中删除对应 Do 引用。"""
    progress = pyqtSignal(int)
    log_signal = pyqtSignal(str)
    done = pyqtSignal(str, str)  # (out_path, status)
    failed = pyqtSignal(str)

    def __init__(self, file_path, xrefs, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.xrefs = set(xrefs)
        self.stop_flag = False

    def run(self):
        import gc, time, re
        try:
            self.log_signal.emit(f">>> 按 DPI 删除 {len(self.xrefs)} 张图片: {os.path.basename(self.file_path)}")

            pdf = pikepdf.open(self.file_path)
            self.log_signal.emit(f">>> PDF: {len(pdf.pages)} pages")

            # 1. 找所有匹配 xref 的 objgen（用 pdf_dewatermark.find_image_objgens）
            from pdf_dewatermark import find_image_objgens
            all_imgs = find_image_objgens(pdf, self.xrefs)
            self.log_signal.emit(f">>> 找到 {len(all_imgs)} 个 objgen 需要删除")

            if not all_imgs:
                pdf.close()
                self.failed.emit("未找到任何匹配的 objgen")
                return

            # 2. 逐页扫描内容流，删除匹配的 Do 引用
            deleted_count = 0
            n_pages = len(pdf.pages)
            for pidx in range(n_pages):
                if self.stop_flag:
                    pdf.close()
                    self.failed.emit("cancelled")
                    return
                if pidx % 10 == 0:
                    self.progress.emit(int(pidx * 80 / n_pages))
                page = pdf.pages[pidx]
                try:
                    contents = page.get('/Contents', None)
                except Exception:
                    continue
                if contents is None:
                    continue

                # Contents 可能是单个 Stream 或 Array of Stream
                streams = []
                if isinstance(contents, pikepdf.Array):
                    streams = list(contents)
                else:
                    streams = [contents]

                changed = False
                page_matched_count = 0
                for stream in streams:
                    try:
                        # 关键：read_bytes() 返回解压后的字节（可用 regex 匹配）
                        # read_raw_bytes() 返回原始压缩字节，正则匹配不到 Do
                        raw = stream.read_bytes().decode('latin-1', errors='replace')
                    except Exception:
                        continue
                    # 找 Do 操作：/<name> Do
                    # 需要知道 name 对应的 objgen 是否命中
                    # 解析页 Resources/XObject 获取 name → objgen 映射
                    name_to_objgen = {}
                    try:
                        res = page.get('/Resources', None)
                        if res is not None:
                            xobj = res.get('/XObject', None)
                            if xobj is not None:
                                for name, val in xobj.items():
                                    # pikepdf Name 是 str 类型（带 / 前缀），去掉开头的 /
                                    name_str = str(name).lstrip('/')
                                    try:
                                        og = val.objgen
                                        # 直接用 pikepdf 原始 objgen（tuple 或对象，和 find_image_objgens 一致）
                                        objgen_key = og
                                        name_to_objgen[name_str] = objgen_key
                                    except Exception:
                                        pass
                    except Exception:
                        pass

                    # 删除匹配的 Do
                    new_raw = raw
                    stream_matched = 0
                    stream_changed = False
                    for name_str, og_key in name_to_objgen.items():
                        if og_key in all_imgs:
                            # 匹配 /<name> Do（可能有空格）
                            pattern = re.compile(rf'/{re.escape(name_str)}\s+Do')
                            if pattern.search(new_raw):
                                new_raw = pattern.sub('', new_raw)
                                deleted_count += 1
                                stream_matched += 1
                                stream_changed = True
                                changed = True
                    if stream_changed:
                        try:
                            # 写回解压后的内容（filter=None 表示无压缩）
                            stream.write(new_raw.encode('latin-1'), filter=None)
                        except Exception as e:
                            self.log_signal.emit(f">>> Page {pidx+1} write err: {e}")
                    page_matched_count += stream_matched

                if changed:
                    self.log_signal.emit(f">>> Page {pidx+1}: {page_matched_count} xref 匹配，删除 Do 引用")

            self.log_signal.emit(f">>> 删除完成: {deleted_count} 个 Do 引用")

            # 3. 保存到临时文件
            # 输出路径：始终另存为新文件（不覆盖源文件）
            self.progress.emit(90)
            # 中间结果写系统临时目录，绝不落在源文件所在文件夹
            # （源目录只在用户点「保存」时才写入）
            _stem = os.path.splitext(os.path.basename(self.file_path))[0]
            out_path = os.path.join(tempfile.gettempdir(), f"{_stem}_dpi.pdf")
            tmp_path = out_path + ".tmp"

            try:
                pdf.save(tmp_path, linearize=True)
                pdf.close()
                gc.collect()
                time.sleep(0.3)
                if os.path.exists(tmp_path):
                    os.replace(tmp_path, out_path)
                self.log_signal.emit(f">>> Saved: {out_path} ({os.path.getsize(out_path):,} bytes)")
                self.progress.emit(100)
                self.done.emit(out_path, "ok")
            except Exception as e:
                self.log_signal.emit(f">>> SAVE FAIL: {e}")
                self.failed.emit(f"保存失败: {e}")

        except Exception as ex:
            import traceback
            tb = traceback.format_exc()
            self.log_signal.emit(f">>> ERROR: {ex}")
            self.log_signal.emit(tb)
            self.failed.emit(f"{ex}")


# --- 4. 主程序窗口 ---
class UltraAppFinal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()

        # 尽早初始化日志路径（必须在任何 add_log 之前）
        self.log_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ExtremePDFCleaner", "logs")
        self.log_file_error = None
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except Exception as e:
            self.log_file_error = f"mkdir failed: {e}"
        self.log_path = os.path.join(self.log_dir, f"app_{datetime.now().strftime('%Y%m%d')}.log")
        self.log_file_handle = None
        try:
            # 追加模式打开并保持句柄，避免每条日志都 open/close
            self.log_file_handle = open(self.log_path, 'a', encoding='utf-8')
            self.log_file_handle.write(f"\n{'='*70}\n")
            self.log_file_handle.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] App started\n")
            self.log_file_handle.write(f"Python: {sys.version.split()[0]}, Platform: {sys.platform}\n")
            self.log_file_handle.write(f"Executable: {sys.executable}\n")
            self.log_file_handle.write(f"CWD: {os.getcwd()}\n")
            self.log_file_handle.write(f"Log path: {self.log_path}\n")
            self.log_file_handle.write(f"Args: {' '.join(sys.argv[1:])}\n")
            try:
                import pymupdf as fitz
                self.log_file_handle.write(f"PyMuPDF: {fitz.__doc__}\n")
            except Exception:
                self.log_file_handle.write("PyMuPDF: not yet loaded\n")
            self.log_file_handle.write(f"{'='*70}\n")
            self.log_file_handle.flush()
        except Exception as e:
            self.log_file_error = f"open failed: {e}"

        # 首次启动按系统语言；之后按用户最后选择
        self.lang = self.config.get("lang") or detect_system_lang()
        self.doc_orig = self.doc_clean = None
        self.display_lists = {}; self.file_path = ""
        # _working_path: 当前所有去水印操作的工作基底文件路径。
        # 首次加载 = file_path；每次去水印处理后更新为输出文件路径（实现叠加）；
        # 保存后重置为 file_path（原文件已被覆盖，工作基底回到原文件）。
        self._working_path = ""
        self.ratio_threshold = self.config.get("ratio", 30)
        self.extra_keywords = list(self.config.get("keywords", []))
        self.worker = None
        self.zoom = 1.0
        self.fit_mode = "fit_page"   # fit_page / fit_width / custom
        self.batch_files = []
        self._sb_guard = False       # 滚动条联动防递归
        self._rb_start = None        # 矩形框放大起点
        self._rubber = None
        # 图像层水印去除（P4：整页位图里的红色广告水印）
        self.imgwm_worker = None
        self.imgwm_mode = "color_inpaint"  # color | color_inpaint | color_blur
        self.imgwm_threshold = 6
        # 按 DPI 去水印（P5）：选图模式 + 用户确认
        self.dpiwm_worker = None
        self._dpi_select_mode = False  # True = 左侧预览进入"选图"模式
        self._dpi_selected_xref = None  # 用户选中的图片 xref
        # 路径水印（P6）：选图模式 + 路径属性匹配删除
        self.pathwm_worker = None
        # 预览调暗高亮：原始图像（未调暗版本）
        self._preview_orig_img_orig = None
        self._preview_orig_img_clean = None

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
        self.btn_imgwm = QPushButton(); self.btn_imgwm.setEnabled(False)
        self.btn_dpiwm = QPushButton(); self.btn_dpiwm.setEnabled(False)
        self.btn_save = QPushButton(); self.btn_save.setEnabled(False)
        self.btn_save_as = QPushButton(); self.btn_save_as.setEnabled(False)
        self.btn_cancel = QPushButton(); self.btn_cancel.setEnabled(False)
        self.btn_settings = QPushButton()
        self.btn_clear_log = QPushButton("🗑 清除日志")
        self.btn_view_log = QPushButton("📁 打开日志")
        self.pbar = QProgressBar()
        self.log_output = QTextEdit(); self.log_output.setReadOnly(True)
        self.log_output.setStyleSheet("font-family: Consolas, 'Courier New', monospace; font-size: 9pt;")
        self.log_output.setMinimumHeight(int(80 * self.scale))

        # 日志文件配置（默认写到 %APPDATA%/ExtremePDFCleaner/logs/app_YYYYMMDD.log）
        # 注：log_dir/log_path 已在 __init__ 里先创建，此处仅当兜底重新计算
        if not hasattr(self, 'log_path'):
            self.log_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ExtremePDFCleaner", "logs")
            os.makedirs(self.log_dir, exist_ok=True)
            self.log_path = os.path.join(self.log_dir, f"app_{datetime.now().strftime('%Y%m%d')}.log")
            self.log_file_error = None
            try:
                self.log_file_handle = open(self.log_path, 'a', encoding='utf-8')
            except Exception as e:
                self.log_file_error = f"open failed: {e}"
                self.log_file_handle = None
        self.log_errors_only = False

        for b in [self.btn_open, self.btn_clean, self.btn_imgwm, self.btn_dpiwm, self.btn_save, self.btn_save_as, self.btn_cancel, self.btn_settings]:
            b.setFixedHeight(int(42 * self.scale)); sidebar.addWidget(b)
        sidebar.addWidget(self.log_output, 1)
        # 日志工具行
        log_tool_row = QHBoxLayout()
        log_tool_row.addWidget(self.btn_clear_log)
        log_tool_row.addWidget(self.btn_view_log)
        sidebar.addLayout(log_tool_row)
        self.btn_clear_log.clicked.connect(self.clear_log)
        self.btn_view_log.clicked.connect(self.open_log_file)
        self.pbar.setTextVisible(True)
        self.pbar.setMinimumHeight(int(22 * self.scale))
        self.pbar.setFixedHeight(int(22 * self.scale))
        sidebar.addWidget(self.pbar)

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
            l.setWordWrap(True)
            l.setMinimumSize(int(180 * self.scale), int(80 * self.scale))
            l.setMouseTracking(True)   # 让 MouseMove 在按住鼠标时也能触发（tooltip 跟随）
            s.setWidget(l)
            s.setWidgetResizable(True)  # 空状态撑满视口，占位文字居中可见
            s.setAlignment(Qt.AlignmentFlag.AlignCenter)
            s.installEventFilter(self)
            l.installEventFilter(self)   # 支持左键拖框放大
        comp.addWidget(self.scroll_orig); comp.addWidget(self.scroll_clean)
        # DPI 选图模式下的鼠标跟随提示：绝对定位在左侧预览上方，鼠标移出即隐藏
        self._dpi_hover_tip = QLabel(self)
        self._dpi_hover_tip.setText("点击水印选择图片")
        self._dpi_hover_tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._dpi_hover_tip.setStyleSheet(
            "QLabel { background: rgba(16, 24, 40, 235); color: white; "
            "border: 1px solid #4a9eff; border-radius: 6px; "
            "padding: 6px 12px; font-size: 13px; }"
        )
        self._dpi_hover_tip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._dpi_hover_tip.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._dpi_hover_tip.setFixedHeight(int(30 * self.scale))
        self._dpi_hover_tip.adjustSize()
        self._dpi_hover_tip.raise_()
        self._dpi_hover_tip.hide()
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
        self.btn_imgwm.clicked.connect(self.start_image_wm)
        self.btn_dpiwm.clicked.connect(self.start_dpi_wm)
        self.btn_save.clicked.connect(self.save_pdf_inplace)
        self.btn_save_as.clicked.connect(self.save_as_pdf)
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
        self.btn_imgwm.setText(t["img_wm"])
        self.btn_dpiwm.setText(t["dpi_wm"])
        self.btn_save.setText(t["save"])
        self.btn_save_as.setText(t["save_as"])
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
        """写日志：同时输出到 GUI 与日志文件。ERROR/WARN/STEP 自动高亮。"""
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{ts}] {text}"
        # 写入文件（优先用持久句柄；失败则尝试重开；再失败则提示）
        try:
            if getattr(self, 'log_file_handle', None) is not None:
                self.log_file_handle.write(line + "\n")
                self.log_file_handle.flush()
            else:
                fh = open(self.log_path, 'a', encoding='utf-8')
                fh.write(line + "\n"); fh.flush(); fh.close()
        except Exception as e:
            # 句柄坏了就尝试重开一次
            if getattr(self, 'log_file_handle', None) is not None:
                try:
                    self.log_file_handle.close()
                except Exception:
                    pass
                self.log_file_handle = None
            try:
                self.log_file_handle = open(self.log_path, 'a', encoding='utf-8')
                self.log_file_handle.write(line + "\n")
                self.log_file_handle.flush()
            except Exception as e2:
                # 无法写文件：在 GUI 打一次警告
                self.log_file_error = f"write failed: {e2} (path={self.log_path})"
                self.add_log_gui_only(f"LOG FILE ERROR: {self.log_file_error}")
        # 输出到 GUI，根据内容着色
        level = "INFO"
        color = ""
        low = text.lower()
        if any(k in low for k in ['error', 'traceback', 'exception', '失败', '错误']):
            level = "ERROR"
            color = "#e74c3c"
        elif any(k in low for k in ['warn', 'warning', '警告', 'warning:', 'residual']):
            level = "WARN"
            color = "#f39c12"
        elif any(k in low for k in ['step', '正在', '启动', '开始', 'complete', 'done']):
            level = "STEP"
            color = "#3498db"
        elif text.startswith('>>>'):
            level = "STEP"
            color = "#3498db"
        if color:
            self.log_output.append(f'<span style="color:#888">[{ts}]</span> <span style="color:{color};font-weight:bold">[{level}]</span> {text}')
        else:
            self.log_output.append(f'<span style="color:#888">[{ts}]</span> {text}')
        self.log_output.moveCursor(QTextCursor.MoveOperation.End)

    def add_log_gui_only(self, text):
        """仅写 GUI（用于日志系统自身报错，避免无限递归）。"""
        try:
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.log_output.append(f'<span style="color:#888">[{ts}]</span> <span style="color:#e74c3c;font-weight:bold">[LOG ERROR]</span> {text}')
            self.log_output.moveCursor(QTextCursor.MoveOperation.End)
        except Exception:
            pass

    def clear_log(self):
        self.log_output.clear()

    def open_log_file(self):
        try:
            os.startfile(self.log_dir)
        except Exception as e:
            QMessageBox.warning(self, "打开日志", f"无法打开日志目录：{e}\n日志路径：{self.log_path}")

    def log_exception(self, exc, context=""):
        """把异常堆栈追加到日志（含 traceback）。"""
        import traceback
        tb = traceback.format_exc()
        self.add_log(f"ERROR {context}: {exc!r}")
        for i, line in enumerate(tb.splitlines()):
            if i == 0:
                continue  # skip "Traceback..."
            self.add_log(f"  {line}")

    def eventFilter(self, source, event):
        et = event.type()
        # --- 选图模式：在左侧预览点击 = 选中图片（点而不是拖框） ---
        if self._dpi_select_mode and source in (self.lab_orig, self.lab_clean):
            if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                # 记录起点，release 时判断是否为"点击"
                self._dpi_click_start = event.position().toPoint()
                return True
            if et == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                if getattr(self, '_dpi_click_start', None) is not None:
                    start = self._dpi_click_start
                    end = event.position().toPoint()
                    dx = abs(end.x() - start.x())
                    dy = abs(end.y() - start.y())
                    self._dpi_click_start = None
                    # 移动 <8 像素视为"点击"，否则视为"拖动"（忽略，让用户拖框放大）
                    if dx < 8 and dy < 8:
                        self._dpi_select_at(source, start)
                    return True
        # --- 选图模式：鼠标进入预览区域显示提示、移出隐藏、移动时跟随 ---
        if self._dpi_select_mode and source in (self.lab_orig, self.lab_clean):
            if et == QEvent.Type.Enter:
                self._dpi_hover_tip.show()
                self._dpi_hover_tip.raise_()
            elif et == QEvent.Type.Leave:
                self._dpi_hover_tip.hide()
                # 离开预览时恢复调暗版
                self._restore_dimmed_preview(source)
            elif et == QEvent.Type.MouseMove:
                gp = event.globalPosition().toPoint()
                if self._dpi_hover_tip.isVisible():
                    lp = self.mapFromGlobal(gp)
                    self._dpi_hover_tip.move(lp.x() + 16, lp.y() + 18)
                    self._dpi_hover_tip.raise_()
                # 鼠标移动时高亮元素
                self._highlight_element_at(source, event.position().toPoint())
            return False
        # --- 左键拖框放大（非选图模式） ---
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

    def _dpi_select_at(self, source, pt):
        """选图模式：点击坐标 → PDF 坐标 → 找命中的图片 → 弹出确认对话框。

        pt: QLabel 内的像素坐标（左上角基准）。
        命中判定：点击点位于该图片的渲染矩形内。若有多个重叠，取面积最小的（最上层）。
        """
        doc = self.doc_orig if source is self.lab_orig else self.doc_clean
        if doc is None:
            return
        idx = self.page_spin.value() - 1
        if idx < 0 or idx >= doc.page_count:
            return
        page = doc[idx]
        lw, lh = source.width(), source.height()
        if lw <= 0 or lh <= 0:
            return
        # 像素 → PDF 坐标
        px = page.rect.width * pt.x() / lw
        py = page.rect.height * pt.y() / lh
        point = fitz.Point(px, py)

        # 遍历该页所有图片，找命中的
        hits = []
        for entry in page.get_images(full=True):
            xref = entry[0]
            w, h = entry[2], entry[3]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                continue
            for r in rects:
                if r.contains(point):
                    hits.append({
                        'xref': xref, 'w': w, 'h': h,
                        'smask': entry[1],
                        'rect': r,
                        'area': r.width * r.height,
                        'dpi': round(w * 72.0 / max(r.width, 1), 1),
                        'dpi_y': round(h * 72.0 / max(r.height, 1), 1),
                    })
        if not hits:
            # 没有图片，尝试检测路径
            self._try_select_path_at(page, point, px, py)
            return
        # 取面积最小的（最上层，最可能是水印）
        target = min(hits, key=lambda x: x['area'])
        self._dpi_selected_xref = target['xref']
        self.add_log(
            f">>> 选中图片: xref={target['xref']} {target['w']}x{target['h']} "
            f"DPI={target['dpi']} 面积={target['area']:.0f} "
            f"(共命中 {len(hits)} 张重叠图)"
        )

        # 弹确认框：让用户选匹配策略 + 删除范围
        _dbg('dpi_select_at 命中 %d 张, target xref=%s, 构造 DpiMatchDialog...', len(hits), target.get('xref'))
        dlg = DpiMatchDialog(self, target, hits_count=len(hits), scale=self.scale, doc=doc, page_idx=idx)
        _dbg('dpi_select_at DpiMatchDialog 构造完成, 调用 exec()...')
        if dlg.exec() != QDialog.DialogCode.Accepted:
            _dbg('dpi_select_at 用户取消')
            self._dpi_selected_xref = None
            self._dpi_select_mode = False
            self._dpi_hover_tip.hide()
            self.btn_dpiwm.setText(TRANSLATIONS[self.lang]["dpi_wm"])
            return
        _dbg('dpi_select_at 用户确认, get_match_mode...')
        match = dlg.get_match_mode()  # "dpi" or "size"
        scope = dlg.get_scope()       # "all" or "page"
        _dbg('dpi_select_at match=%s, scope=%s', match, scope)
        # 关闭选图模式
        self._dpi_select_mode = False
        self._dpi_hover_tip.hide()
        self.btn_dpiwm.setText(TRANSLATIONS[self.lang]["dpi_wm"])

        # 扫描全 PDF 找匹配的图片
        self.add_log(f">>> 开始扫描匹配 (mode={match}, scope={scope})...")
        candidates = self._dpi_find_matches(target, match, scope)
        if not candidates:
            self.add_log(f">>> 未找到匹配图片")
            QMessageBox.information(self, "无匹配", "未找到匹配的图片。")
            return

        self.add_log(f">>> 找到 {len(candidates)} 张匹配图片")
        # 弹最终确认
        confirm = QMessageBox.question(
            self, "确认删除",
            f"将删除 {len(candidates)} 张图片水印。\n\n"
            f"参考图: {target['w']}x{target['h']} DPI={target['dpi']}\n"
            f"匹配策略: {match}\n"
            f"范围: {scope}\n\n"
            f"是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            self.add_log(f">>> 用户取消删除")
            return

        # 启动后台删除
        self.pbar.setValue(0)
        self.btn_dpiwm.setEnabled(False)
        self.dpiwm_worker = DpiWmWorker(self._working_path, [c['xref'] for c in candidates], self)
        self.dpiwm_worker.progress.connect(self.pbar.setValue)
        self.dpiwm_worker.log_signal.connect(self.add_log)
        self.dpiwm_worker.done.connect(self._dpiwm_finished)
        self.dpiwm_worker.failed.connect(self._dpiwm_failed)
        self.dpiwm_worker.start()

    def _dpi_find_matches(self, target, match_mode, scope):
        """扫描全 PDF，找出匹配 DPI 或尺寸的图片。

        match_mode:
          - "dpi": 按 DPI 匹配（默认 ±20 允许差）
          - "size": 按精确尺寸匹配
        scope:
          - "all": 全 PDF
          - "page": 只当前页
        返回: [{'xref', 'page', 'w', 'h', 'dpi'}, ...]

        扫描当前工作状态（doc_clean 优先），确保 xref 与 _working_path 一致。
        """
        scan_doc = self.doc_clean if self.doc_clean else self.doc_orig
        if scan_doc is None:
            return []
        tw, th = target['w'], target['h']
        tdpi = target['dpi']
        results = []
        page_start = 0 if scope == "all" else (self.page_spin.value() - 1)
        page_end = scan_doc.page_count if scope == "all" else (page_start + 1)

        for pidx in range(page_start, page_end):
            page = scan_doc[pidx]
            for entry in page.get_images(full=True):
                xref, _, w, h, *_ = entry
                try:
                    rects = page.get_image_rects(xref)
                    if not rects:
                        continue
                    r = rects[0]
                    dpi = round(w * 72.0 / max(r.width, 1), 1)
                except Exception:
                    continue
                # 匹配判定
                if match_mode == "size":
                    if (w, h) != (tw, th):
                        continue
                else:  # dpi
                    if abs(dpi - tdpi) > 20:
                        continue
                results.append({
                    'xref': xref, 'page': pidx, 'w': w, 'h': h, 'dpi': dpi,
                })
        return results

    def _dpiwm_finished(self, out_path, status):
        """按 DPI 去水印完成回调。

        左侧预览保持原版（doc_orig 不动），直到用户点"保存"才更新。
        右侧预览显示处理结果（doc_clean = 处理结果）。
        叠加：_working_path 更新为 out_path，下次去水印基于此文件。
        """
        self.btn_dpiwm.setEnabled(True)
        self.add_log(f">>> 按 DPI 去水印完成: {out_path}")
        try:
            if self.doc_clean is not None:
                try: self.doc_clean.close()
                except Exception: pass
            self.doc_clean = fitz.open(out_path)
            self._working_path = out_path
            self.btn_save.setEnabled(True)
            self.btn_save_as.setEnabled(True)
            self._pending_path = out_path
            self.update_previews()
        except Exception as e:
            self.log_exception(e, "_dpiwm_finished open")
            self.add_log(f">>> 打开处理结果失败: {e}")

    def _dpiwm_failed(self, err):
        self.btn_dpiwm.setEnabled(True)
        self.add_log(f">>> 按 DPI 去水印失败: {err}")
        try:
            QMessageBox.critical(self, "处理失败", str(err))
        except Exception:
            pass

    def start_dpi_wm(self):
        """🎯 按 DPI 去水印：进入选图模式，用户在预览里点击水印图片。"""
        if not self.file_path or not os.path.isfile(self.file_path):
            QMessageBox.warning(self, "无法处理", "请先打开一个 PDF 文件")
            return
        if self.dpiwm_worker is not None and self.dpiwm_worker.isRunning():
            self.add_log(f">>> 按 DPI 去水印进行中")
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "等待中", "结构层清理还在进行")
            return
        if not self.doc_orig:
            QMessageBox.warning(self, "无法处理", "请先打开一个 PDF 文件")
            return

        # 切换选图模式（再点一次则退出）
        if self._dpi_select_mode:
            self._dpi_select_mode = False
            self._dpi_selected_xref = None
            self._dpi_hover_tip.hide()
            self.btn_dpiwm.setText(TRANSLATIONS[self.lang]["dpi_wm"])
            self.add_log(f">>> 退出选图模式")
            self.update_previews()
            return

        self._dpi_select_mode = True
        self._dpi_selected_xref = None
        self.btn_dpiwm.setText(TRANSLATIONS[self.lang]["dpi_wm_select"])
        self.update_previews()
        # tooltip 不在此处显示，等鼠标移入预览区域时由 eventFilter 自动显示
        self.add_log(f">>> 进入选图模式：请在左侧预览里【点击】一张水印图片。按 Esc 或再点本按钮可退出。")

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
                scroll.setWidgetResizable(False)
                z = compute_zoom(doc, scroll)
                pix = doc[idx].get_pixmap(matrix=fitz.Matrix(z, z))
                qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()

                if self._dpi_select_mode:
                    # 选图模式：渲染调暗30%的预览，保存原始图像用于高亮恢复
                    orig_img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
                    painter = QPainter(qimg)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(QColor(0, 0, 0, 77)))  # 30%暗度 = 77/255
                    painter.drawRect(0, 0, qimg.width(), qimg.height())
                    painter.end()
                    if lab is self.lab_orig:
                        self._preview_orig_img_orig = orig_img
                    else:
                        self._preview_orig_img_clean = orig_img
                else:
                    self._preview_orig_img_orig = None
                    self._preview_orig_img_clean = None

                lab.setPixmap(QPixmap.fromImage(qimg))
                lab.setFixedSize(pix.width, pix.height)
                if self.fit_mode != "custom":
                    self.zoom_label.setText(f"{int(z * 100)}%")
            except Exception as e:
                lab.setText(f"Render error: {e}")
        render_to_label(self.doc_orig, self.lab_orig, self.scroll_orig)
        if self.doc_clean:
            render_to_label(self.doc_clean, self.lab_clean, self.scroll_clean)
        else:
            # 右侧无处理结果时清空，避免残留上次 PDF 的图片
            self.lab_clean.clear()
            self.lab_clean.setPixmap(QPixmap())
            self.lab_clean.setFixedSize(self.scroll_clean.viewport().width(), self.scroll_clean.viewport().height())
            self.lab_clean.setText("（未处理）")

    def _update_recent(self, path):
        rec = [p for p in self.config.get("recent_files", []) if p != path]
        rec.insert(0, path)
        self.config["recent_files"] = rec[:10]
        self.config["last_dir"] = os.path.dirname(path)
        save_config(self.config)
        self.rebuild_recent_menu()

    def _highlight_element_at(self, lab, pt):
        """在选图模式下，鼠标指向的元素恢复原始亮度（不暗）。"""
        if not self._dpi_select_mode:
            return
        doc = self.doc_orig if lab is self.lab_orig else self.doc_clean
        if doc is None:
            return

        # 获取原始图像（未调暗的版本）
        orig_img = getattr(self, '_preview_orig_img_orig' if lab is self.lab_orig else '_preview_orig_img_clean', None)
        if orig_img is None or orig_img.isNull():
            return

        idx = self.page_spin.value() - 1
        if idx < 0 or idx >= doc.page_count:
            return

        page = doc[idx]
        lw, lh = lab.width(), lab.height()
        if lw <= 0 or lh <= 0:
            return

        # 像素 → PDF 坐标
        px = page.rect.width * pt.x() / lw
        py = page.rect.height * pt.y() / lh
        point = fitz.Point(px, py)

        # 查找鼠标指向的元素
        highlight_rect = None

        # 先检查图片
        for entry in page.get_images(full=True):
            xref = entry[0]
            try:
                rects = page.get_image_rects(xref)
                for r in rects:
                    if r.contains(point):
                        highlight_rect = QRect(
                            int(r.x0 / page.rect.width * lw),
                            int(r.y0 / page.rect.height * lh),
                            int(r.width / page.rect.width * lw),
                            int(r.height / page.rect.height * lh)
                        )
                        break
            except Exception:
                continue
            if highlight_rect:
                break

        # 如果没有图片，检查路径
        if not highlight_rect:
            drawings = page.get_drawings(extended=True)
            for d in drawings:
                if d.get('rect') and d['rect'].contains(point):
                    highlight_rect = QRect(
                        int(d['rect'].x0 / page.rect.width * lw),
                        int(d['rect'].y0 / page.rect.height * lh),
                        int(d['rect'].width / page.rect.width * lw),
                        int(d['rect'].height / page.rect.height * lh)
                    )
                    break

        # 恢复原始图像 + 绘制高亮边框
        if highlight_rect and not orig_img.isNull():
            # 创建当前图像副本（调暗版）
            cur_img = lab.pixmap().toImage().copy()
            # 恢复元素区域为原始亮度。
            # 注意：QImage.copy(img, rect) 在 PyQt6 里不存在（只有 copy()/copy(rect)/copy(x,y,w,h)），
            # 必须用 QPainter.drawImage(目标矩形, 源图, 源矩形) 把原图区域画回去。
            _p = QPainter(cur_img)
            _p.drawImage(highlight_rect, orig_img, highlight_rect)
            _p.end()

            # 绘制高亮边框
            painter = QPainter(cur_img)
            painter.setPen(QPen(QColor(255, 255, 0), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            expanded = highlight_rect.adjusted(-2, -2, 2, 2)
            painter.drawRect(expanded)
            painter.end()

            lab.setPixmap(QPixmap.fromImage(cur_img))

    def _restore_dimmed_preview(self, lab):
        """鼠标离开预览时恢复调暗版。"""
        if not self._dpi_select_mode:
            return
        self.update_previews()

    def _try_select_path_at(self, page, point, px, py):
        """在点击位置选择路径水印，弹出 PathMatchDialog。"""
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            from PathMatchDialog import PathMatchDialog
            from path_wm import count_matching_paths
        except ImportError:
            QMessageBox.warning(self, "错误", "无法加载 PathMatchDialog")
            return

        doc = self.doc_orig if self.doc_orig else self.doc_clean
        if doc is None:
            return

        drawings = page.get_drawings(extended=True)
        # 查找点击位置的路径
        hit = None
        for d in drawings:
            if d.get('rect') and d['rect'].contains(point):
                hit = d
                break

        if not hit:
            # 退而求其次：扫描页面内容流，找包含点击点的路径块。
            # 适用于路径被容器/对象间接组织、get_drawings 命中不到的情况。
            try:
                for xref in page.get_contents():
                    stream = self.doc_orig.xref_stream(xref) if self.doc_orig else None
                    if not stream:
                        continue
                    text = stream.decode('latin-1', errors='replace')
                    count, matches = count_matching_paths(text)
                    if count and matches:
                        hit = matches[0]
                        break
            except Exception:
                pass

        if not hit:
            QMessageBox.information(self, "选择路径", "点击位置没有路径。")
            return

        # 确定填充色（从 drawing 属性获取）
        fill_color = None
        if hit.get('fill') and hit['fill'] != (0.0, 0.0, 0.0):
            fill_color = hit['fill']

        # 构造对话框
        idx = self.page_spin.value() - 1
        try:
            dlg = PathMatchDialog(doc, idx, self._working_path, page.rect, self.scale, self)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.log_exception(e, "_try_select_path_at construct")
            QMessageBox.warning(self, "错误", f"构造对话框失败: {e}\n{tb}")
            return

        # 自动填充颜色
        if fill_color:
            dlg.fill_row.set_color(fill_color)

        if dlg.exec() != dlg.DialogCode.Accepted:
            return

        # 用户确认后，退出选图模式
        if self._dpi_select_mode:
            self._dpi_select_mode = False
            self._dpi_selected_xref = None
            self._dpi_hover_tip.hide()
            self.btn_dpiwm.setText(TRANSLATIONS[self.lang]["dpi_wm"])
            self.update_previews()
            self.add_log(">>> 已退出选图模式")

        settings = dlg.get_settings()

        # 启动后台删除
        self.pbar.setValue(0)
        self.btn_dpiwm.setEnabled(False)

        try:
            from PathWmWorker import PathWmWorker
            self.pathwm_worker = PathWmWorker(self._working_path, settings, self)
            self.pathwm_worker.progress.connect(self._pathwm_progress)
            self.pathwm_worker.done.connect(self._pathwm_finished)
            self.pathwm_worker.error.connect(self._pathwm_failed)
            self.pathwm_worker.start()
        except Exception as e:
            self.btn_dpiwm.setEnabled(True)
            QMessageBox.critical(self, "错误", f"启动删除失败: {e}")

    def _pathwm_progress(self, percent, msg):
        self.pbar.setValue(percent)
        self.add_log(f">>> {msg}")

    def _pathwm_finished(self, doc, out_path):
        """路径水印删除完成回调。doc 是 fitz.Document 对象，out_path 是输出路径。"""
        self.btn_dpiwm.setEnabled(True)
        self.add_log(f">>> 路径水印删除完成: {out_path}")
        try:
            if self.doc_clean is not None:
                try:
                    self.doc_clean.close()
                except Exception:
                    pass
            self.doc_clean = doc  # 直接使用 worker 传来的 fitz 文档，无需重新打开
            self._working_path = out_path
            self.btn_save.setEnabled(True)
            self.btn_save_as.setEnabled(True)
            self._pending_path = out_path
            self.update_previews()
        except Exception as e:
            self.add_log(f">>> 打开处理结果失败: {e}")

    def _pathwm_failed(self, err):
        self.btn_dpiwm.setEnabled(True)
        self.add_log(f">>> 路径水印删除失败: {err}")
        try:
            QMessageBox.critical(self, "处理失败", str(err))
        except Exception:
            pass

    def load_pdf(self, path):
        """加载 PDF 文件（文件对话框、拖拽、最近文件共用的入口）。"""
        if fitz is None:
            _load_heavy_libs()
        if not path or not os.path.isfile(path):
            self.add_log(f"File not found: {path}")
            return False
        # 关闭旧文档，避免文件句柄泄漏（Windows 上尤其重要）
        for attr in ("doc_orig", "doc_clean"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        import gc
        gc.collect()
        try:
            self.doc_orig = fitz.open(path)
        except Exception as ex:
            self.log_exception(ex, f"Open failed: {os.path.basename(path)}")
            return False
        self.file_path = path
        self.display_lists = {}; self.doc_clean = None
        self._working_path = path  # 工作基底 = 原文件（去水印叠加的起点）
        self.btn_save.setEnabled(False)
        self.btn_save_as.setEnabled(False)
        self.btn_imgwm.setEnabled(True)
        self.btn_dpiwm.setEnabled(True)
        self.page_spin.setRange(1, len(self.doc_orig)); self.page_spin.setValue(1)
        self.add_log(f"File loaded: {os.path.basename(path)} ({len(self.doc_orig)} pages, {os.path.getsize(path):,} bytes)")
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
            QMessageBox.warning(self, "无法处理", "请先打开一个 PDF 文件")
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "等待中", "结构层清理还在进行，请稍候")
            return

        self.pbar.setValue(0)
        self.btn_clean.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        files = self.batch_files if len(self.batch_files) > 1 else [self.file_path]
        try:
            self.worker = MasterWorker(files[0], self.ratio_threshold)
            self.worker.batch_files = files if len(files) > 1 else []
            self.worker.extra_keywords = list(self.extra_keywords)
            self.worker.progress.connect(self.pbar.setValue)
            self.worker.log_signal.connect(self.add_log)
            self.worker.need_confirm.connect(self.ask_user)
            self.worker.done.connect(self.task_done)
            self.worker.failed.connect(lambda: (self.btn_clean.setEnabled(True),
                                                self.btn_cancel.setEnabled(False)))
            self.worker.start()
        except Exception as e:
            self.btn_clean.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.log_exception(e, "start_task")

    def start_image_wm(self):
        """🎨 去除图像水印：先扫描 PDF 所有图像 → 让用户选参数 → 后台处理"""
        _dbg('start_image_wm ENTER')
        if not self.file_path or not os.path.isfile(self.file_path):
            QMessageBox.warning(self, "无法处理", "请先打开一个 PDF 文件")
            return
        if self.imgwm_worker is not None and self.imgwm_worker.isRunning():
            self.add_log(">>> 图像水印处理进行中，请稍候")
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "等待中", "结构层清理还在进行，请稍候")
            return

        self.pbar.setValue(0)
        self.btn_imgwm.setEnabled(False)
        self.add_log(f">>> 图像水印处理启动: {os.path.basename(self._working_path)}")
        _dbg('image_wm 启动完成, working=%s, doc_orig=%s', self._working_path, self.doc_orig is not None)

        # 弹出对话框让用户选参数（默认用上次保存的 channel/fill/threshold）
        _dbg('image_wm 弹出对话框...')
        try:
            prev_channel, prev_fill = self.imgwm_mode.split("_", 1) if "_" in self.imgwm_mode else ("sat", self.imgwm_mode)
        except Exception:
            prev_channel, prev_fill = "sat", "inpaint"
        _dbg('image_wm 构造 ImageWmDialog (channel=%s, fill=%s, threshold=%s)', prev_channel, prev_fill, self.imgwm_threshold)
        dlg = ImageWmDialog(self, current_channel=prev_channel, current_fill=prev_fill, current_threshold=self.imgwm_threshold, scale=self.scale)
        _dbg('image_wm ImageWmDialog 构造完成, 调用 exec()...')
        if dlg.exec() != QDialog.DialogCode.Accepted:
            _dbg('image_wm 用户取消')
            self.add_log(">>> 用户取消图像水印处理")
            self.btn_imgwm.setEnabled(True)
            return
        _dbg('image_wm 用户确认, 获取设置...')
        settings = dlg.get_settings()
        _dbg('image_wm settings=%s', settings)
        self.imgwm_mode = f"{settings['channel']}_{settings['fill']}"
        self.imgwm_threshold = settings['threshold']
        self.add_log(f">>> 参数: channel={settings['channel']}, fill={settings['fill']}, threshold={settings['threshold']}")

        _dbg('image_wm 创建 ImageWmWorker...')
        self.imgwm_worker = ImageWmWorker(self._working_path, settings, self)
        _dbg('image_wm ImageWmWorker 创建完成, 连接信号...')
        self.imgwm_worker.progress.connect(self.pbar.setValue)
        self.imgwm_worker.log_signal.connect(self.add_log)
        self.imgwm_worker.done.connect(self._imgwm_finished)
        self.imgwm_worker.failed.connect(self._imgwm_failed)
        _dbg('image_wm 信号连接完成, 调用 start()...')
        self.imgwm_worker.start()
        _dbg('image_wm Worker start() 完成, isRunning=%s', self.imgwm_worker.isRunning())

    def _imgwm_finished(self, out_path, status):
        """图像水印处理完成回调。

        左侧预览保持原版（doc_orig 不动），直到用户点"保存"才更新。
        右侧预览显示处理结果（doc_clean = 处理结果）。
        叠加：_working_path 更新为 out_path，下次去水印基于此文件。
        """
        self.btn_imgwm.setEnabled(True)
        self.add_log(f">>> 图像水印处理完成: {out_path}")
        try:
            if self.doc_clean is not None:
                try: self.doc_clean.close()
                except Exception: pass
            self.doc_clean = fitz.open(out_path)
            self._working_path = out_path
            self.btn_save.setEnabled(True)
            self.btn_save_as.setEnabled(True)
            self._pending_path = out_path
            self.update_previews()
        except Exception as e:
            self.log_exception(e, "_imgwm_finished open")
            self.add_log(f">>> 打开处理结果失败: {e}")

    def _imgwm_failed(self, err):
        """图像水印处理失败回调。"""
        self.btn_imgwm.setEnabled(True)
        self.add_log(f">>> 图像水印处理失败: {err}")
        try:
            QMessageBox.critical(self, "处理失败", str(err))
        except Exception:
            pass

    def stop_task(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop_flag = True
            self.add_log(">>> 正在停止…")
            self.btn_cancel.setEnabled(False)

    def ask_user(self, ic, tc):
        """弹窗确认水印，把 dialog.get_selection() 的 dict 完整写回 worker。"""
        self.add_log(f">>> ask_user: {len(ic)} imgs, {len(tc)} txts")
        try:
            dialog = EnhancedWatermarkDialog(ic, tc, self.doc_orig, lang=self.lang, scale=self.scale, parent=self,
                                             adobe_info=getattr(self.worker, 'adobe_info', None),
                                             annot_info=getattr(self.worker, 'annot_info', None),
                                             ocg_info=getattr(self.worker, 'ocg_info', None),
                                             xobj_info=getattr(self.worker, 'xobj_info', None),
                                             extgs_info=getattr(self.worker, 'extgs_info', None),
                                             type3_info=getattr(self.worker, 'type3_info', None),
                                             nested_info=getattr(self.worker, 'nested_info', None),
                                             uri_info=getattr(self.worker, 'uri_info', None),
                                             pattern_info=getattr(self.worker, 'pattern_info', None),
                                             struct_info=getattr(self.worker, 'struct_info', None),
                                             meta_info=getattr(self.worker, 'meta_info', None),
                                             outline_info=getattr(self.worker, 'outline_info', None))
            self.add_log(">>> Dialog constructed; calling exec()...")
            if dialog.exec():
                self.add_log(">>> Dialog accepted")
                sel = dialog.get_selection()
                # 完整 dict 写回 worker
                self.worker._confirmed_selection = sel
                # 兼容旧字段
                self.worker.confirmed_hashes = sel.get('imgs', [])
                self.worker.confirmed_texts = sel.get('txts', [])
                self.worker.confirmed_adobe = sel.get('adobe', False)
                self.worker.apply_all_requested = dialog.get_apply_all()
                summary_parts = [
                    f"{len(sel.get('imgs', []))} img",
                    f"{len(sel.get('txts', []))} txt",
                    f"adobe={'Y' if sel.get('adobe') else 'N'}",
                    f"annot={'Y' if sel.get('annot') else 'N'}",
                    f"ocg={len(sel.get('ocg', []))}",
                    f"xobj={'Y' if sel.get('xobj') else 'N'}",
                    f"extgs={'Y' if sel.get('extgs') else 'N'}",
                    f"uri={'Y' if sel.get('uri') else 'N'}",
                    f"meta={'Y' if sel.get('meta') else 'N'}",
                ]
                self.add_log(f"User confirmed: {', '.join(summary_parts)}.")
            else:
                self.add_log("Clean process cancelled by user.")
        except Exception as ex:
            self.log_exception(ex, "ask_user dialog error")
        self.worker.is_confirmed = True

    def task_done(self, doc, out_path):
        """结构层去水印完成回调。

        左侧预览保持原版（doc_orig 不动），直到用户点"保存"才更新。
        右侧预览显示处理结果（doc_clean = 处理结果）。
        叠加：_working_path 更新为 out_path，下次去水印基于此文件。
        """
        self.btn_clean.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if doc is None:
            return
        try:
            if self.doc_clean is not None:
                try: self.doc_clean.close()
                except Exception: pass
            self.doc_clean = doc
            self._working_path = out_path or self.file_path
            self.btn_save.setEnabled(True)
            self.btn_save_as.setEnabled(True)
            self._pending_path = out_path
            self.update_previews()
        except Exception as e:
            self.log_exception(e, "task_done open")
            self.add_log(f">>> 打开处理结果失败: {e}")

    def save_pdf_inplace(self):
        """保存：直接覆盖源文件（不改变文件名和路径）。

        Windows 上 os.replace 会因源文件句柄被占用（doc_orig / doc_clean 还开着）
        返回 WinError 5；即使换 os.remove+rename 也会 WinError 32（文件被另一个程序占用）。
        正确顺序：先落盘 tmp → 关闭所有 fitz doc → 删源 → 改名 → 重新打开。
        """
        if self.doc_clean is None:
            return
        src = self.file_path
        if not src or not os.path.isfile(src):
            self.add_log(f"ERROR 保存失败：源文件不存在 {src}")
            return
        tmp = src + ".tmp_cleaning"
        # 1. 先把清理后的内容写到 tmp（此时 doc_clean 还开着，但 write 不受影响）
        try:
            self.doc_clean.save(tmp, garbage=4, deflate=True)
        except Exception as e:
            self.log_exception(e, "save_pdf_inplace.save")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            QMessageBox.critical(self, "保存失败", f"写入临时文件失败：\n{e}")
            return

        # 2. 关闭所有持有 src 句柄的 doc（doc_orig 是加载时打开的源文件，doc_clean 是 save 后的对象）
        for attr in ("doc_orig", "doc_clean"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        # 让 GC 立刻回收 PyMuPDF 内部缓冲区
        import gc
        gc.collect()
        # 短暂等待，让 Windows 文件系统释放锁
        time.sleep(0.3)

        # 3. 替换：优先 os.replace（Windows 同盘原子），失败则 remove+rename
        try:
            try:
                os.replace(tmp, src)
            except (PermissionError, OSError):
                # 兜底：删除 + 重命名
                try:
                    if os.path.exists(src):
                        os.remove(src)
                except Exception:
                    raise
                os.rename(tmp, src)
        except Exception as e:
            self.log_exception(e, "save_pdf_inplace.replace")
            # 清理临时文件
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            QMessageBox.critical(
                self, "保存失败",
                f"覆盖源文件失败：\n{e}\n\n"
                f"提示：源文件正被其他程序占用（如 PDF 阅读器）。请关闭后再试，\n"
                f"或使用『另存为』按钮另存到不同路径。"
            )
            # 重新打开源文件保持 UI 一致
            self._reopen_orig(src)
            return

        self.add_log(f"Saved (overwrite source): {src} ({os.path.getsize(src):,} bytes)")
        # 4. 重新打开保存后的文件：两侧预览都显示"刚保存后的 PDF"，
        #    工作基底重置为原文件路径（因为原文件已被覆盖为最新状态）
        self.doc_orig = fitz.open(src)
        self.doc_clean = fitz.open(src)
        self._working_path = src
        self.btn_save.setEnabled(True)
        self.btn_save_as.setEnabled(True)
        if hasattr(self, "page_spin"):
            self.page_spin.setRange(1, len(self.doc_orig))
            self.page_spin.setValue(1)
        self.update_previews()

    def _reopen_orig(self, src):
        """重新打开源文件为 doc_orig，失败时静默。"""
        try:
            self.doc_orig = fitz.open(src)
            if hasattr(self, "page_spin"):
                self.page_spin.setRange(1, len(self.doc_orig))
                self.page_spin.setValue(1)
            if hasattr(self, "update_previews"):
                self.update_previews()
        except Exception:
            self.doc_orig = None

    def _reopen_clean(self, src):
        """重新打开清理后文件为 doc_clean，失败时静默。"""
        try:
            self.doc_clean = fitz.open(src)
            self.btn_save.setEnabled(True)
            self.btn_save_as.setEnabled(True)
            self.update_previews()
        except Exception:
            self.doc_clean = None

    def save_as_pdf(self):
        if self.doc_clean is None:
            return
        default = os.path.join(os.path.dirname(self.file_path),
                               f"cleaned_{os.path.basename(self.file_path)}")
        path, _ = QFileDialog.getSaveFileName(self, "Save", default, "PDF (*.pdf)")
        if path:
            try:
                self.doc_clean.save(path, garbage=4, deflate=True)
                self.add_log(f"Saved (另存为): {path} ({os.path.getsize(path):,} bytes)")
            except Exception as e:
                self.log_exception(e, "save_as_pdf")
                QMessageBox.critical(self, "保存失败", f"另存为失败：\n{e}")

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    app = QApplication(sys.argv)
    apply_dark_mode(app, None)
    window = UltraAppFinal()
    # 拖拽 PDF 到 exe 图标自动打开：接收命令行第一个参数
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]) and sys.argv[1].lower().endswith('.pdf'):
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(150, lambda p=sys.argv[1]: window.load_pdf(p))
    # 重型库后台延迟加载，窗口先出现
    import threading
    threading.Thread(target=_load_heavy_libs, daemon=True).start()
    sys.exit(app.exec())

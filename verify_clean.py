#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_clean.py — 验证去水印结果（清理工具配套）
====================================================
检查项：
  1. 输出文件是否还带加密字典/权限位（应为无）。
  2. 逐页检查水印关键词是否残留（应为 0 页）。
  3. 正文抽样关键词是否保留（--body 可多次指定，缺失会告警）。
  4. 给定 --orig 原文件与 --pages 时，做像素级渲染对比：
     输出"差异像素占比"与差异区域 bbox——理想情况差异仅在水印斜带内。

用法：
  python verify_clean.py cleaned.pdf -k "Confidential" -k "Sichuan Airlines"
  python verify_clean.py cleaned.pdf -k "Confidential" --body "29-11-67" --orig in.pdf --pages 1 26 104

退出码：0=全部通过；1=有残留/结构异常；2=参数错误。
"""
import argparse
import sys

import pikepdf


def main():
    ap = argparse.ArgumentParser(description="验证去水印结果")
    ap.add_argument("pdf", help="待验证的清理后 PDF")
    ap.add_argument("-k", "--keyword", action="append", default=[], help="水印关键词")
    ap.add_argument("-b", "--body", action="append", default=[], help="必须保留的正文关键词")
    ap.add_argument("--orig", default=None, help="原 PDF（做像素级渲染对比）")
    ap.add_argument("--pages", type=int, nargs="+", default=[1], help="渲染对比页（1 起）")
    args = ap.parse_args()

    problems = 0

    # 1) 加密/结构
    try:
        with pikepdf.open(args.pdf) as pdf:
            enc = pdf.is_encrypted
            print(f"[1] 加密字典: {'无 ✅ 权限已移除' if not enc else '仍存在 ❌'}")
            if enc:
                problems += 1
            n_pages = len(pdf.pages)
            print(f"    页数: {n_pages}")
    except Exception as e:
        print(f"[1] 打开失败 ❌ {e}")
        sys.exit(1)

    # 2) 水印残留
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            print("[2] 未安装 PyMuPDF，跳过文本残留检查（pip install pymupdf）")
            pymupdf = None

    if pymupdf is not None:
        doc = pymupdf.open(args.pdf)
        residual = 0
        for i in range(doc.page_count):
            t = doc[i].get_text()
            if any(kw.lower() in t.lower() for kw in args.keyword):
                residual += 1
        print(f"[2] 水印关键词残留页数: {residual}/{doc.page_count} "
              f"{'✅' if residual == 0 else '❌'}")
        if residual:
            problems += 1

        missing = []
        for kw in args.body:
            if kw not in doc[0].get_text():
                missing.append(kw)
        if missing:
            print(f"[3] 首页正文抽样缺失 ❌: {missing}")
            problems += 1
        else:
            print(f"[3] 正文抽样关键词保留: {len(args.body)} 个 ✅（{args.body}）")
        doc.close()

    # 4) 像素级渲染对比
    if args.orig and pymupdf is not None:
        a = pymupdf.open(args.orig)
        b = pymupdf.open(args.pdf)
        for pno in args.pages:
            if pno < 1 or pno > min(a.page_count, b.page_count):
                print(f"[4] 页 {pno} 越界，跳过")
                continue
            pa = a[pno - 1].get_pixmap(matrix=pymupdf.Matrix(1.0, 1.0))
            pb = b[pno - 1].get_pixmap(matrix=pymupdf.Matrix(1.0, 1.0))
            sa, sb = pa.samples, pb.samples
            n = min(len(sa), len(sb))
            w = pa.width
            diff = 0
            minx = miny = 10 ** 9
            maxx = maxy = -1
            for i in range(0, n, 3):
                if sa[i] != sb[i] or sa[i + 1] != sb[i + 1] or sa[i + 2] != sb[i + 2]:
                    diff += 1
                    px, py = (i // 3) % w, (i // 3) // w
                    if px < minx: minx = px
                    if px > maxx: maxx = px
                    if py < miny: miny = py
                    if py > maxy: maxy = py
            pct = 100.0 * diff / (w * pa.height)
            box = f"({minx},{miny})-({maxx},{maxy})" if diff else "无"
            print(f"[4] 页 {pno}: 差异像素 {diff} ({pct:.2f}%) bbox={box} "
                  f"（页面 {w}x{pa.height}）")
        a.close()
        b.close()

    print("\n结果:", "全部通过 ✅" if problems == 0 else f"{problems} 项未通过 ❌")
    sys.exit(0 if problems == 0 else 1)


if __name__ == "__main__":
    main()
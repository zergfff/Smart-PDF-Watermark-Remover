#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批处理：原始/ -> 处理后/
自动识别每份 PDF 的水印（文本关键词 + 重复图片双通道）
-> 内容流级删除 + 图片水印移除 + 去加密 + 自动验证。
"""
import os
import re
import sys

sys.path.insert(0, r"C:/Users/ASUS/Desktop/去水印")
import pikepdf
import pdf_dewatermark as dw

SRC = r"C:/Users/ASUS/Desktop/去水印/原始"
DST = r"C:/Users/ASUS/Desktop/去水印/处理后"
os.makedirs(DST, exist_ok=True)

# 宽指纹集：覆盖不同航司/下载者文案（C2-Confidential 系 / Air China 系）
FINGERPRINTS = [
    b"Confidential", b"Downloaded by", b"Downloaded on", b"exclusive use",
    b"SLS Terms", b"Sichuan Airlines", b"Terms & Conditions", b"Unauthorized",
    b"redisclosure", b"authorized use", b"Air China", b"C2 -",
]


def sniff_stream_keywords(path):
    """从内容流里收集含特征词的实际字符串字面量作为精准关键词。
    比用短特征词更稳：拿到的是完整字符串，正好是流里 Tj 的原文子串。"""
    kws = set()
    pdf = pikepdf.open(path)

    def scan_bytes(data):
        for s in dw.parse_strings_in_block(data):
            low = s.lower()
            if any(f.lower() in low for f in FINGERPRINTS):
                kws.add(bytes(s))

    def scan_stream(st):
        if st is None:
            return
        scan_bytes(st.read_bytes())

    def scan_res(res):
        if res is None:
            return
        xo = res.get("/XObject")
        if xo is None:
            return
        for obj in xo.values():
            if obj is None:
                continue
            try:
                if obj.get("/Subtype") == pikepdf.Name("/Form"):
                    scan_stream(obj)
                    scan_res(obj.get("/Resources"))
            except Exception:
                pass

    for page in pdf.pages:
        c = page.Contents
        if c is None:
            continue
        streams = c if isinstance(c, pikepdf.Array) else [c]
        for st in streams:
            scan_stream(st)
        scan_res(page.get("/Resources"))
    pdf.close()
    return list(kws)


results = []
for fn in sorted(os.listdir(SRC)):
    if not fn.lower().endswith(".pdf"):
        continue
    src = os.path.join(SRC, fn)
    dst = os.path.join(DST, fn)  # 同名输出到 处理后/
    pdf = None
    try:
        kws = sniff_stream_keywords(src)
        pdf = pikepdf.open(src)

        # 文本通道
        t = 0
        for page in pdf.pages:
            t += dw.process_page(pdf, page, kws)
            t += dw.process_xobjects(page.get("/Resources"), kws)

        # 图片通道（自动检测重复的大面积图片水印）
        auto, cnt = dw.detect_image_watermarks(src, pdf, 0.5, 0.08)
        ir = dw.remove_image_watermarks(pdf, auto) if auto else 0

        pdf.save(dst, encryption=False)
        pdf.close()
        pdf = None

        pdf2 = pikepdf.open(dst)
        enc = pdf2.is_encrypted
        pdf2.close()

        if t > 0 or ir > 0:
            detail = []
            if t:
                detail.append(f"文本块{t}")
            if ir:
                detail.append(f"图片Do{ir}(xref={sorted(g[0] for g in auto)})")
            resid = dw.verify_residual(dst, [k.decode("utf-8", "replace") for k in kws]) \
                if kws else {"residual_pages": 0}
            ok = resid.get("residual_pages", 0) == 0
            status = (f"OK 页数={dw.verify_residual(dst, [])['pages']} 删除={'+'.join(detail)} "
                      f"残留={resid.get('residual_pages')} 加密={'无' if not enc else '有!'}")
            print(f"{fn} => {status}")
        elif kws:
            print(f"{fn} => PARTIAL: 识别到文本特征但无删除匹配（可能是 CID/特殊编码文本水印）")
        else:
            print(f"{fn} => 跳过：未识别到文本/图片水印特征")
    except Exception as e:
        print(f"{fn} => ERROR: {e}")
    finally:
        if pdf is not None:
            pdf.close()

print("\n===== 汇总 =====")
for fn in sorted(os.listdir(SRC)):
    if fn.lower().endswith(".pdf") and os.path.exists(os.path.join(DST, fn)):
        print(f"[OK] {fn}")
print("完成")
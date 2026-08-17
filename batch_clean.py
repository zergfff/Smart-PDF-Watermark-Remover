#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批处理：原始/ -> 处理后/
自动识别每份 PDF 的水印字符串（内容流级指纹收集）-> 内容流级删除 + 去加密 + 自动验证。
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
    dst = os.path.join(DST, fn)  # 同名输出到处理后/（默认加 _cleaned 也行）
    try:
        kws = sniff_stream_keywords(src)
        if not kws:
            msg = "跳过：未识别到文本水印特征（可能为图片/矢量水印或本无文本水印）"
            results.append((fn, msg))
            print(f"{fn} => {msg}")
            continue

        info = dw.clean_pdf(src, dst, kws)
        v = dw.verify_residual(dst, [k.decode("utf-8", "replace") for k in kws])
        resid = v.get("residual_pages", "?")
        enc_off = (v.get("encrypted", None) is False)
        if resid == 0 and enc_off:
            status = f"OK 页数={v.get('pages')} 删除块={info['removed']} 残留页=0 加密=无"
        else:
            status = f"PARTIAL/F 页数={v.get('pages')} 删除块={info['removed']} 残留页={resid} 加密移除={enc_off}"
        results.append((fn, status, len(kws)))
        print(f"{fn} => {status} | 关键词数: {len(kws)}")
    except Exception as e:
        results.append((fn, f"ERROR: {e}"))
        print(f"{fn} => ERROR: {e}")

print("\n===== 汇总 =====")
for r in results:
    print(f"[{'OK ' if r[1].startswith('OK') else '!! '}] {r[0]}  {r[1]}")
print(f"\n共 {len(results)} 个文件, 成功 {sum(1 for r in results if r[1].startswith('OK'))} 个")
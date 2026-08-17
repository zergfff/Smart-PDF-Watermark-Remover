# 批量像素级验证：原始 vs 处理后（抽查页）
import os
import pymupdf

PAIRS = [
    (r"C:/Users/ASUS/Desktop/去水印/原始/1258 Rev0.pdf",
     r"C:/Users/ASUS/Desktop/去水印/处理后/1258 Rev0.pdf", [1, 5, 10], ["AIRCRAFT", "MAINTENANCE"]),
    (r"C:/Users/ASUS/Desktop/去水印/原始/32-41-52 Rev2.pdf",
     r"C:/Users/ASUS/Desktop/去水印/处理后/32-41-52 Rev2.pdf", [1, 40, 100, 139], ["32-41-52", "COMPONENT"]),
    (r"C:/Users/ASUS/Desktop/去水印/原始/70167_25-62-31_Rev.15_36 PERSON LIFERAFT_1.pdf",
     r"C:/Users/ASUS/Desktop/去水印/处理后/70167_25-62-31_Rev.15_36 PERSON LIFERAFT_1.pdf",
     [1, 5, 10], ["25-62-31", "LIFERAFT"]),
]

for src, dst, pages, body_kw in PAIRS:
    a = pymupdf.open(src)
    b = pymupdf.open(dst)
    print(f"== {os.path.basename(src)}: 源 {a.page_count} 页 -> 处理 {b.page_count} 页, 加密标记={b.is_encrypted}")
    for pno in pages:
        pa = a[pno - 1].get_pixmap(matrix=pymupdf.Matrix(1.0, 1.0))
        pb = b[pno - 1].get_pixmap(matrix=pymupdf.Matrix(1.0, 1.0))
        sa, sb, n, w = pa.samples, pb.samples, min(len(pa.samples), len(pb.samples)), pa.width
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
        txt = b[pno - 1].get_text()
        ok_body = all(k.lower() in txt.lower() for k in body_kw)
        wm = ("Confidential" in txt or "Downloaded" in txt or "Unauthorized" in txt
              or "Sichuan" in txt or "SLS Terms" in txt or "redisclosure" in txt)
        print(f"  页{pno}: 差异 {pct:.2f}% bbox={box} | 正文关键词OK={ok_body} 水印残留={wm}")
    a.close()
    b.close()
print("DONE")
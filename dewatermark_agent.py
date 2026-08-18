#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dewatermark_agent.py — 面向 Agent 的 PDF 去水印接口
====================================================
两种模式：
  1) list <pdf>         列出该 PDF 中的「疑似水印」候选并编号
       [1] TEXT  以正式出版文本为准   (36.0pt, 16页, bbox=..., 旋转-35.0°)
       [2] TEXT  HS/T 85—2024         (10.5pt, 10页)
       [3] IMAGE xref=262 (26页)
    加 --json 输出机器可读 JSON，便于 Agent 解析。
  2) remove <pdf> --select ID[,ID...]  [-o out.pdf]
        只删除选中的候选（文本按关键词+几何，图片按 xref）
     --select all  删除全部候选
    默认 -o <原名>_agent_cleaned.pdf；删除后自动复检。

供 Agent 结合：Agent 先 list 拿编号，用户报号，Agent 再 remove --select 编号。
"""
import argparse
import json
import math
import os
import re
import sys

import fitz
import pikepdf
import pdf_dewatermark as dw


# ---------------------------------------------------------------- 检测
def _detect_text(path, ratio):
    import fitz
    doc = fitz.open(path)
    n = doc.page_count
    th = max(2, int(n * ratio))
    cand = {}
    pages_by = {}
    for i in range(n):
        page = doc[i]
        for b in page.get_text("rawdict")["blocks"]:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                spans = line["spans"]
                content = "".join(ch.get("c", "") for sp in spans
                                  for ch in sp.get("chars", [])).strip()
                if len(content) <= 1:
                    continue
                size = round(max(sp.get("size", 0) for sp in spans), 1)
                key = (content, size)
                pages_by.setdefault(key, []).append(i)
                if key not in cand:
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
                    cand[key] = {'text': content, 'size': size,
                                 'bbox': tuple(round(v, 1) for v in line['bbox']),
                                 'origin': origin, 'rot': rot}
    doc.close()
    out = []
    for key, c in cand.items():
        pg = sorted(set(pages_by.get(key, [])))
        if len(pg) >= th:
            out.append({'kind': 'text', 'text': c['text'], 'size': c['size'],
                        'bbox': c['bbox'], 'origin': c['origin'], 'rot': c['rot'],
                        'count': len(pg), 'pages': pg})
    return out


def _detect_image(path, ratio):
    pdf = pikepdf.open(path)
    auto, stats = dw.find_repeated_images(pdf, ratio)
    img_cnt, _form_cnt = stats
    # 只保留真正的图片（find_repeated_images 里的候选可能含包装 Form）
    img_obj = set()
    for g in auto:
        try:
            obj = pdf.get_object(g)
            if obj.get("/Subtype") == pikepdf.Name("/Image"):
                img_obj.add(g)
        except Exception:
            pass
    out = []
    if img_obj:
        import fitz
        doc = fitz.open(path)
        for g in sorted(img_obj, key=lambda x: x[0]):
            xref = g[0]
            rect = None
            for pno in range(doc.page_count):
                rs = doc[pno].get_image_rects(xref)
                if rs:
                    rect = tuple(round(v, 1) for v in rs[0])
                    break
            out.append({'kind': 'image', 'xref': xref, 'objgen': (xref, g[1]),
                        'count': img_cnt.get(g, 0), 'bbox': rect})
        doc.close()
    pdf.close()
    return out


def detect(path, ratio=0.3):
    text = _detect_text(path, ratio)
    img = _detect_image(path, ratio)
    cands = []
    _id = 1
    for c in text:
        c['id'] = _id
        _id += 1
        cands.append(c)
    for c in img:
        c['id'] = _id
        _id += 1
        cands.append(c)
    return cands


# ---------------------------------------------------------------- 渲染候选预览图
def render_crops(path, cands, outdir):
    """把每个候选所在的页面区域截图存成 PNG，供 Agent 看图确认。"""
    import fitz
    os.makedirs(outdir, exist_ok=True)
    doc = fitz.open(path)
    files = []
    for c in cands:
        page_idx = (c.get('pages') or [0])[0] if c['kind'] == 'text' else 0
        if page_idx >= doc.page_count:
            page_idx = 0
        page = doc[page_idx]
        box = fitz.Rect(c['bbox']) if c.get('bbox') else page.rect
        pad = 16
        clip = fitz.Rect(max(0, box.x0 - pad), max(0, box.y0 - pad),
                         min(page.rect.width, box.x1 + pad),
                         min(page.rect.height, box.y1 + pad))
        try:
            pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(2.0, 2.0))
            fn = os.path.join(outdir, f"{c['id']:02d}_{c['kind']}.png")
            pix.save(fn)
            files.append(fn)
        except Exception:
            continue
    doc.close()
    return files


# ---------------------------------------------------------------- 删除
def remove(path, out_path, ids, ratio=0.3):
    cands = detect(path, ratio)
    by_id = {c['id']: c for c in cands}
    if isinstance(ids, str) and ids.strip().lower() == "all":
        sel = cands
    else:
        sel = [by_id[i] for i in ids if i in by_id]

    keywords = []
    geo_targets = []
    img_objgens = set()
    for c in sel:
        if c['kind'] == 'text':
            if c.get('text'):
                keywords.append(c['text'])
            if c.get('origin') and c.get('size'):
                geo_targets.append((c['size'], c.get('rot', 0.0),
                                    c['origin'][0], c['origin'][1]))
        else:
            img_objgens.add(c['objgen'])

    kws = [k.encode('utf-8') for k in keywords]
    pdf = pikepdf.open(path)
    rem_txt = 0
    rem_geo = 0
    for page in pdf.pages:
        rem_txt += dw.process_page(pdf, page, kws)
        rem_txt += dw.process_xobjects(page.get('/Resources'), kws)
        if geo_targets:
            rem_geo += dw.process_page_geo(pdf, page, geo_targets)
    rem_img = 0
    if img_objgens:
        rem_img = dw.remove_image_watermarks(pdf, img_objgens)
    pdf.save(out_path, encryption=False)
    pdf.close()

    # 复检
    try:
        import fitz
    except ImportError:
        fitz = None
    resid_txt = 0
    resid_img = 0
    if fitz is not None:
        d = fitz.open(out_path)
        for pg in d:
            t = pg.get_text()
            if any(k in t for k in keywords):
                resid_txt += 1
        for c in sel:
            if c['kind'] == 'image':
                for pg in d:
                    if any(g[0] == c['xref'] for g in pg.get_images(full=True)):
                        resid_img += 1
        d.close()
    return {'selected': [c['id'] for c in sel], 'text_blocks': rem_txt,
            'geo_blocks': rem_geo, 'image_do': rem_img,
            'residual_text_pages': resid_txt, 'residual_images': resid_img}


# ---------------------------------------------------------------- 前后对比预览
def _clip_rect(c, page):
    box = fitz.Rect(c['bbox']) if c.get('bbox') else page.rect
    pad = 16
    return fitz.Rect(max(0, box.x0 - pad), max(0, box.y0 - pad),
                     min(page.rect.width, box.x1 + pad),
                     min(page.rect.height, box.y1 + pad))


def render_before_after(path, cands, outdir, ratio=0.3):
    """为每个候选生成 前/后 两张同区域截图：
    <id>_before.png（含水印）与 <id>_after.png（只删该候选后）。"""
    import fitz
    os.makedirs(outdir, exist_ok=True)
    files = []
    doc = fitz.open(path)
    for c in cands:
        page_idx = (c.get('pages') or [0])[0] if c['kind'] == 'text' else 0
        if page_idx >= doc.page_count:
            page_idx = 0
        page = doc[page_idx]
        clip = _clip_rect(c, page)
        try:
            pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(2.0, 2.0))
            b = os.path.join(outdir, f"{c['id']:02d}_before.png")
            pix.save(b)
        except Exception:
            continue
        # 生成只去掉该候选的临时清理版，渲染同一区域
        tmp = os.path.join(outdir, f"_tmp_{c['id']}.pdf")
        try:
            remove(path, tmp, [c['id']], ratio)
            d2 = fitz.open(tmp)
            p2 = d2[page_idx] if page_idx < d2.page_count else d2[0]
            clip2 = _clip_rect(c, p2)
            pix2 = p2.get_pixmap(clip=clip2, matrix=fitz.Matrix(2.0, 2.0))
            a = os.path.join(outdir, f"{c['id']:02d}_after.png")
            pix2.save(a)
            d2.close()
        except Exception:
            a = None
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        files.append((b, a))
    doc.close()
    return files


# ---------------------------------------------------------------- CLI
def _fmt(c):
    if c['kind'] == 'text':
        return (f"[{c['id']}] TEXT  {c['text'][:60]!r}  "
                f"({c['size']}pt, {c['count']}页, bbox={c['bbox']}, 旋转{c.get('rot', 0.0):.1f}°)")
    return (f"[{c['id']}] IMAGE xref={c['xref']} ({c['count']}页, "
            f"bbox={c['bbox']})")


def main():
    ap = argparse.ArgumentParser(description="PDF 去水印 Agent 接口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出疑似水印候选并编号")
    p_list.add_argument("pdf")
    p_list.add_argument("--ratio", type=float, default=0.3)
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--previews-dir", default=None,
                        help="同时把每个候选区域截图存到该目录(供Agent看图确认)")
    p_list.add_argument("--before-after-dir", default=None,
                        help="生成每个候选的『去除前/去除后』对比图到该目录")

    p_rm = sub.add_parser("remove", help="按编号删除水印")
    p_rm.add_argument("pdf")
    p_rm.add_argument("--select", required=True, help="候选编号，逗号分隔，或 all")
    p_rm.add_argument("-o", "--output", default=None)
    p_rm.add_argument("--ratio", type=float, default=0.3)
    args = ap.parse_args()

    if args.cmd == "list":
        cands = detect(args.pdf, args.ratio)
        if args.json:
            # 去 objgen 元组（JSON 不可序列化）
            clean = []
            for c in cands:
                cc = {k: v for k, v in c.items() if k != 'objgen'}
                if c['kind'] == 'image':
                    cc['xref'] = c['xref']
                clean.append(cc)
            json.dump(clean, sys.stdout, ensure_ascii=False, indent=1)
            print()
        else:
            print(f"共 {len(cands)} 个候选，编号如下：")
            for c in cands:
                print("  " + _fmt(c))
            if args.previews_dir:
                files = render_crops(args.pdf, cands, args.previews_dir)
                print(f"\n候选预览图已存到 {args.previews_dir}：")
                for f in files:
                    print("  " + f)
            if args.before_after_dir:
                pairs = render_before_after(args.pdf, cands, args.before_after_dir, args.ratio)
                print(f"\n候选『前/后』对比图已存到 {args.before_after_dir}：")
                for b, a in pairs:
                    print(f"  [{b.split(os.sep)[-1].split('_')[0]}] 前: {b}\n     后: {a}")
            print("删除示例: dewatermark_agent.py remove <pdf> --select 1,3")
        return

    # remove
    out = args.output or re.sub(r"\.pdf$", "", args.pdf, flags=re.I) + "_agent_cleaned.pdf"
    sel_ids = []
    if args.select.strip().lower() != "all":
        for part in args.select.split(","):
            part = part.strip()
            if part.isdigit():
                sel_ids.append(int(part))
    res = remove(args.pdf, out, sel_ids if sel_ids else "all", args.ratio)
    print(f"输出: {out}")
    print(f"选中删除: #{res['selected']}  "
          f"文本块 {res['text_blocks']}(关键词) + {res['geo_blocks']}(几何)  "
          f"图片Do {res['image_do']}")
    if res['residual_text_pages'] == 0 and res['residual_images'] == 0:
        print("复检: 无残留 ✅")
    else:
        print(f"复检: 文本残留页 {res['residual_text_pages']}, 图片残留 {res['residual_images']} ⚠️")


if __name__ == "__main__":
    main()
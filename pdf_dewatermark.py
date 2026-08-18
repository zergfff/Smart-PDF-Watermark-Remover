#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_dewatermark.py — 手搓 PDF 去水印 + 去权限工具（命令行版）
================================================================
原理（区别于"红色矩形遮盖"方案）：
  1. 逐页解析 PDF 内容流（Contents），识别 BT...ET 文本块，
     提取块内 Tj/TJ 绘制的字符串，命中关键词的块整体删除。
     水印若独占一个流，则整个流移除 —— 完全不触碰正文/图形。
  2. 保存时 encryption=False，把文档的加密字典和权限位
     （禁止打印/复制/修改等）一并移除。
  3. 默认 --verify：用 PyMuPDF 重新打开输出，逐页检查水印关键词残留。

解析器说明（字符串感知）：
  内容流里的 BT/ET 是"操作符"，但字符串字面量里也可能出现大写 BT/ET
  字符（如正文 "(BT-2000 series...)"）。本工具扫描内容流时同时追踪
  字符串括号 (...) 与十六进制串 <...> 的嵌套状态，只在字符串之外识别
  BT/ET —— 避免把正文里的 "BT" 误当成文本块起点导致误删正文。

用法：
  python pdf_dewatermark.py in.pdf -o out.pdf -k "C2 - Confidential"
  python pdf_dewatermark.py in.pdf -o out.pdf -k "Confidential" -k "exclusive use of Sichuan"
  # 不加 -o 时输出到 <原文件名>_cleaned.pdf
  # -k 可多次指定；关键词为子串匹配（内容流中明文/hex 均会转码后比对）
  # --no-verify 关闭输出后的自动残留检查

依赖：pikepdf（必选）、pymupdf（可选，--verify 用）
"""
import argparse
import math
import re
import sys
from collections import Counter

import pikepdf


# ---------------------------------------------------------------- 内容流解析

def _find_ops(stream: bytes) -> list:
    """扫描内容流，返回字符串之外的 (BT/ET, 位置) 操作符列表。
    跳过 (…) 字符串（含转义/嵌套）、<…> 十六进制串与 <<…>> 字典的字节。"""
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


def split_bt_et_pieces(stream: bytes) -> list:
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


def split_bt_et_blocks(stream: bytes) -> tuple:
    """按字符串感知的 BT/ET 把内容流切成块。
    返回 (块列表, 非块内容拼接)——非块内容按原顺序保留。"""
    pieces = split_bt_et_pieces(stream)
    blocks = [b for kind, b in pieces if kind == "block"]
    tail = b"".join(b for kind, b in pieces if kind == "rest")
    return blocks, tail


def parse_strings_in_block(block: bytes) -> list:
    """从 BT..ET 块中提取所有 Tj/TJ 字符串（明文 (...) 与 hex <...>）。"""
    out = []
    i = 0
    n = len(block)
    while i < n:
        c = block[i]
        if c == ord("("):  # 明文串，处理转义
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
                            ord("0") <= block[j + kk] <= ord("7") for kk in (1, 2, 3)):
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
        elif c == ord("<"):  # hex 串
            j = i + 1
            while j < n and block[j] != ord(">"):
                j += 1
            hexpart = re.sub(rb"\s", b"", block[i + 1:j])
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


def block_matches(block: bytes, keywords: list) -> bool:
    """块内任一字符串命中任一关键词（子串匹配，忽略大小写）。"""
    if not keywords:
        return False
    for s in parse_strings_in_block(block):
        ls = s.lower()
        for kw in keywords:
            if kw.lower() in ls:
                return True
    return False


def strip_watermark_stream(data: bytes, keywords: list) -> tuple:
    """从单个内容流中删除命中关键词的 BT..ET 块。
    返回 (新流或 None 表示流已空, 删除块数)。"""
    pieces = split_bt_et_pieces(data)
    kept = []
    removed = 0
    for kind, b in pieces:
        if kind == "block" and block_matches(b, keywords):
            removed += 1
        else:
            kept.append(b)
    if removed == 0:
        return data, 0
    new_data = b"".join(kept)  # 保持原始顺序；rest 与保留块交错位置不变
    if new_data.strip() in (b"", b"q", b"Q", b"q\nQ", b"Q\nq"):
        return None, removed
    return new_data, removed


# ---------------------------------------------------------------- 几何签名删除
# 针对 CID/特殊编码字体水印：关键词匹配不到明文时，按"字号+旋转+起点"匹配文本块

def _mmult(m1, m2):
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2]


def _ang_diff(a, b):
    d = (a - b) % 360
    return min(d, 360 - d)


def _block_text_signature(blk: bytes):
    """解析 BT..ET 块中第一个文本绘制的签名 (字号, 旋转角度, 起点x, 起点y)。
    无文本则返回 None。"""
    import math as _math
    ops = []
    for m in re.finditer(rb"/[\w.#]+\s+([-\d.]+)\s+Tf", blk):
        ops.append((m.start(), "Tf", float(m.group(1))))
    for m in re.finditer(rb"([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+Tm", blk):
        ops.append((m.start(), "Tm", [float(m.group(i)) for i in range(1, 7)]))
    for m in re.finditer(rb"([-\d.]+)\s+([-\d.]+)\s+Td", blk):
        ops.append((m.start(), "Td", [float(m.group(1)), float(m.group(2))]))
    for m in re.finditer(rb"([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+cm", blk):
        ops.append((m.start(), "cm", [float(m.group(i)) for i in range(1, 7)]))
    show = len(blk)
    for m in re.finditer(rb"\)\s*Tj|\]\s*TJ", blk):
        show = m.start()
        break
    ops.sort(key=lambda x: x[0])
    M = [1, 0, 0, 1, 0, 0]
    tm = None
    size = None
    for pos, op, val in ops:
        if pos > show:
            break
        if op == "Tf":
            size = val
        elif op == "cm":
            M = _mmult(M, val)
        elif op == "Tm":
            tm = list(val)
        elif op == "Td":
            if tm is None:
                tm = [1, 0, 0, 1, 0, 0]
            tm[4] += val[0]
            tm[5] += val[1]
    if tm is None or size is None:
        return None
    # 有效字号 = Tf 字号 × Tm 缩放（CJK 字体常见 Tf=1，真实字号在 Tm 里）
    eff_size = size * tm[0]
    # combined = M * tm，取旋转与平移
    a1, b1, c1, d1, e1, f1 = M
    a2, b2, c2, d2, e2, f2 = tm
    ca = a1 * a2 + b1 * c2
    cb = a1 * b2 + b1 * d2
    ox = e1 * a2 + f1 * c2 + e2
    oy = e1 * b2 + f1 * d2 + f2
    rot = _math.degrees(_math.atan2(cb, ca))
    return (eff_size, rot, ox, oy)


def _strip_block_single_draw(blk: bytes, targets: list) -> tuple:
    """块内精确删除：解析块内各 Tj/TJ 绘制片段，只删几何签名命中目标的片段。
    返回 (新块字节, 删除片段数)。"""
    import math as _math
    ops = []
    for m in re.finditer(rb"/[\w.#]+\s+([-\d.]+)\s+Tf", blk):
        ops.append((m.start(), "Tf", float(m.group(1))))
    for m in re.finditer(rb"([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+Tm", blk):
        ops.append((m.start(), "Tm", [float(m.group(i)) for i in range(1, 7)]))
    for m in re.finditer(rb"([-\d.]+)\s+([-\d.]+)\s+Td", blk):
        ops.append((m.start(), "Td", [float(m.group(1)), float(m.group(2))]))
    for m in re.finditer(rb"\[[^\]]*\]\s*TJ|\([^)]*\)\s*Tj", blk):
        ops.append((m.start(), "draw", m.end()))
    ops.sort(key=lambda x: x[0])

    M = [1, 0, 0, 1, 0, 0]
    tm = None
    size = None
    draws = []
    for pos, op, val in ops:
        if op == "Tf":
            size = val
        elif op == "cm":
            M = _mmult(M, val)
        elif op == "Tm":
            tm = list(val)
        elif op == "Td":
            if tm is None:
                tm = [1, 0, 0, 1, 0, 0]
            tm[4] += val[0]
            tm[5] += val[1]
        elif op == "draw":
            if tm is not None and size is not None:
                eff_size = size * tm[0]
                a1, b1, c1, d1, e1, f1 = M
                a2, b2, c2, d2, e2, f2 = tm
                ca = a1 * a2 + b1 * c2
                cb = a1 * b2 + b1 * d2
                ox = e1 * a2 + f1 * c2 + e2
                oy = e1 * b2 + f1 * d2 + f2
                rot = _math.degrees(_math.atan2(cb, ca))
                hit = False
                for tsz, trot, tox, toy in targets:
                    if (abs(eff_size - tsz) <= 1.0 and _ang_diff(rot, trot) <= 6.0 and
                            abs(ox - tox) <= 6.0 and abs(oy - toy) <= 6.0):
                        hit = True
                        break
                draws.append((pos, val, hit))
    removed = 0
    new = bytearray(blk)
    for start, end, hit in reversed(draws):
        if hit:
            # 只删字符串字面量起始到操作符结束（保留 Tf/Tm/Td 状态；多余状态无害）
            del new[start:end]
            removed += 1
    return bytes(new), removed


def strip_watermark_blocks_geo(data: bytes, targets: list,
                               tol_size=1.0, tol_rot=6.0, tol_xy=20.0) -> tuple:
    """按几何签名删除文本块（块内精确删除命中片段，保留同块其他内容）。"""
    pieces = split_bt_et_pieces(data)
    kept = []
    removed = 0
    for kind, b in pieces:
        if kind == "block":
            sig = _block_text_signature(b)
            hit = False
            if sig is not None:
                for tsz, trot, tox, toy in targets:
                    if (abs(sig[0] - tsz) <= tol_size and
                            _ang_diff(sig[1], trot) <= tol_rot and
                            abs(sig[2] - tox) <= tol_xy and
                            abs(sig[3] - toy) <= tol_xy):
                        hit = True
                        break
                if hit:
                    nb, nremoved = _strip_block_single_draw(b, targets)
                    removed += nremoved
                    if nremoved > 0:
                        if nb.strip() in (b"", b"q", b"Q"):
                            continue
                        kept.append(nb)
                        continue
            kept.append(b)
        else:
            kept.append(b)
    if removed == 0:
        return data, 0
    new_data = b"".join(kept)
    if new_data.strip() in (b"", b"q", b"Q", b"q\nQ", b"Q\nq"):
        return None, removed
    return new_data, removed

def _scan_do_ctms(data: bytes) -> dict:
    """扫描内容流：返回 {Form名: [绘制时 CTM, ...]}（考虑 q/Q 状态栈）。"""
    out = {}
    M = [1, 0, 0, 1, 0, 0]
    stack = []
    nums = []
    i, n = 0, len(data)

    DELIM = b' \t\r\n()<>[]{}/%'
    WS = b' \t\r\n'

    while i < n:
        c = data[i]
        if c in WS:
            i += 1
            continue
        if c == 0x28:  # 字符串
            depth = 1
            i += 1
            while i < n and depth:
                if data[i] == 0x5C:
                    i += 2
                    continue
                if data[i] == 0x28:
                    depth += 1
                elif data[i] == 0x29:
                    depth -= 1
                i += 1
            continue
        if c == 0x3C:  # <...>
            j = data.find(b">", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == 0x2F:  # /名字
            j = i + 1
            while j < n and data[j] not in DELIM:
                j += 1
            nm = data[i + 1:j]
            k = j
            while k < n and data[k] in WS:
                k += 1
            if data[k:k + 2] == b"Do":
                out.setdefault(nm.decode("latin-1", "replace"), []).append(list(M))
                i = k + 2
            else:
                i = j
            continue
        if c in b"+-.0123456789":
            j = i + 1
            while j < n and data[j] in b"+-.0123456789eE":
                j += 1
            try:
                nums.append(float(data[i:j]))
            except Exception:
                pass
            i = j
            continue
        # 操作符
        if data[i:i + 2] == b"cm" and (i + 2 >= n or data[i + 2] in DELIM) and len(nums) >= 6:
            M = _mmult(M, nums[-6:])
            nums = []
            i += 2
            continue
        if data[i:i + 1] == b"q":
            stack.append(list(M))
            i += 1
            continue
        if data[i:i + 1] == b"Q":
            if stack:
                M = stack.pop()
            i += 1
            continue
        j = i + 1
        while j < n and data[j] not in DELIM:
            j += 1
        nums = []  # 其它操作符清空数字缓冲
        i = j
    return out


def _process_form_geo(obj, m_total, targets):
    """处理单个 Form 流：块签名经 m_total 变换到页面坐标后与 targets 比较。"""
    total = 0
    try:
        data = obj.read_bytes()
    except Exception:
        return 0
    mf = obj.get("/Matrix")
    try:
        M = _mmult(m_total, [float(x) for x in mf]) if mf is not None else list(m_total)
    except Exception:
        M = list(m_total)
    pieces = split_bt_et_pieces(data)
    kept = []
    removed = 0
    for kind, b in pieces:
        if kind == "block":
            sig = _block_text_signature(b)
            hit = False
            if sig is not None:
                sz, rot, ox, oy = sig
                px = M[0] * ox + M[2] * oy + M[4]
                py = M[1] * ox + M[3] * oy + M[5]
                rot2 = rot + math.degrees(math.atan2(M[1], M[0]))
                for tsz, trot, tox, toy in targets:
                    if (abs(sz - tsz) <= 1.0 and _ang_diff(rot2, trot) <= 6.0 and
                            abs(px - tox) <= 6.0 and abs(py - toy) <= 6.0):
                        hit = True
                        break
            if hit:
                # 块内精确删除：只删命中片段，保留同块其他内容
                nb, nremoved = _strip_block_single_draw(b, targets)
                removed += nremoved
                if nremoved > 0:
                    if nb.strip() in (b"", b"q", b"Q"):
                        continue
                    kept.append(nb)
                    continue
        kept.append(b)
    if removed:
        new_data = b"".join(kept)
        if new_data.strip() not in (b"", b"q", b"Q", b"q\nQ", b"Q\nq"):
            try:
                obj.write(new_data)
            except Exception:
                pass
        total += removed
    total += _process_form_children(obj.get("/Resources"), M, targets)
    return total


def _process_form_children(res, m_total, targets):
    total = 0
    if res is None:
        return 0
    xo = res.get("/XObject")
    if xo is None:
        return 0
    for obj in xo.values():
        if obj is None:
            continue
        try:
            sub = obj.get("/Subtype")
        except Exception:
            continue
        if sub == pikepdf.Name("/Form"):
            total += _process_form_geo(obj, m_total, targets)
    return total


def locate_text_instances(path, text, size=None, rot=None, pages=None):
    """用 PyMuPDF 在页面上定位候选文本的所有实例位置（可解码 CID）。
    返回 {页索引: [(bbox...), ...]}，只匹配文本相同且 size 接近的实例。"""
    try:
        import fitz as _f
    except ImportError:
        import pymupdf as _f
    doc = _f.open(path)
    want = text.replace(" ", "").strip()
    out = {}
    page_list = pages if pages is not None else range(doc.page_count)
    for pno in page_list:
        if pno >= doc.page_count:
            continue
        hits = []
        for b in doc[pno].get_text("rawdict")["blocks"]:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                # 同 span 拼接（去空格，与采集端一致）
                segs = []
                for sp in line["spans"]:
                    t = "".join(ch.get("c", "") for ch in sp.get("chars", []))
                    if t == "":
                        continue
                    sz = round(sp.get("size", 0) or 0, 1)
                    col = sp.get("color")
                    if segs and segs[-1][0] == sz and segs[-1][2] == col:
                        segs[-1][1] += t
                    else:
                        segs.append([sz, t, col, sp.get("bbox") or (0,0,0,0)])
                for sz, t, col, bb in segs:
                    t2 = t.replace(" ", "").strip()
                    if t2 != want:
                        continue
                    if size and abs(sz - size) > max(2.0, size * 0.25):
                        continue
                    hits.append((bb[0], bb[1], bb[2], bb[3]))
        if hits:
            out[pno] = hits
    doc.close()
    return out


def process_page_geo(pdf, page, geo_targets) -> int:
    """按几何签名删除页面上匹配的水印文本块（含 Form 容器），返回删除块数。
    geo_targets 使用 fitz 坐标（y 向下）；这里按页高转换到 PDF 坐标（y 向上）。"""
    if not geo_targets:
        return 0
    try:
        mb = page.mediabox
        H = float(mb[3]) - float(mb[1])
    except Exception:
        H = 842.0
    targets = [(tsz, -trot, tox, H - toy) for tsz, trot, tox, toy in geo_targets]
    total = 0
    contents = page.Contents
    streams = []
    if contents is not None:
        if isinstance(contents, pikepdf.Array):
            streams = [s for s in contents if s is not None]
        else:
            streams = [contents]
        for s in streams:
            data = s.read_bytes()
            new_data, removed = strip_watermark_blocks_geo(data, targets)
            total += removed
            if removed:
                if new_data is None:
                    continue
                s.write(new_data)
    # Form 容器：扫描页面流里各 Form 的绘制 CTM，把 Form 本地坐标变换到页面坐标
    placements = {}
    for s in streams:
        try:
            for nm, ctms in _scan_do_ctms(s.read_bytes()).items():
                placements.setdefault(nm, []).extend(ctms)
        except Exception:
            pass
    res = page.get("/Resources")
    if res is not None:
        xo = res.get("/XObject")
        if xo is not None:
            for name, obj in xo.items():
                if obj is None:
                    continue
                try:
                    sub = obj.get("/Subtype")
                except Exception:
                    continue
                if sub == pikepdf.Name("/Form"):
                    ctms = placements.get(name.lstrip('/')) or [None]
                    for ctm in ctms:
                        total += _process_form_geo(obj, ctm or [1, 0, 0, 1, 0, 0], targets)
    return total


# ---------------------------------------------------------------- 主流程
def process_page(pdf, page, keywords: list) -> int:
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
            new_data, removed = strip_watermark_stream(data, keywords)
            total += removed
            if new_data is None:
                continue  # 整个流被删
            if removed:
                s.write(new_data)
            new_arr.append(s)
        if len(new_arr) == 0:
            page.Contents = pikepdf.Stream(pdf, b"")  # 页面没有内容了：给空流避免损坏
        else:
            page.Contents = new_arr
    else:
        data = contents.read_bytes()
        new_data, removed = strip_watermark_stream(data, keywords)
        total += removed
        if new_data is None:
            page.Contents = pikepdf.Stream(pdf, b"")
        elif removed:
            contents.write(new_data)
    return total


def process_xobjects(resources, keywords: list) -> int:
    """递归处理 Resources/XObject 中的 Form 流（水印可能嵌在 XObject 里）。"""
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
            new_data, removed = strip_watermark_stream(data, keywords)
            total += removed
            if removed:
                if new_data is None:
                    del xo[name]
                else:
                    obj.write(new_data)
            if "/Resources" in obj:
                total += process_xobjects(obj["/Resources"], keywords)
    return total


def _parse_do_names(data: bytes) -> list:
    """字符串感知地扫描内容流中的 /Name Do 操作符，返回名字列表（str）。"""
    names = []
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c == 0x28:  # ( 字符串
            depth = 1; i += 1
            while i < n and depth:
                ch = data[i]
                if ch == 0x5C:
                    i += 2; continue
                if ch == 0x28: depth += 1
                elif ch == 0x29: depth -= 1
                i += 1
            continue
        if c == 0x3C:  # < 十六进制/字典
            if i + 1 < n and data[i + 1] == 0x3C:
                j = data.find(b'>>', i + 2); i = n if j < 0 else j + 2
            else:
                j = data.find(b'>', i + 1); i = n if j < 0 else j + 1
            continue
        if c == 0x2F:  # / 名字
            j = i + 1
            while j < n and data[j] not in b" \t\r\n()<>[]{}/%":
                j += 1
            nm = data[i + 1:j]
            k = j
            while k < n and data[k] in b" \t\r\n":
                k += 1
            if data[k:k + 2] == b"Do" and (k + 2 >= n or data[k + 2] in b" \t\r\n"):
                try:
                    names.append(nm.decode("latin-1"))
                except Exception:
                    pass
                i = k + 2
                continue
            i = j
            continue
        i += 1
    return names


def _resolve_xo_name(res, name):
    """在资源树里按名字找 XObject；找不到返回 None。"""
    if res is None:
        return None
    xo = res.get('/XObject')
    if xo is None:
        return None
    if not name.startswith('/'):
        name = '/' + name
    return xo.get(name)


def _leaf_image_refs(stream_data, res, seen_forms):
    """解析流中所有 Do 引用的叶子图片 objgen（Form 递归展开）。
    返回 (图片 objgen 集合, 直接引用的 Form objgen 集合)。"""
    imgs, forms = set(), set()
    for nm in _parse_do_names(stream_data):
        obj = _resolve_xo_name(res, nm)
        if obj is None:
            continue
        try:
            sub = obj.get('/Subtype')
        except Exception:
            continue
        if sub == pikepdf.Name('/Image'):
            imgs.add(obj.objgen)
        elif sub == pikepdf.Name('/Form'):
            g = obj.objgen
            if g in forms or g in seen_forms:
                continue
            forms.add(g); seen_forms.add(g)
            try:
                fdata = obj.read_bytes()
            except Exception:
                fdata = b''
            ii, _ = _leaf_image_refs(fdata, obj.get('/Resources'), seen_forms)
            imgs |= ii
    return imgs, forms


def find_repeated_images(pdf, min_ratio=0.5):
    """按『内容流实际 Do 绘制』统计重复图片水印。
    返回 (候选 objgen 集合, 统计)。
    候选 = 被 >= max(2, 页数*ratio) 页实际绘制的图片，以及
    只包装候选图片的 Form 容器自身。"""
    n = len(pdf.pages)
    img_cnt, form_cnt = {}, {}
    for page in pdf.pages:
        res = page.get('/Resources')
        imgs, forms = set(), set()
        contents = page.Contents
        if contents is not None:
            streams = contents if isinstance(contents, pikepdf.Array) else [contents]
            for s in streams:
                if s is None:
                    continue
                try:
                    data = s.read_bytes()
                except Exception:
                    data = b''
                ii, ff = _leaf_image_refs(data, res, set())
                imgs |= ii; forms |= ff
        for g in imgs:
            img_cnt[g] = img_cnt.get(g, 0) + 1
        for g in forms:
            form_cnt[g] = form_cnt.get(g, 0) + 1
    th = max(2, int(n * min_ratio))
    cand_imgs = {g for g, c in img_cnt.items() if c >= th}
    cand = set(cand_imgs)
    for g, c in form_cnt.items():
        if c < th:
            continue
        try:
            obj = pdf.get_object(g)
            fdata = obj.read_bytes()
            ii, _ = _leaf_image_refs(fdata, obj.get('/Resources'), {g})
        except Exception:
            ii = set()
        if ii and ii <= cand_imgs:
            cand.add(g)
    return cand, (img_cnt, form_cnt)


def detect_image_watermarks(path, pdf, min_ratio=0.5, min_area_ratio=0.08):
    """完整图片水印检测：内容流 Do 计数 + 渲染面积过滤 + 包装 Form。
    只有被多数页实际绘制、且渲染面积 >= min_area_ratio 的图片才算水印
    （避免把每页都出现的页眉/Logo 小图误删）。返回 (候选 objgen, 统计)。"""
    try:
        import pymupdf as _mupdf
    except ImportError:
        import fitz as _mupdf
    cand, stats = find_repeated_images(pdf, min_ratio)
    if not cand:
        return set(), stats
    img_cnt, form_cnt = stats
    doc = _mupdf.open(path)
    keep = set()
    for g in cand:
        try:
            obj = pdf.get_object(g)
            if obj.get("/Subtype") == pikepdf.Name("/Form"):
                continue  # 包装 Form 稍后单独判断
        except Exception:
            pass
        maxr = 0.0
        try:
            for pno in range(doc.page_count):
                pg = doc[pno]
                for r in pg.get_image_rects(g[0]):
                    area = r.width * r.height / (pg.rect.width * pg.rect.height)
                    if area > maxr:
                        maxr = area
        except Exception:
            pass
        if maxr >= min_area_ratio:
            keep.add(g)
    doc.close()
    # 纯包装 Form：其叶子图片全部命中候选才保留
    th = max(2, int(len(pdf.pages) * min_ratio))
    for g, c in form_cnt.items():
        if c < th:
            continue
        try:
            obj = pdf.get_object(g)
            fdata = obj.read_bytes()
            ii, _ = _leaf_image_refs(fdata, obj.get("/Resources"), {g})
        except Exception:
            ii = set()
        if ii and ii <= keep:
            keep.add(g)
    return keep, stats


def _names_to_remove(res, cand, seen_forms):
    """返回资源树中需移除的 XObject 名字。
    规则：图片 objgen 命中候选；或 Form 容器（无论自身 objgen 是否候选）
    只要其内部图片非空且全部命中候选 → 整容器删除（兼容容器包装的水印）。"""
    names = set()
    if res is None:
        return names
    xo = res.get('/XObject')
    if xo is None:
        return names
    for name, obj in xo.items():
        if obj is None:
            continue
        try:
            sub = obj.get('/Subtype')
        except Exception:
            continue
        if sub == pikepdf.Name('/Image') and obj.objgen in cand:
            names.add(name)
        elif sub == pikepdf.Name('/Form'):
            g = obj.objgen
            if g not in seen_forms:
                seen_forms.add(g)
                try:
                    inner, _ = _leaf_image_refs(obj.read_bytes(),
                                                obj.get('/Resources'), {g})
                except Exception:
                    inner = set()
                if obj.objgen in cand or (inner and inner <= cand):
                    names.add(name)
                names |= _names_to_remove(obj.get('/Resources'), cand, seen_forms)
    return names


def remove_image_watermarks(pdf, cand) -> int:
    """删除各页绘制候选图片/包装 Form 的 /name Do 操作与资源项。
    返回删除的 Do 操作数。"""
    total = 0
    for page in pdf.pages:
        res = page.get('/Resources')
        names = _names_to_remove(res, cand, set())
        if not names:
            continue
        contents = page.Contents
        if contents is not None:
            streams = contents if isinstance(contents, pikepdf.Array) else [contents]
            keep = pikepdf.Array()
            for s in streams:
                if s is None:
                    keep.append(s)
                    continue
                data = s.read_bytes()
                new_data = data
                for nm in names:
                    nm_b = nm.lstrip('/').encode('latin-1')
                    pat = re.compile(rb'/' + re.escape(nm_b) +
                                     rb'(?![A-Za-z0-9])[ \t\r\n]*Do\b')
                    new_data, k = pat.subn(b'', new_data)
                    total += k
                if new_data != data:
                    s.write(new_data)
                keep.append(s)
            if isinstance(contents, pikepdf.Array):
                page.Contents = keep
        if res is not None:
            xo = res.get('/XObject')
            if xo is not None:
                for nm in names:
                    if nm in xo:
                        del xo[nm]
    return total


def find_image_objgens(pdf, xrefs):
    """按 xref 号过滤全部图片 objgen（手动指定模式）。"""
    all_imgs = set()
    for page in pdf.pages:
        res = page.get('/Resources')
        contents = page.Contents
        if contents is None:
            continue
        streams = contents if isinstance(contents, pikepdf.Array) else [contents]
        for s in streams:
            if s is None:
                continue
            try:
                ii, _ = _leaf_image_refs(s.read_bytes(), res, set())
                all_imgs |= ii
            except Exception:
                pass
    return {g for g in all_imgs if g[0] in xrefs}


def clean_pdf(src: str, dst: str, keywords: list, password: str = "") -> dict:
    """打开 src，删除命中关键词的文本块，以 dst 无加密保存。
    返回 {'page_count': int, 'removed': int}。"""
    pdf = pikepdf.open(src, password=password)
    info = {"page_count": len(pdf.pages), "encryption": pdf.is_encrypted, "removed": 0}
    with pdf:
        for page in pdf.pages:
            info["removed"] += process_page(pdf, page, keywords)
            info["removed"] += process_xobjects(page.get("/Resources"), keywords)
        pdf.save(dst, encryption=False)
    return info


def verify_residual(path: str, keywords: list) -> dict:
    """用 PyMuPDF 检查输出中的水印残留（关键词命中页数）。"""
    out = {"pages": 0, "residual_pages": 0, "encrypted": None}
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            return out
    doc = pymupdf.open(path)
    out["pages"] = doc.page_count
    out["encrypted"] = bool(doc.is_encrypted)
    for i in range(doc.page_count):
        t = doc[i].get_text()
        if any(kw.lower() in t.lower() for kw in keywords):
            out["residual_pages"] += 1
    doc.close()
    return out


def main():
    ap = argparse.ArgumentParser(description="PDF 去水印 + 去权限工具")
    ap.add_argument("input", help="输入 PDF")
    ap.add_argument("-o", "--output", default=None, help="输出 PDF（默认 <原名>_cleaned.pdf）")
    ap.add_argument("-k", "--keyword", action="append", default=[],
                    help="水印关键词，可多次指定（子串匹配）")
    ap.add_argument("--no-images", action="store_true",
                    help="关闭自动图片水印检测（默认开启：自动检测多数页绘制的大面积重复图片，页眉/Logo 小图不误删）")
    ap.add_argument("--image-xref", action="append", type=int, default=[],
                    help="手动指定要移除的水印图片 xref（可多次）")
    ap.add_argument("--password", default="", help="打开加密 PDF 的密码（默认空）")
    ap.add_argument("--no-verify", action="store_true", help="关闭输出后的残留检查")
    args = ap.parse_args()

    if not args.keyword and not args.image_xref and args.no_images:
        print("错误：需要 -k 关键词，或 --image-xref 图片水印（--no-images 已关闭自动检测）", file=sys.stderr)
        sys.exit(2)

    out_path = args.output or re.sub(r"\.pdf$", "", args.input, flags=re.I) + "_cleaned.pdf"
    keywords = [k.encode("utf-8", "replace") for k in args.keyword]

    pdf = pikepdf.open(args.input, password=args.password)
    print(f"打开: {args.input}")
    print(f"  页数: {len(pdf.pages)}")
    if pdf.is_encrypted:
        e = pdf.encryption
        print(f"  加密: R={e.R} V={e.V} P={e.P}"
              f" user_password={'<空>' if not e.user_password else '***'}")
    else:
        print("  加密: 无")

    per_page = Counter()
    total = 0
    for i, page in enumerate(pdf.pages):
        n = process_page(pdf, page, keywords)
        n += process_xobjects(page.get("/Resources"), keywords)
        per_page[i] = n
        total += n

    print(f"删除水印文本块: {total} 个")

    # ---- 图片水印 ----
    img_cands = set()
    if args.image_xref:
        img_cands |= find_image_objgens(pdf, set(args.image_xref))
        print(f"手动指定图片 xref: {sorted(set(args.image_xref))}")
    if not args.no_images:
        auto, cnt = detect_image_watermarks(args.input, pdf, 0.5, 0.08)
        img_cands |= auto
        if auto:
            shown = {f"xref{g[0]}": c for g, c in cnt[0].items() if g in auto}
            print(f"自动检测重复图片水印(引用页数): {shown}")
        else:
            print("自动检测：未发现重复的大面积图片水印（可用 --image-xref 手动指定）。")
    img_removed = 0
    if img_cands:
        img_removed = remove_image_watermarks(pdf, img_cands)
        print(f"删除图片水印 Do 操作: {img_removed} 个 "
              f"(图片 xref: {sorted(g[0] for g in img_cands)})")

    if total == 0 and img_removed == 0:
        print("警告：文本与图片均未匹配到水印，请检查关键词/图片或水印形式（矢量描边）。")

    pdf.save(out_path, encryption=False)
    pdf.close()
    print(f"\n输出: {out_path}  (已写入，加密字典/权限位已移除)")

    if not args.no_verify:
        v = verify_residual(out_path, [k.decode("utf-8", "replace") for k in keywords])
        if v["pages"]:
            print(f"验证: 页数={v['pages']} 加密={v['encrypted']} "
                  f"水印残留页数={v['residual_pages']}")
            if v["residual_pages"]:
                print("警告: 仍有水印关键词残留！请检查关键词或水印形式（矢量/图片）。")
                sys.exit(1)
            print("验证通过：无残留。")
        else:
            print("验证: 未安装 PyMuPDF，跳过残留检查（pip install pymupdf 可启用）")


if __name__ == "__main__":
    main()
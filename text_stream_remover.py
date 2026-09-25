"""内容流级文本水印精确删除（CID 感知，绝不使用 bbox / 红化）。

设计原则
--------
1. 只删除内容流里**文本绘制操作符**（`Tj` / `TJ`），且要求：
   - 解码文本与用户确认的候选文本（去空格）匹配；
   - 该操作符的**有效字号**与候选字号一致（容差 max(0.6, 5%)）；
   - 旋转角与候选旋转角一致（候选角为 0 且实测有旋转时，改为要求文本完全相等）。
   三者同时满足才删，其余内容一字节不动。
2. 绝不使用 `add_redact_annot` / `apply_redactions`：旋转文本的轴对齐 bbox 会覆盖
   整页（实测 43%），红化会把框内正文一起删掉。
3. 支持两类编码：
   - 明文串 `(C2 - Confidential ...)Tj`（按字体 ToUnicode / 直接字节解码）
   - 十六进制 CID `<0ECB...>Tj` / `[...]TJ`（2 字节 CID → ToUnicode CMap）
4. 解码不出来（字体无 ToUnicode 且非 ASCII）时**跳过并报告**，绝不猜测。

字号/角度为什么要校验：正文里同样存在 `-` `.` `1` 等单字符，字号 9.9；
而水印 `-` 字号 22。不做字号校验会把正文里的同类字符一起删掉。
"""
import math
import re

try:
    import pikepdf
except Exception:  # pragma: no cover
    pikepdf = None


# --------------------------------------------------------------------------- #
# 基础解码
# --------------------------------------------------------------------------- #
_ESC_MAP = {ord('n'): 10, ord('r'): 13, ord('t'): 9, ord('b'): 8, ord('f'): 12,
            ord('('): 40, ord(')'): 41, ord('\\'): 92}

_DELIM = b' \t\r\n()<>[]{}/%'


def decode_pdf_literal(raw: bytes) -> bytes:
    """把 PDF 字面串 `(...)` 的内容解码为原始字节（处理转义与八进制）。"""
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == 0x5C and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in _ESC_MAP:
                out.append(_ESC_MAP[nxt])
                i += 2
                continue
            if 0x30 <= nxt <= 0x37:
                j = i + 1
                oct_s = bytearray()
                while j < n and len(oct_s) < 3 and 0x30 <= raw[j] <= 0x37:
                    oct_s.append(raw[j])
                    j += 1
                try:
                    out.append(int(bytes(oct_s), 8) & 0xFF)
                except Exception:
                    pass
                i = j
                continue
            if nxt in (0x0A, 0x0D):
                i += 2
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return bytes(out)


def parse_tounicode(data: bytes) -> dict:
    """解析 ToUnicode CMap → {code:int -> str}。支持 bfchar / bfrange。"""
    cmap = {}
    if not data:
        return cmap
    for m in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", m.group(1)):
            try:
                src = int(mm.group(1), 16)
                dst_hex = mm.group(2).decode('ascii')
                if len(dst_hex) % 4:
                    dst_hex = dst_hex.ljust((len(dst_hex) + 3) // 4 * 4, '0')
                cmap[src] = bytes.fromhex(dst_hex).decode('utf-16-be', 'ignore')
            except Exception:
                continue
    for m in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        body = m.group(1)
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", body):
            try:
                lo = int(mm.group(1), 16)
                hi = int(mm.group(2), 16)
                dst = int(mm.group(3), 16)
            except Exception:
                continue
            if hi < lo or hi - lo > 65535:
                continue
            for off, code in enumerate(range(lo, hi + 1)):
                v = dst + off
                try:
                    cmap[code] = bytes([(v >> 8) & 0xFF, v & 0xFF]).decode('utf-16-be', 'ignore')
                except Exception:
                    pass
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", body, re.S):
            try:
                lo = int(mm.group(1), 16)
            except Exception:
                continue
            items = re.findall(rb"<([0-9A-Fa-f]*)>", mm.group(3))
            for off, item in enumerate(items):
                try:
                    h = item.decode('ascii')
                    if len(h) % 4:
                        h = h.ljust((len(h) + 3) // 4 * 4, '0')
                    cmap[lo + off] = bytes.fromhex(h).decode('utf-16-be', 'ignore')
                except Exception:
                    continue
    return cmap


def _font_maps(pdf, page) -> dict:
    """页面级字体映射（兼容旧签名）。"""
    try:
        return _font_maps_res(page.get('/Resources'))
    except Exception:
        return {}


def _font_maps_res(res) -> dict:
    """从任意一层的 /Resources 解析字体映射 → {资源名(不含/) : {'cmap': {...}, 'two_byte': bool}}。
    重要：Form XObject 有自己的 /Resources/Font，必须用它自己的映射来解码该 Form 流，
    否则 CID 会解错（跨作用域共用页面字体表是错的）。"""
    out = {}
    try:
        if res is None:
            return out
        fonts = res.get('/Font')
        if fonts is None:
            return out
        font_items = list(fonts.items())
    except Exception:
        return out
    for name, fobj in font_items:
        key = str(name).lstrip('/')
        info = {'cmap': {}, 'two_byte': False}
        try:
            sub = str(fobj.get('/Subtype') or '')
            info['two_byte'] = ('Type0' in sub)
            targets = [fobj]
            try:
                desc = fobj.get('/DescendantFonts')
                if desc is not None:
                    targets = list(desc) + targets
            except Exception:
                pass
            for t in targets:
                try:
                    tu = t.get('/ToUnicode')
                except Exception:
                    tu = None
                if tu is None:
                    continue
                try:
                    info['cmap'] = parse_tounicode(tu.read_bytes())
                except Exception:
                    info['cmap'] = {}
                if info['cmap']:
                    break
        except Exception:
            pass
        out[key] = info
    return out


# --------------------------------------------------------------------------- #
# 矩阵与内容流扫描
# --------------------------------------------------------------------------- #
def _mm(a, b):
    a1, b1, c1, d1, e1, f1 = a
    a2, b2, c2, d2, e2, f2 = b
    return [a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2]


def _ang_diff(a, b):
    d = (a - b) % 360.0
    return min(d, 360.0 - d)


def iter_text_draw_ops(data: bytes):
    """扫描内容流，产出 (start, end, operand, font, eff_size, rot)。

    start/end 覆盖「操作数 + Tj/TJ 操作符」，删除该区间后语法依然合法。
    eff_size / rot 由 CTM(q/Q/cm) × Tm(Tm/Td/TD) × 字体字号换算得到。
    """
    ops = []
    i, n = 0, len(data)
    block_id = 0
    ctm = [1, 0, 0, 1, 0, 0]
    ctm_stack = []
    tm = [1, 0, 0, 1, 0, 0]
    font_size = 0.0
    cur_font = None
    last_name = None
    nums = []
    last_operand = None

    while i < n:
        c = data[i]
        if c in b' \t\r\n':
            i += 1
            continue
        if c == 0x25:  # 注释
            j = data.find(b'\n', i)
            i = n if j < 0 else j + 1
            continue
        if c == 0x28:  # 字面串
            j = i + 1
            depth = 1
            esc = False
            while j < n:
                ch = data[j]
                if esc:
                    esc = False
                    j += 1
                    continue
                if ch == 0x5C:
                    esc = True
                    j += 1
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            last_operand = (i, j + 1, 'lit', data[i + 1:j])
            i = j + 1
            continue
        if c == 0x3C:
            if i + 1 < n and data[i + 1] == 0x3C:      # << 字典
                k = data.find(b'>>', i + 2)
                i = n if k < 0 else k + 2
                continue
            j = data.find(b'>', i + 1)                 # hex 串
            if j < 0:
                break
            last_operand = (i, j + 1, 'hex', re.sub(rb'\s', b'', data[i + 1:j]))
            i = j + 1
            continue
        if c == 0x5B:  # [ ... ] 数组
            j = i + 1
            parts = []
            while j < n:
                ch = data[j]
                if ch == 0x28:
                    k = j + 1
                    d2 = 1
                    esc2 = False
                    while k < n:
                        c2 = data[k]
                        if esc2:
                            esc2 = False
                            k += 1
                            continue
                        if c2 == 0x5C:
                            esc2 = True
                            k += 1
                            continue
                        if c2 == 0x28:
                            d2 += 1
                        elif c2 == 0x29:
                            d2 -= 1
                            if d2 == 0:
                                break
                        k += 1
                    parts.append(('lit', data[j + 1:k]))
                    j = k + 1
                    continue
                if ch == 0x3C and not (j + 1 < n and data[j + 1] == 0x3C):
                    k = data.find(b'>', j + 1)
                    if k < 0:
                        break
                    parts.append(('hex', re.sub(rb'\s', b'', data[j + 1:k])))
                    j = k + 1
                    continue
                if ch == 0x5D:
                    j += 1
                    break
                j += 1
            last_operand = (i, j, 'arr', parts)
            i = j
            continue
        if c == 0x2F:  # /名字
            j = i + 1
            while j < n and data[j] not in _DELIM:
                j += 1
            last_name = data[i + 1:j].decode('latin-1')
            i = j
            continue
        if c in b'+-.0123456789':
            j = i + 1
            while j < n and data[j] in b'+-.0123456789eE':
                j += 1
            try:
                nums.append(float(data[i:j]))
            except Exception:
                pass
            i = j
            continue

        j = i + 1
        while j < n and data[j] not in _DELIM:
            j += 1
        tok = data[i:j]
        if tok == b'q':
            ctm_stack.append(list(ctm))
            nums = []
        elif tok == b'Q':
            if ctm_stack:
                ctm = ctm_stack.pop()
            nums = []
        elif tok == b'cm':
            if len(nums) >= 6:
                ctm = _mm(ctm, nums[-6:])
            nums = []
        elif tok == b'BT':
            tm = [1, 0, 0, 1, 0, 0]
            block_id += 1          # 新的文本块：分组时不得跨块
            nums = []
        elif tok == b'Tm':
            if len(nums) >= 6:
                tm = list(nums[-6:])
            nums = []
        elif tok in (b'Td', b'TD'):
            if len(nums) >= 2:
                tm = _mm(tm, [1, 0, 0, 1, nums[-2], nums[-1]])
            nums = []
        elif tok == b'Tf':
            if nums:
                font_size = nums[-1]
            cur_font = last_name
            nums = []
        elif tok in (b'Tj', b'TJ'):
            if last_operand is not None:
                M = _mm(ctm, tm)
                eff = font_size * math.hypot(M[0], M[1])
                rot = math.degrees(math.atan2(M[1], M[0]))
                # 文本原点 (x, y) 与 BT 块号：用于把"逐字符绘制"的操作符合并成行组
                ops.append((last_operand[0], j, last_operand, cur_font, eff, rot,
                            M[4], M[5], block_id))
            last_operand = None
            nums = []
        else:
            nums = []
        i = j
    return ops


def decode_draw_op(op, font_info: dict) -> str:
    """把一条绘制操作符解码为文本（解码不出来返回空串）。"""
    operand = op[2]
    font_name = op[3]
    finfo = font_info.get(font_name) or {}
    cmap = finfo.get('cmap') or {}
    two_byte = bool(finfo.get('two_byte'))

    def map_codes(b: bytes) -> str:
        if not b:
            return ''
        if two_byte:
            codes = [int.from_bytes(b[k:k + 2], 'big') for k in range(0, len(b) - 1, 2)]
        else:
            codes = list(b)
        if cmap:
            return ''.join(cmap.get(c, '') for c in codes)
        return ''

    def bytes_of(kind, payload):
        if kind == 'lit':
            return decode_pdf_literal(payload)
        try:
            return bytes.fromhex(payload.decode('ascii'))
        except Exception:
            return b''

    kind = operand[2]
    payload = operand[3]
    if kind == 'arr':
        out = []
        for pk, pd in payload:
            b = bytes_of(pk, pd)
            s = map_codes(b)
            if not s and not two_byte:
                try:
                    s = b.decode('utf-8')
                except Exception:
                    s = b.decode('latin-1')
            out.append(s)
        return ''.join(out)
    b = bytes_of(kind, payload)
    s = map_codes(b)
    if not s and not two_byte:
        try:
            s = b.decode('utf-8')
        except Exception:
            s = b.decode('latin-1')
    return s


# --------------------------------------------------------------------------- #
# 匹配安全阀
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


# 水印候选里常出现「：」「元」「-」这类 1 字符候选；若允许子串包含匹配，
# 会命中正文里的同类字符并把整段正文操作符删掉（实测 153 条）。
#   - 完全相等：长度 >= 1（是否删除还要过字号/角度校验）；
#   - 子串包含：候选长度 >= 8 且候选占被匹配文本一半以上。
_MIN_EQ_LEN = 1
_MIN_CONTAIN_LEN = 8
_MIN_CONTAIN_RATIO = 0.5


def _is_match(decoded_norm: str, cand_norm: str) -> bool:
    if not decoded_norm or not cand_norm:
        return False
    if decoded_norm == cand_norm:
        return len(cand_norm) >= _MIN_EQ_LEN
    if len(cand_norm) >= _MIN_CONTAIN_LEN and cand_norm in decoded_norm:
        return len(cand_norm) >= _MIN_CONTAIN_RATIO * len(decoded_norm)
    return False


def _size_ok(eff_size: float, cand_size: float) -> bool:
    try:
        cs = float(cand_size or 0.0)
    except Exception:
        cs = 0.0
    if cs <= 0:
        return True
    return abs(float(eff_size) - cs) <= max(0.6, cs * 0.05)


def _rot_ok(rot: float, cand_rot: float, exact_text: bool) -> bool:
    """角度校验（符号无关）。

    我的扫描器在 PDF 坐标（y 向上）里算旋转；PyMuPDF/分析端的候选角度在
    屏幕坐标（y 向下）里，两者符号相反（实测 PDF +52° ↔ 分析 -52°）。
    因此比较时取 ±rot 中较近的一侧。
    """
    try:
        cr = float(cand_rot or 0.0)
    except Exception:
        cr = 0.0
    if abs(cr) > 1.0:
        return min(_ang_diff(rot, cr), _ang_diff(-rot, cr)) <= 6.0
    # 候选角为 0（单字符候选常常没记录到角度）：若实测也近似 0 则通过；
    # 若实测带旋转，则要求文本完全相等才允许删除（更严格）。
    if min(abs(_ang_diff(rot, 0.0)), abs(_ang_diff(-rot, 0.0))) <= 6.0:
        return True
    return bool(exact_text)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 子串级裁剪：候选是更长文本串的一段时，只删覆盖到的那几个字符单元
# --------------------------------------------------------------------------- #
def _hex_bytes(payload: bytes) -> bytes:
    h = re.sub(rb"\s", b"", payload or b"")
    if len(h) % 2:
        h += b"0"
    try:
        return bytes.fromhex(h.decode("ascii"))
    except Exception:
        return b""


def _map_bytes(b: bytes, cmap: dict, two_byte: bool):
    """字节 -> [(char, unit_index)]；单元 = 2 字节码(Type0) 或 1 字节"""
    mapping = []
    if two_byte:
        for u, i in enumerate(range(0, len(b) - 1, 2)):
            code = int.from_bytes(b[i:i + 2], "big")
            ch = cmap.get(code, "") if cmap else ""
            for c in ch:
                mapping.append((c, u))
        return mapping, len(b) // 2
    for u, byte in enumerate(b):
        ch = cmap.get(byte, "") if cmap else ""
        if not ch:
            try:
                ch = bytes([byte]).decode("utf-8")
            except Exception:
                ch = bytes([byte]).decode("latin-1")
        for c in ch:
            mapping.append((c, u))
    return mapping, len(b)


def op_units(op, font_info):
    """返回 (mapping, per_payload_units)
    mapping: [(char, payload_idx, unit_idx)]，只含能定位到单元的字符。"""
    operand = op[2]
    finfo = font_info.get(op[3]) or {}
    cmap = finfo.get("cmap") or {}
    two_byte = bool(finfo.get("two_byte"))
    kind, payload = operand[2], operand[3]
    mapping = []
    units = []
    if kind == "arr":
        for pi, (pk, pd) in enumerate(payload):
            b = decode_pdf_literal(pd) if pk == "lit" else _hex_bytes(pd)
            m, n = _map_bytes(b, cmap, two_byte)
            for c, u in m:
                mapping.append((c, pi, u))
            units.append(n)
        return mapping, units
    b = decode_pdf_literal(payload) if kind == "lit" else _hex_bytes(payload)
    m, n = _map_bytes(b, cmap, two_byte)
    for c, u in m:
        mapping.append((c, 0, u))
    return mapping, [n]


def string_spans(buf: bytes, start: int, end: int):
    """在 [start, end) 内按出现顺序找出字符串 token 的内容区间 -> [(起, 止, kind)]"""
    spans = []
    i = start
    while i < end:
        c = buf[i]
        if c == 0x28:  # (
            j = i + 1
            depth = 1
            esc = False
            while j < end:
                ch = buf[j]
                if esc:
                    esc = False
                    j += 1
                    continue
                if ch == 0x5C:
                    esc = True
                    j += 1
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            spans.append((i + 1, j, "lit"))
            i = j + 1
            continue
        if c == 0x3C and not (i + 1 < end and buf[i + 1] == 0x3C):
            j = buf.find(b">", i + 1, end)
            if j < 0:
                break
            spans.append((i + 1, j, "hex"))
            i = j + 1
            continue
        i += 1
    return spans


def _escape_literal(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([b])
        elif b < 32 or b > 126:
            out += ("\\%03o" % b).encode("ascii")
        else:
            out.append(b)
    return bytes(out)


def trim_payload(buf: bytearray, op, payload_idx: int, u0: int, u1: int, two_byte: bool) -> bool:
    """把 op 内第 payload_idx 个字符串的单元区间 [u0, u1) 就地裁掉（只动这一段字节）"""
    if u1 <= u0:
        return False
    spans = string_spans(buf, op[0], op[1])
    if payload_idx >= len(spans):
        return False
    cs, ce, kind = spans[payload_idx]
    unit = 2 if two_byte else 1
    if kind == "hex":
        raw = bytes(buf[cs:ce])
        h = re.sub(rb"\s", b"", raw)
        cut0 = min(len(h), u0 * 2)
        cut1 = min(len(h), u1 * 2)
        new = b"<" + h[:cut0] + h[cut1:] + b">"
        buf[cs - 1:ce + 1] = new
        return True
    data = decode_pdf_literal(bytes(buf[cs:ce]))
    b0 = min(len(data), u0 * unit)
    b1 = min(len(data), u1 * unit)
    data = data[:b0] + data[b1:]
    new = b"(" + _escape_literal(data) + b")"
    buf[cs - 1:ce + 1] = new
    return True



def _find_cuts(buf, font_info, uniq):
    """在一段内容流里找出应删除的文本操作符区间。
    返回 [(start, end, 候选原文, 命中文本长度), ...]

    匹配策略（按优先级）：
      (a) 连续子序列精确相等：同一 BT 块内、同字体/字号/角度，把连续操作符的
          解码文本拼起来，若与某候选**完全相等**则删这几条。
          解决"一个字符一个操作符"的逐字符绘制（实测 57 页 × 6 个操作符的水印）。
          不依赖位置判据 —— 某些 PDF 的 Tm/Td 计算原点与视觉位置不成比例
          （实测同字号 7.45pt 的字符计算间距达 149pt），按位置分组会切碎。
      (b) 单条匹配：整行一个操作符时按 相等 / 长候选包含 规则。
    三重校验（文本 + 有效字号 + 旋转角）始终生效，避免误删正文。
    """
    try:
        ops = iter_text_draw_ops(bytes(buf))
    except Exception:
        return [], {}
    if not ops:
        return [], {}

    decoded = []
    for op in ops:
        try:
            decoded.append(_norm(decode_draw_op(op, font_info)))
        except Exception:
            decoded.append('')

    cuts = []

    def _cut_group(indices, text):
        for n_cand, rc, c_size, c_rot in uniq:
            if not _is_match(text, n_cand):
                continue
            if not _size_ok(ops[indices[0]][4], c_size):
                continue
            if not _rot_ok(ops[indices[0]][5], c_rot, text == n_cand):
                continue
            for oi in indices:
                cuts.append((ops[oi][0], ops[oi][1], rc, len(text), None))
                cand_ops.setdefault(rc, set()).add(ops[oi][0])
            return True
        return False

    cands_norm = dict((n, (rc, cs, cr)) for n, rc, cs, cr in uniq)
    cand_ops = {}       # {候选原文: set(操作符起始偏移)} —— 用于命中口径
    maxlen = max((len(k) for k in cands_norm), default=0)
    nops = len(ops)
    i = 0
    while i < nops:
        # (a) 连续子序列精确相等
        acc = ''
        k2 = i
        matched = False
        base = ops[i]
        while k2 < nops and len(acc) <= maxlen:
            q = ops[k2]
            # 拼接条件（实测必需，否则大批候选拼不起来）：
            #   - 允许跨 BT 块：某些 PDF 一个字符一个 BT..ET 块（实测 '答案：【D】' 6 个字
            #     分布在 block 26/27/28/30/32，'NOTE:' 每个字母一个块）
            #   - 允许换字体：水印常是中文 CJK 字体 + 数字/字母拉丁字体混排
            #   - 必须同一行：|Δy| <= max(1.0, 0.6×字号)，避免把不同行的字粘在一起
            # 安全性由"拼接文本与候选完全相等 + 字号/角度校验"保证。
            if not (abs(q[4] - base[4]) <= 0.3
                    and _ang_diff(q[5], base[5]) <= 2.0
                    and abs(q[7] - base[7]) <= max(1.0, 0.6 * (base[4] or 1.0))):
                break
            acc += decoded[k2]
            k2 += 1
            if acc in cands_norm:
                rc, c_size, c_rot = cands_norm[acc]
                if _size_ok(base[4], c_size) and _rot_ok(base[5], c_rot, True):
                    for oi in range(i, k2):
                        cuts.append((ops[oi][0], ops[oi][1], rc, len(acc), None))
                        cand_ops.setdefault(rc, set()).add(ops[oi][0])
                    i = k2
                    matched = True
                    break
        if matched:
            continue
        # (b) 单条匹配
        single = decoded[i]
        if single:
            _cut_group([i], single)
        i += 1

    # (c) 子串级：候选是更长串的一段（被邻居文本合并，例如 '1.' 落在 op 文本 '1.T' 里，
    #     'NOTE:' 由 N/O/T/E + ':Equivalent...' 组成）→ 只裁掉候选覆盖的字符单元，
    #     绝不多删邻接文本。
    def _runs():
        runs = []
        cur = []
        for idx2, op2 in enumerate(ops):
            if not cur:
                cur = [idx2]
                continue
            p2 = ops[cur[-1]]
            if (abs(op2[4] - p2[4]) <= 0.3 and _ang_diff(op2[5], p2[5]) <= 2.0
                    and abs(op2[7] - p2[7]) <= max(1.0, 0.6 * (p2[4] or 1.0))):
                cur.append(idx2)
            else:
                runs.append(cur)
                cur = [idx2]
        if cur:
            runs.append(cur)
        return runs

    for run in _runs():
        seq = []          # [(char, op_idx, payload_idx, unit_idx)]
        for oi in run:
            try:
                m, _units = op_units(ops[oi], font_info)
            except Exception:
                continue
            for c, pi, ui in m:
                seq.append((c, oi, pi, ui))
        if not seq:
            continue
        idxmap = [k for k, item in enumerate(seq) if not item[0].isspace()]
        norm_text = ''.join(seq[k][0] for k in idxmap)
        if not norm_text:
            continue
        for n_cand, (rc, c_size, c_rot) in cands_norm.items():
            if not n_cand:
                continue
            st = norm_text.find(n_cand)
            while st >= 0:
                en2 = st + len(n_cand)
                raw_idx = idxmap[st:en2]
                if len(raw_idx) == len(n_cand):
                    base_op = ops[seq[raw_idx[0]][1]]
                    if _size_ok(base_op[4], c_size) and _rot_ok(base_op[5], c_rot, True):
                        covered = {}
                        for k3 in raw_idx:
                            _c, oi, pi, ui = seq[k3]
                            covered.setdefault(oi, {}).setdefault(pi, set()).add(ui)
                        for oi, per_payload in covered.items():
                            op3 = ops[oi]
                            try:
                                _m3, units3 = op_units(op3, font_info)
                            except Exception:
                                continue
                            pieces = []
                            fully = True
                            for pi, uset in per_payload.items():
                                total = units3[pi] if pi < len(units3) else 0
                                if len(uset) < total:
                                    fully = False
                                u0 = min(uset)
                                u1 = max(uset) + 1
                                pieces.append((pi, u0, u1, len(uset) == (u1 - u0)))
                            cand_ops.setdefault(rc, set()).add(op3[0])
                            two_b = bool((font_info.get(op3[3]) or {}).get('two_byte'))
                            if fully:
                                cuts.append((op3[0], op3[1], rc, len(n_cand), None))
                            else:
                                # 按连续单元区间逐段裁剪（同一条操作符可能同时承载多个候选的文字）
                                for pi, uset in per_payload.items():
                                    us = sorted(uset)
                                    run_start = prev = us[0]
                                    for u in us[1:] + [None]:
                                        if u is not None and u == prev + 1:
                                            prev = u
                                            continue
                                        cuts.append((op3[0], op3[1], rc, len(n_cand),
                                                     (pi, run_start, prev + 1, two_b)))
                                        if u is not None:
                                            run_start = prev = u
                st = norm_text.find(n_cand, st + 1)

    # 去重（并集语义）：同一条操作符上可能同时要删多个候选的文字，
    # 必须保留所有*不同的*裁剪区间；若该操作符有整条删除，则它的裁剪区间可全部丢弃。
    whole_ops = set(c[0] for c in cuts if c[4] is None)
    seen = set()
    out = []
    for c in cuts:
        if c[4] is None:
            pass
        elif c[0] in whole_ops:
            continue                      # 该 op 整条都删，无需再裁剪
        key = (c[0], c[4][0], c[4][1], c[4][2]) if c[4] is not None else (c[0], -1, -1, -1)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out, cand_ops


def remove_candidate_text(pdf, candidates, log=None, dry_run=False):
    """从 pdf 各页内容流与所有 Form 内部流删除与候选匹配的文本操作符。

    覆盖两种曾经的漏删场景：
      1) 文本画在 Form XObject 内部（页面流里看不到）
      2) 逐字符绘制（一个字符一个操作符）

    返回 (removed_ops, hits, unmatched, removed_chars)
    """
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    uniq = []
    seen = set()
    for c in candidates or []:
        raw = str((c or {}).get('text') or '')
        n = _norm(raw)
        if not n or n in seen:
            continue
        seen.add(n)
        uniq.append((n, raw, (c or {}).get('size'), (c or {}).get('rot')))
    if not uniq:
        return 0, {}, [], 0

    hits = {raw: 0 for _, raw, _, _ in uniq}
    removed_ops = 0
    removed_chars = 0
    all_cand_ops = {}       # {候选: set(操作符起点 或 (起点, 流id))}
    applied_ops = set()     # 实际被修改过的操作符

    def _apply_to_stream(stream_obj, cuts, base_off=0):
        """把区间（可选基于拼接缓冲的偏移）落到该流上并删除"""
        nonlocal removed_ops, removed_chars
        if not cuts:
            return
        try:
            data = stream_obj.read_bytes()
        except Exception:
            return
        new = bytearray(data)
        # 从后往前应用，保证偏移不失效。两种动作：
        #   trim=None → 删除整条操作符
        #   trim=(payload_idx,u0,u1,two_byte) → 只裁掉该字符串里的字符单元（候选是长串的一段）
        def _sort_key(c):
            t = c[4]
            return (c[0], (t[0] if t else -1), (t[1] if t else -1))
        for start, end, rc, tlen, trim in sorted(cuts, key=_sort_key, reverse=True):
            st, en = start - base_off, end - base_off
            if trim is None:
                if 0 <= st < en <= len(new):
                    del new[st:en]
                    removed_ops += 1
                    removed_chars += tlen
                    applied_ops.add(start)
            else:
                pi, u0, u1, two_b = trim
                try:
                    ok = trim_payload(new, (st, en), pi, u0, u1, two_b)
                except Exception:
                    ok = False
                if ok:
                    removed_ops += 1
                    removed_chars += tlen
                    applied_ops.add(start)
        if dry_run:
            return
        try:
            stream_obj.write(bytes(new))
        except Exception as e:
            _log(f">>> stream write failed: {e}")

    def walk_forms(res, depth=0, seen=None):
        """递归处理 Form XObject 内部流（用该 Form 自己的 Font 资源解码）"""
        if res is None or depth > 20:
            return
        if seen is None:
            seen = set()
        try:
            xo = res.get('/XObject')
        except Exception:
            xo = None
        if xo is None:
            return
        for name, obj in list(xo.items()):
            try:
                if str(obj.get('/Subtype')) != '/Form':
                    continue
                g = obj.objgen
            except Exception:
                continue
            if g in seen:
                continue
            seen.add(g)
            try:
                fres = obj.get('/Resources')
            except Exception:
                fres = None
            try:
                fbuf = obj.read_bytes()
            except Exception:
                fbuf = b''
            if fbuf:
                finfo = _font_maps_res(fres)
                fcuts, f_cand_ops = _find_cuts(fbuf, finfo, uniq)
                for _k, _v in f_cand_ops.items():
                    all_cand_ops.setdefault(_k, set()).update(
                        (op_start, id(obj)) for op_start in _v)
                if fcuts:
                    _apply_to_stream(obj, fcuts, 0)
            walk_forms(fres, depth + 1, seen)

    seen_forms = set()
    for page in pdf.pages:
        try:
            contents = page.get('/Contents')
        except Exception:
            contents = None
        if contents is None:
            continue
        streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]

        # /Contents 多流拼接成一个逻辑流：q/Q 与 CTM 跨流继承，必须拼起来扫描
        parts = []
        buf = bytearray()
        for st in streams:
            if st is None:
                continue
            try:
                d = st.read_bytes()
            except Exception:
                d = b''
            parts.append((st, len(buf), len(buf) + len(d)))
            buf.extend(d)
            buf.extend(b"\n")
        if buf:
            try:
                font_info = _font_maps_res(page.get('/Resources'))
            except Exception:
                font_info = {}
            cuts, cand_ops = _find_cuts(buf, font_info, uniq)
            all_cand_ops.update({k: set(v) for k, v in cand_ops.items()})
            if cuts:
                per_stream = {}
                for cut in cuts:
                    start, end = cut[0], cut[1]
                    for st, o0, o1 in parts:
                        if o0 <= start and end <= o1:
                            per_stream.setdefault(id(st), (st, o0, []))[2].append(cut)
                            break
                for st, o0, mycuts in per_stream.values():
                    _apply_to_stream(st, mycuts, o0)

        # Form 内部流（含嵌套）
        try:
            walk_forms(page.get('/Resources'), 0, seen_forms)
        except Exception:
            pass

    # 命中口径：候选覆盖到的操作符只要被实际修改过就算命中（多个候选可能共享同一条操作符，
    # 去重只保留一个动作，但不能因此把另一个候选记成"未删除"）。
    for n_cand, raw, _, _ in uniq:
        ops_cov = all_cand_ops.get(raw, set())
        hit = False
        for it in ops_cov:
            key = it if not isinstance(it, tuple) else it[0]
            if key in applied_ops:
                hit = True
                break
        hits[raw] = 1 if hit else 0
    unmatched = [raw for n_cand, raw, _, _ in uniq if hits.get(raw, 0) == 0]
    return removed_ops, hits, unmatched, removed_chars

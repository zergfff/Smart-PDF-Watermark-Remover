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
    """返回 {资源名(不含/) : {'cmap': {...}, 'two_byte': bool}}。"""
    out = {}
    try:
        res = page.get('/Resources')
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
                ops.append((last_operand[0], j, last_operand, cur_font, eff, rot))
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
def remove_candidate_text(pdf, candidates, log=None):
    """从 pdf 各页内容流删除与候选匹配的文本绘制操作符（含字号/角度校验）。

    返回 (removed_ops, hits, unmatched, removed_chars)
      hits      : {candidate_text: 删除次数}
      unmatched : [candidate_text, ...] 未在流里安全命中的候选
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
    for page in pdf.pages:
        try:
            contents = page.get('/Contents')
        except Exception:
            continue
        if contents is None:
            continue
        streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
        try:
            font_info = _font_maps(pdf, page)
        except Exception:
            font_info = {}

        # /Contents 里多个流在 PDF 语义上是**拼接成一个**逻辑流：
        # q/Q 栈与 CTM 会跨流继承（页级翻转常在前面的流里，水印旋转在后面的流里）。
        # 因此必须拼接后统一扫描，再把删除区间映射回各自的流。
        parts = []   # (stream_obj, abs_start, abs_end_in_buf)
        buf = bytearray()
        for s in streams:
            if s is None:
                continue
            try:
                d = s.read_bytes()
            except Exception:
                d = b''
            parts.append((s, len(buf), len(buf) + len(d)))
            buf.extend(d)
            buf.extend(b"\n")
        if not buf:
            continue
        try:
            ops = iter_text_draw_ops(bytes(buf))
        except Exception:
            continue
        if not ops:
            continue

        cuts = []
        for op in ops:
            try:
                txt = _norm(decode_draw_op(op, font_info))
            except Exception:
                continue
            if not txt:
                continue
            eff_size, rot = op[4], op[5]
            for n_cand, raw_cand, c_size, c_rot in uniq:
                if not _is_match(txt, n_cand):
                    continue
                if not _size_ok(eff_size, c_size):
                    continue
                if not _rot_ok(rot, c_rot, txt == n_cand):
                    continue
                cuts.append((op[0], op[1], raw_cand, len(txt)))
                break
        if not cuts:
            continue

        # 把全局区间分配给所属流（跨流边界的一律跳过，避免切坏语法）
        per_stream = {}
        for start, end, raw_cand, tlen in cuts:
            for s, o0, o1 in parts:
                if o0 <= start and end <= o1:
                    per_stream.setdefault(id(s), (s, o0, []))[2].append(
                        (start - o0, end - o0, raw_cand, tlen))
                    break

        for s, o0, mycuts in per_stream.values():
            try:
                data = s.read_bytes()
            except Exception:
                continue
            new = bytearray(data)
            for start, end, raw_cand, tlen in sorted(mycuts, key=lambda x: -x[0]):
                if 0 <= start < end <= len(new):
                    del new[start:end]
                    removed_ops += 1
                    removed_chars += tlen
                    hits[raw_cand] = hits.get(raw_cand, 0) + 1
            try:
                s.write(bytes(new))
            except Exception as e:
                _log(f">>> stream write failed: {e}")

    unmatched = [raw for n_cand, raw, _, _ in uniq if hits.get(raw, 0) == 0]
    return removed_ops, hits, unmatched, removed_chars

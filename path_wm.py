"""
PDF 路径解析和删除工具
======================
从 PDF 内容流中提取路径块及其属性（填充色、线条色、透明度），
支持按属性匹配删除路径。
"""
from typing import Optional
import pikepdf

_PAINT_OPS = frozenset({'f', 'F', 'f*', 'S', 's', 'B', 'B*', 'b', 'b*', 'h'})
_PATH_CMDS = frozenset({'m', 'l', 'c', 'v', 'y', 'h', 're'})
_COLOR_SUFFIXES = frozenset({'rg', 'RG', 'k', 'K', 'g', 'G', 'cs', 'CS'})


def _tokenize_stream(content: str):
    """把内容流拆成 token 序列，保留每个 token 的真实起止偏移。"""
    tokens = []
    pos = 0
    for raw in content.splitlines(keepends=True):
        line_start = pos
        pos += len(raw)
        line_no_nl = raw.rstrip('\r\n')
        line_end = line_start + len(line_no_nl)
        idx = line_start
        while idx < line_end:
            while idx < line_end and line_no_nl[idx - line_start:idx - line_start + 1] in (' ', '\t'):
                idx += 1
            if idx >= line_end:
                break
            tok_start = idx
            while idx < line_end and line_no_nl[idx - line_start:idx - line_start + 1] not in (' ', '\t'):
                idx += 1
            tokens.append((line_no_nl[tok_start - line_start:idx - line_start], tok_start, idx))
    return tokens


def parse_color_command(cmd: str) -> Optional[tuple]:
    """解析颜色命令为 RGB tuple (0.0-1.0)。"""
    parts = cmd.strip().split()
    if not parts:
        return None
    last = parts[-1]
    if last in ('rg', 'RG'):
        if len(parts) >= 4:
            try:
                return (float(parts[-4]), float(parts[-3]), float(parts[-2]))
            except ValueError:
                return None
    if last in ('k', 'K'):
        if len(parts) >= 5:
            try:
                c, m, y, k = (float(parts[-5]), float(parts[-4]),
                               float(parts[-3]), float(parts[-2]))
                r = 1.0 - min(1.0, c + k)
                g = 1.0 - min(1.0, m + k)
                b = 1.0 - min(1.0, y + k)
                return (max(0.0, r), max(0.0, g), max(0.0, b))
            except ValueError:
                return None
    if last in ('g', 'G'):
        if len(parts) >= 2:
            try:
                v = float(parts[-2])
                return (v, v, v)
            except ValueError:
                return None
    if last in ('cs', 'CS'):
        return ('pattern', parts[0] if parts else '')
    return None


def _rgb_to_hex(c: tuple) -> str:
    """RGB tuple (0.0-1.0) → hex string 'RRGGBB'."""
    r = int(round(c[0] * 255))
    g = int(round(c[1] * 255))
    b = int(round(c[2] * 255))
    return f'{r:02x}{g:02x}{b:02x}'


def extract_path_blocks(content: str) -> list:
    """从内容流中提取路径块。"""
    blocks = []
    tokens = _tokenize_stream(content)

    cur_fill = None
    cur_stroke = None
    cur_opacity = None
    path_start_token = None
    path_start_pos = None
    in_path = False
    last_block_idx = -1

    def flush_block(end_pos: int, op: str):
        nonlocal in_path, path_start_token, path_start_pos, last_block_idx
        if in_path and path_start_token is not None:
            block = {
                'start': path_start_pos if path_start_pos is not None else end_pos,
                'end': end_pos,
                'start_line': path_start_token[0],
                'end_line': tokens[-1][0] if tokens else path_start_token[0],
                'fill_color': cur_fill,
                'stroke_color': cur_stroke,
                'opacity': cur_opacity,
                'painting_op': op,
            }
            blocks.append(block)
            last_block_idx = len(blocks) - 1
        in_path = False
        path_start_token = None
        path_start_pos = None

    def apply_color_to_last(c, is_fill=True):
        """颜色操作符出现在绘制之后时的补偿：只补该通道的**空缺**，绝不覆盖已记录的颜色。

        原实现无条件把 fill/stroke 都改成新颜色，会把上一个已绘制块的颜色改错
        （实测：clean_contents() 重写后 `0 0 0 RG` 紧跟 `rg .749`，填充色被改成黑色，
         导致"按填充色匹配"命中 0 —— 于是只能靠几何兜底删，颜色区分失效）。
        """
        nonlocal last_block_idx
        if last_block_idx < 0:
            return
        if is_fill:
            if blocks[last_block_idx].get('fill_color') is None:
                blocks[last_block_idx]['fill_color'] = c
        else:
            if blocks[last_block_idx].get('stroke_color') is None:
                blocks[last_block_idx]['stroke_color'] = c

    for idx, (tok, start, end) in enumerate(tokens):
        op = tok

        if op in _PATH_CMDS:
            if not in_path:
                path_start_token = tokens[idx]
                path_start_pos = start
                in_path = True
            continue

        if op in _PAINT_OPS:
            flush_block(end, op)
            continue

        if op == 'gs':
            if idx > 0:
                try:
                    cur_opacity = float(tokens[idx - 1][0])
                    if last_block_idx >= 0:
                        blocks[last_block_idx]['opacity'] = cur_opacity
                except ValueError:
                    pass
            continue

        if op in ('rg', 'RG'):
            if idx >= 3:
                try:
                    c = (float(tokens[idx - 3][0]), float(tokens[idx - 2][0]), float(tokens[idx - 1][0]))
                    if op == 'rg':
                        cur_fill = c           # 仅填充色
                        apply_color_to_last(c, True)
                    else:
                        cur_stroke = c         # 仅描边色（原实现会连带改掉填充色 → 颜色归属错）
                        apply_color_to_last(c, False)
                except ValueError:
                    pass
            continue

        if op in ('k', 'K'):
            if idx >= 4:
                try:
                    c, m, y, k = (float(tokens[idx - 4][0]), float(tokens[idx - 3][0]),
                                  float(tokens[idx - 2][0]), float(tokens[idx - 1][0]))
                    rgb = (1.0 - min(1.0, c + k), 1.0 - min(1.0, m + k), 1.0 - min(1.0, y + k))
                    if op == 'k':
                        cur_fill = rgb
                        apply_color_to_last(rgb, True)
                    else:
                        cur_stroke = rgb
                        apply_color_to_last(rgb, False)
                except ValueError:
                    pass
            continue

        if op in ('g', 'G'):
            if idx >= 1:
                try:
                    v = float(tokens[idx - 1][0])
                    c = (v, v, v)
                    if op == 'g':
                        cur_fill = c
                        apply_color_to_last(c, True)
                    else:
                        cur_stroke = c
                        apply_color_to_last(c, False)
                except ValueError:
                    pass
            continue

        if op in ('cs', 'CS'):
            if idx >= 1:
                pat = ('pattern', tokens[idx - 1][0])
                if op == 'cs':
                    cur_fill = pat
                    apply_color_to_last(pat, True)
                else:
                    cur_stroke = pat
                    apply_color_to_last(pat, False)
            continue

        if in_path and op not in ('w', 'd', 'ri', 'i', 'j', 'J', 'M', 'TR', 'q', 'Q', 'cm', 'BT', 'ET', 'Do'):
            flush_block(end, op)

    if in_path and path_start_token is not None and tokens:
        end_pos = tokens[-1][1] + len(tokens[-1][0])
        flush_block(end_pos, 'end')

    return blocks


def colors_match(c1, c2, tolerance=0.02):
    """比较两个颜色是否匹配（浮点容差）。"""
    if c1 is None and c2 is None:
        return True
    if c1 is None or c2 is None:
        return False
    if isinstance(c1, tuple) and len(c1) == 2 and c1[0] == 'pattern':
        return c1 == c2
    if not isinstance(c1, (tuple, list)) or not isinstance(c2, (tuple, list)):
        return False
    if len(c1) != len(c2):
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(c1, c2))


def find_matching_paths(content: str, fill_color=None, stroke_color=None,
                        opacity=None, match_fill=True, match_stroke=True,
                        match_opacity=True) -> list:
    """查找匹配指定属性的路径块。"""
    blocks = extract_path_blocks(content)
    matches = []
    for block in blocks:
        if match_fill and fill_color is not None:
            if not colors_match(block['fill_color'], fill_color):
                continue
        if match_stroke and stroke_color is not None:
            if not colors_match(block['stroke_color'], stroke_color):
                continue
        if match_opacity and opacity is not None:
            # 软条件：只有当块**自身记录了**透明度且与用户设定明显不同时才过滤。
            # 绝大多数路径流里没有 /GS 透明度（记为 None），
            # 旧实现把 None 当"不匹配"直接滤掉 → 用户选好了填充色却删 0 个。
            if block['opacity'] is not None and abs(block['opacity'] - opacity) > 0.01:
                continue
        matches.append(block)
    return matches


def remove_matching_paths(content: str, fill_color=None, stroke_color=None,
                          opacity=None, match_fill=True, match_stroke=True,
                          match_opacity=True) -> tuple:
    """从内容流中删除匹配路径。返回 (new_content, removed_count)。"""
    matches = find_matching_paths(content, fill_color, stroke_color, opacity,
                                   match_fill, match_stroke, match_opacity)
    if not matches:
        return content, 0
    matches.sort(key=lambda x: x['start'], reverse=True)
    new_content = content
    for block in matches:
        s, e = block['start'], block['end']
        if 0 <= s <= e <= len(new_content):
            new_content = new_content[:s] + new_content[e:]
    return new_content, len(matches)


def count_matching_paths(content: str, fill_color=None, stroke_color=None,
                          opacity=None, match_fill=True, match_stroke=True,
                          match_opacity=True) -> tuple:
    """只计数匹配的路径块，不修改文本。返回 (匹配数, 匹配块列表)。"""
    matches = find_matching_paths(content, fill_color, stroke_color, opacity,
                                   match_fill, match_stroke, match_opacity)
    return len(matches), matches


def get_page_stream(pdf, page) -> Optional[str]:
    """获取页面的合并内容流文本。"""
    try:
        contents = page.get('/Contents')
        if contents is None:
            return None
        streams = []
        if isinstance(contents, pikepdf.Array):
            for item in contents:
                try:
                    streams.append(item.read_bytes())
                except Exception:
                    try:
                        obj = pdf.get_object(item)
                        if obj is not None:
                            streams.append(obj.read_bytes())
                    except Exception:
                        pass
        else:
            try:
                streams.append(contents.read_bytes())
            except Exception:
                pass
        if not streams:
            return None
        return b'\n'.join(streams).decode('latin-1', errors='replace')
    except Exception:
        return None


def set_page_stream(pdf, page, new_content: str) -> bool:
    """将修改后的内容流写回页面。"""
    try:
        # 旧实现写的是 pikepdf.Name.Stream —— 那是 Name 对象不是 Stream，
        # make_indirect 会报错 → 每次都返回 False（写入失败），
        # 于是"内容流按颜色删除"这一整段实际上从未生效，全靠几何兜底在删（颜色不参与）。
        data = new_content.encode('latin-1')
        try:
            obj = pikepdf.Stream(pdf, data)
        except Exception:
            # 退路：直接构造 stream 字典对象
            obj = pdf.make_indirect(pikepdf.Dictionary(
                Length=len(data), **{'/Filter': None}))
            obj.write(data)
        page['/Contents'] = obj
        return True
    except Exception:
        return False

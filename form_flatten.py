import re
import pikepdf

DO_RE = re.compile(rb"/([A-Za-z0-9.]+)\s+Do\b")


def _iter_page_streams(page):
    contents = page.get("/Contents")
    if contents is None:
        return []
    if isinstance(contents, pikepdf.Array):
        return list(contents)
    return [contents]


def _get_resource_dict(page_or_obj):
    res = page_or_obj.get("/Resources")
    if res is None:
        return pikepdf.Dictionary()
    try:
        out = pikepdf.Dictionary()
        for k in res.keys():
            out[k] = res[k]
        return out
    except Exception:
        return pikepdf.Dictionary()


def _form_matrix(form):
    m = form.get("/Matrix")
    if m is None:
        return [1, 0, 0, 1, 0, 0]
    try:
        vals = [float(x) for x in m]
    except Exception:
        return [1, 0, 0, 1, 0, 0]
    if len(vals) != 6:
        return [1, 0, 0, 1, 0, 0]
    return vals


def _form_bbox(form):
    bbox = form.get("/BBox")
    if bbox is None:
        return [0, 0, 595, 842]
    try:
        return [float(x) for x in bbox]
    except Exception:
        return [0, 0, 595, 842]


def _clone_obj(obj):
    if obj is None:
        return None
    if obj.is_indirect:
        return obj.get_object()
    if isinstance(obj, pikepdf.Dictionary):
        out = pikepdf.Dictionary()
        for k in obj.keys():
            try:
                out[k] = _clone_obj(obj[k])
            except Exception:
                continue
        return out
    if isinstance(obj, pikepdf.Array):
        return pikepdf.Array([_clone_obj(x) for x in obj])
    return obj


def _merge_resources(pdf, page, extra=None):
    base = _clone_obj(page.get("/Resources")) or pikepdf.Dictionary()
    if extra is None:
        return base
    try:
        for k, v in extra.items():
            if k not in base:
                base[k] = _clone_obj(v)
    except Exception:
        pass
    return base


def _num(x):
    return f"{float(x):.12g}".encode("latin-1")


def _mmat(vals):
    return b" ".join(_num(x) for x in vals)


def _flatten_stream(pdf, data, owner_res=None, depth=0, max_depth=8):
    matches = list(DO_RE.finditer(data))
    if not matches:
        return data
    if depth >= max_depth or owner_res is None:
        return data
    xo = owner_res.get("/XObject") if isinstance(owner_res, pikepdf.Dictionary) else None
    if xo is None:
        return data
    forms = {}
    for name in xo.keys():
        try:
            obj = xo[name]
            if obj is None:
                continue
            if str(obj.get("/Subtype")) == "/Form":
                forms[name] = obj
        except Exception:
            continue
    if not forms:
        return data

    out = bytearray()
    pos = 0
    for m in matches:
        key = pikepdf.Name("/" + m.group(1).decode("latin-1"))
        if key not in forms:
            continue
        out += data[pos:m.start()]
        form = forms[key]
        matrix = _form_matrix(form)
        inner_res = _clone_obj(form.get("/Resources"))
        merged = _merge_resources(pdf, owner_res, inner_res)

        inline = bytearray()
        inline += b"q\n"
        inline += pikepdf.unparse_content_stream([("", [])]) if False else b""
        inline += pikepdf.Dictionary().unparse() if False else b""
        inline += merged.unparse()
        inline += b"\n"
        inline += _mmat(matrix) + b" cm\n"
        inner = _flatten_stream(pdf, form.read_bytes(), merged, depth=depth + 1, max_depth=max_depth)
        inline += inner
        inv = [1.0 / matrix[0] if matrix[0] else 1.0, 0.0, 0.0, 1.0 / matrix[3] if matrix[3] else 1.0, 0.0, 0.0]
        inline += b" " + _mmat(inv) + b" cm\n"
        inline += b"Q\n"
        out += inline
        pos = m.end()
    out += data[pos:]
    return bytes(out)


def flatten_page_forms(pdf, page, include_existing_do=True):
    """Append page-level Form XObject streams into page Contents, recursively flattening Do calls.

    This intentionally ignores whether /Do appears in the current content stream:
    every page-level Form XObject is inlined so all elements become first-class
    page content and can be deleted like any other object.
    """
    res = page.get("/Resources")
    if res is None:
        return 0, []
    xo = res.get("/XObject")
    if xo is None:
        return 0, []
    forms = []
    for name in xo.keys():
        try:
            obj = xo[name]
            if obj is None:
                continue
            if str(obj.get("/Subtype")) == "/Form":
                forms.append((name, obj))
        except Exception:
            continue
    if not forms:
        return 0, []

    base_res = _clone_obj(res)
    streams = _iter_page_streams(page)
    existing = b"".join(s.read_bytes() if s is not None else b"" for s in streams)
    if include_existing_do:
        existing = _flatten_stream(pdf, existing, base_res)

    appended = bytearray()
    for name, obj in forms:
        matrix = _form_matrix(obj)
        inner_res = _clone_obj(obj.get("/Resources"))
        merged = _merge_resources(pdf, base_res, inner_res)
        block = bytearray()
        block += b"q\n"
        block += merged.unparse() + b"\n"
        block += _mmat(matrix) + b" cm\n"
        inner = _flatten_stream(pdf, obj.read_bytes(), merged)
        block += inner
        inv = [1.0 / matrix[0] if matrix[0] else 1.0, 0.0, 0.0, 1.0 / matrix[3] if matrix[3] else 1.0, 0.0, 0.0]
        block += b" " + _mmat(inv) + b" cm\n"
        block += b"Q\n"
        appended += block

    if appended:
        existing += b"\n" + appended
    new_stream = pikepdf.Stream(pdf, bytes(existing))
    page["/Contents"] = new_stream
    return len(forms), [name.lstrip('/') for name, _ in forms]


def flatten_all_page_forms(pdf, verbose=False):
    total = 0
    for page in pdf.pages:
        try:
            n, names = flatten_page_forms(pdf, page)
            total += n
            if verbose and names:
                print('flattened', len(names), names[:10])
        except Exception:
            continue
    return total
